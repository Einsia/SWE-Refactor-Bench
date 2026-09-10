#!/usr/bin/env python3
"""The identity self-test: State A, graded as if it were a submission.

Runs when the verifier image is built, immediately after `freeze.py`, and fails
the build if State A does not answer every frozen case exactly and satisfy every
row of the type surface.

Why this exists
---------------
Every other check in this suite measures a submission against the frozen corpus.
None of them measures the corpus itself. A generator that emitted an unanswerable
case, an expectation captured through a code path no live process can reach, a
request the executor encodes one way at grading time and the freeze encoded
another -- each of those presents as the *submission* failing, and there is
nothing in a report of four hundred failed cases that says which side is wrong.

So the suite is pointed at the tree it was cut from. This is a rewrite task and a
rewrite preserves behaviour, so the thing being rewritten has to pass. A rate
below 1.0 here is a bug in this suite, never a fact about anyone's port, and it is
a build failure rather than a warning because a corpus with one unanswerable case
in it silently taxes every submission that ever runs against it.

What makes it a real test rather than a tautology
------------------------------------------------
Less of this is free than in the sibling tasks, and the difference is worth being
precise about.

`freeze.capture` drives the reference through `executor.ProbeRunner` in serve
mode -- deliberately, so that the rules about what may share a process and how a
multi-line answer is assembled exist in exactly one place. This test drives it
through the same class in the same mode. So the part of a sibling task's identity
run that came for free, two independent readers meeting in the middle, does not
come for free here and is bought explicitly instead: `--batch` is driven over the
whole corpus (`batch_differential`), which is the second reader, the second
writer and the second flush discipline over one `handle`.

There is a second thing the freeze and this test share for free and must not:
the clock. Both run on build day, so an expectation that captured *today's date*
agrees with itself here and fails every day after. `clock_differential` answers
the whole corpus a third time with the probe's wall clock moved 400 days, and
requires every answer byte-identical. That is the only pass that can distinguish
a fact about State A from a fact about when the image was built, and it found the
one case in this corpus that was the latter.

What the serve-mode pass proves on its own is still substantial, and none of it is
implied by the freeze having succeeded:

  * every case is answerable *twice*. The freeze answered each one once; a case
    whose answer depends on iteration order, a hash seed, a cached regex compiled
    on first use or the order the fixtures were touched agrees with itself once
    and not twice. This is the only place that difference is visible before a
    submission is ever graded.
  * `executor.request_for` and `executor.encode_request` produce, at grading time,
    the bytes the freeze recorded an answer to -- they are called again here,
    against the same cases, in a separate process from the one that captured.
  * `executor.parse_stream` recovers one response per request from a real pipe,
    including the protocol cases whose ids are unrecoverable and which therefore
    run one process at a time.
  * `freeze.load_expected` reads back what `freeze.write_expected` wrote. The
    envelope format has two jobs -- carrying a multi-line answer, and letting the
    positional pairing be checked rather than trusted -- and a round trip that
    lost either would show up here as ten protocol cases failing, not as a
    parsing error.
  * the 24 type-surface rows hold against the declarations State A's own build
    emits. `types-surface.py --check` validates the consumer files against the
    catalogue without compiling anything; this compiles them.

It does *not* prove the expectations are correct. Nothing in this task can: State
A is the definition of correct here, quirks included. That is what a rewrite
benchmark means, and it is why `check_answers` in `freeze.py` refuses individual
expectations it cannot classify -- correctness-adjacent judgement lives there, at
capture time, and not here.

Not shipped in the grading image
--------------------------------
The grading image has no State A tree: the Dockerfile deletes the reference after
the freeze, and the final stage deletes the suite's `data/` -- which is where
`original.tar.gz` lands, because `COPY . /tests/behavioural` copies the whole build
context and the tarball is in it. The copy is what makes the deletion load-bearing,
and the archive sweep beside it is what checks the deletion happened. This file
cannot run in the grading image and does not try to. It runs in the oracle stage,
where the tree still exists, and its result is a build failure or nothing.

That deletion is the constraint this whole task is shaped by. The old language and
the new one share a runtime, so the `node` a submission legitimately needs is the
`node` that would run a leftover reference -- see the docstring in `build.py`.
Hiding the toolchain, which is how every sibling task handles this, is not
available.
"""

from __future__ import annotations

import argparse
import importlib
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import catalog  # noqa: E402
import executor  # noqa: E402
import freeze  # noqa: E402
import vlib  # noqa: E402

# `types-surface.py` is not an identifier, so it cannot be imported by name. The
# hyphen matches every other module directory in the suite and `importlib` answers
# the syntax question, so the file is not renamed to satisfy it -- the same call and
# the same reasoning as in `driver.py`.
types_surface = importlib.import_module("types-surface")

STEMS = freeze.STEMS

# The declarations State A's build emits, and the module name the consumers import.
# Both are contract: `source-contract.json` records that the build emits
# `dist/jsonata.d.ts`, and every consumer says `require('jsonata')`, resolved
# through the harness tsconfig's `paths` -- which is how a consumer of the published
# package would reach it, rather than by a relative path into a build directory.
DECLARATIONS = "dist/jsonata.d.ts"
CONSUMER_LINK = "jsonata.d.ts"

# How long one stem gets. `main` is 10,598 cases through a single node process,
# measured at about three seconds; this is headroom for a hung process to stop being
# somebody's afternoon, not a threshold anything is graded against.
STEM_TIMEOUT = 1800.0

# How far the probe's wall clock is moved for the third pass, and the preload that
# moves it.
#
# Every component of the shifted instant differs from the real one -- year, month,
# day, weekday, day-of-year, hour, minute, second and millisecond -- because a case
# that defaults exactly one field from the clock is invisible under a shift that
# happens to preserve that field. 400 days is a year and five weeks, which moves the
# year and the weekday (400 mod 7 is 1); the odd hours, minutes, seconds and
# milliseconds move the rest. A round number of days would have missed a picture that
# names a date and defaults only the time.
CLOCK_SHIFT_MS = (400 * 86_400_000) + (7 * 3_600_000) + (13 * 60_000) + 11_000 + 137

# `%(shift)d` rather than an f-string or `.format`: the body is JavaScript and has
# braces in it.
CLOCK_SHIM_JS = """\
'use strict';
// Written by identity.py, loaded with `node -r`. Moves this process's wall clock and
// changes nothing else that any case can observe.
//
// `new Date()` with no arguments is the only spelling that reads the clock, so it is
// the only one redirected. Every other constructor call forwards its arguments
// untouched -- `new Date(millis)` inside `$fromMillis` still means what it says --
// and `Date.now()` moves with the clock so that an interval measured across it keeps
// its length, which is what the `timelimit` cases depend on.
//
// A Proxy rather than `class ShiftedDate extends Date`, because the subclass was
// visible to the corpus and this has to not be. The `register/host-hostDate` family
// binds a function returning `new Date(0)` and the probe reports the value as `x`
// *with its constructor's name* -- the fixture comment says so: it exists to
// distinguish "reports `x`" from "reports `x` with the right name". Under a subclass
// that name is `ShiftedDate`, and 22 cases drift for a reason that has nothing to do
// with the clock. Through the trap the instance is built by the real `Date` with the
// real prototype, so the name, `instanceof`, `constructor`, `Date.UTC` and
// `Date.parse` are all the genuine ones, and the only difference a case can see is
// which instant a zero-argument construction names.
const SHIFT = %(shift)d;
const RealDate = Date;
const realNow = RealDate.now;
globalThis.Date = new Proxy(RealDate, {
    construct(target, args, newTarget) {
        if (args.length === 0) {
            return Reflect.construct(target, [realNow() + SHIFT], newTarget);
        }
        return Reflect.construct(target, args, newTarget);
    },
    // `Date()` without `new` returns a string for the current instant. Nothing in
    // State A spells it that way, and a trap that silently did the wrong thing if
    // something did would be worse than one that is written down.
    apply() {
        return new RealDate(realNow() + SHIFT).toString();
    },
    get(target, prop, receiver) {
        if (prop === 'now') {
            return function now() { return realNow() + SHIFT; };
        }
        return Reflect.get(target, prop, receiver);
    },
});
"""


def log(message: str) -> None:
    print(f"identity: {message}", flush=True)


def load(corpus: Path, stem: str) -> tuple[list[dict], list[bytes]]:
    """One stem's cases and its frozen expectations, paired positionally.

    The expectations come back through `freeze.load_expected` -- the one reader --
    rather than by splitting the file here. Two reasons, and the second is the one
    that matters: an answer may be several lines (ten protocol cases are graded on
    their whole stdout stream), so a reader that split the file on newlines would
    read one such answer back as several expectations and shift every later case
    onto its neighbour's. And the envelope carries the case id, which lets the
    pairing be *checked*: positional pairing is not optional here, because the
    protocol family collapses onto id 0, but a file that has drifted out of order
    should say so at load time instead of failing a hundred cases against the wrong
    answers.
    """
    cases_path = corpus / f"{stem}-cases.jsonl"
    expected_path = corpus / f"{stem}-expected.jsonl"
    for path in (cases_path, expected_path):
        if not path.is_file():
            raise SystemExit(f"identity: {path} does not exist")

    cases = [json.loads(line) for line
             in cases_path.read_text(encoding="utf-8").splitlines()
             if line.strip()]
    expected = freeze.load_expected(expected_path)
    if len(cases) != len(expected):
        raise SystemExit(
            f"identity: {stem}: {len(cases)} cases against {len(expected)} "
            f"expectations. The pairing is positional and cannot be repaired here; "
            f"the freeze wrote one of the two files wrong.")

    answers: list[bytes] = []
    for case, (case_id, want) in zip(cases, expected):
        if case_id != case["id"]:
            raise SystemExit(
                f"identity: {stem}: expectation {case_id!r} sits where case "
                f"{case['id']!r} does. The two files have drifted out of order, "
                f"and grading them would compare every case from here on against "
                f"a neighbour's answer.")
        answers.append(want)
    return cases, answers


def grade(probe: Path, node: str, cases: list[dict], expected: list[bytes],
          work: Path, stem: str, limit: int) -> tuple[int, int, list[str]]:
    """Run one stem in serve mode and compare every response.

    Returns (passed, total, notes).
    """
    # `vlib.Log` is not a context manager, so the close is explicit. It matters: the
    # log holds an open file handle and this is called once per stem.
    probe_log = vlib.Log(work / f"identity-{stem}.log")
    try:
        runner = executor.ProbeRunner(
            prefix=[node, str(probe)], cwd=work, log=probe_log,
            label=f"state-a/{stem}")
        requests = [executor.request_for(case) for case in cases]
        started = time.monotonic()
        outcome = runner.run_all(requests)
        elapsed = time.monotonic() - started
    finally:
        probe_log.close()

    # Positional, through the same field `driver.grade_corpus` grades on. Not the
    # id-keyed dict: 117 protocol cases answer under id 0 and would overwrite each
    # other there, which cost an earlier task in this family 11 of its 33 protocol
    # cases silently -- see `BatchOutcome`.
    if len(outcome.ordered) != len(cases):
        raise AssertionError(
            f"{stem}: ran {len(cases)} cases and got {len(outcome.ordered)} "
            f"answer slots")

    passed = 0
    notes: list[str] = []
    for case, want, got in zip(cases, expected, outcome.ordered):
        ok, detail, diff = executor.compare(want, got)
        if ok:
            passed += 1
        elif len(notes) < limit:
            notes.append(
                f"{stem}/{case.get('family', '?')}/{case['id']}: {detail}"
                + (f"\n{diff}" if diff else ""))

    log(f"{stem}: {passed}/{len(cases)} in {elapsed:.1f}s "
        f"({outcome.spawns} process(es))")
    if outcome.crashed:
        notes.append(
            f"{stem}: the probe died mid-batch (exit {outcome.returncode}). State A "
            f"crashing on its own corpus means some request reaches a failure the "
            f"probe cannot catch and turn into a response. stderr: "
            f"{outcome.stderr_tail[:400]}")
    for complaint in outcome.complaints[:5]:
        notes.append(f"{stem}: unparseable output: {complaint}")
    return passed, len(cases), notes


def batch_differential(probe: Path, node: str, cases: list[dict],
                       expected: list[bytes], work: Path,
                       stem: str) -> tuple[int, list[str]]:
    """The same stem through `--batch <in> <out>`, compared to the frozen answers.

    This is the second reader and the second writer, and in this task it has to be
    asked for explicitly. In the sibling tasks the freeze captured through the file
    mode and grading ran over a pipe, so their identity run compared the two modes as
    a side effect of existing. Here the freeze runs through `ProbeRunner` in serve
    mode -- for a reason that is written down in `freeze.capture` and still holds --
    so serve-versus-serve would leave `--batch` unexercised at image build time,
    exactly as it was unexercised at grading time until `struct/probe-batch-matches-
    serve` was added.

    Only the cases that can share one process go in the file: a multi-line protocol
    case is answered by the whole of stdout, which is a claim about a stream and not
    about a line, and putting one in a shared file would make the line count differ
    from the case count for the rest of the stem. Those cases are covered by the
    serve pass. What goes here is every single-line case, which for `main` is all
    10,598 of them, and they are compared to the frozen answers directly rather than
    to the serve run: the frozen answer is the stronger comparison, since a defect
    both modes shared would agree with itself and still be wrong.

    Returns (cases compared through the file mode, notes); empty notes is a pass. The
    count is what lets `main` tell "this stem has nothing single-line in it", which is
    true and fine for `protocol`, from "the file mode never ran at all", which is not.
    """
    solo = [(index, case) for index, case in enumerate(cases)
            if "raw_lines" not in case]
    if not solo:
        # `protocol` is legitimately all multi-line: every one of its 117 cases is a
        # claim about a whole stream. That is not a defect and must not be reported as
        # one, or a healthy corpus fails the build. Whether the file mode went
        # unexercised is a question about the run, not about one stem, so it is
        # answered in `main` from the returned counts.
        log(f"{stem}: no single-line cases; the file mode is covered by other stems")
        return 0, []

    in_path = work / f"batch-{stem}-in.ndjson"
    out_path = work / f"batch-{stem}-out.ndjson"
    payload = b"".join(
        executor.encode_request(executor.request_for(case)) for _, case in solo)
    in_path.write_bytes(payload)
    if out_path.exists():
        out_path.unlink()

    started = time.monotonic()
    result = vlib.run([node, str(probe), "--batch", str(in_path), str(out_path)],
                      cwd=work, timeout=STEM_TIMEOUT)
    elapsed = time.monotonic() - started
    if not result.ok:
        return 0, [f"{stem}: `--batch` exited {result.returncode}"
                   f"{' after timing out' if result.timed_out else ''}: "
                   f"{result.stderr.decode('utf-8', 'replace')[:400]}"]
    if not out_path.is_file():
        return 0, [f"{stem}: `--batch` exited 0 without creating its output file"]

    produced = out_path.read_bytes()
    lines = produced.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    if len(lines) != len(solo):
        return 0, [f"{stem}: `--batch` answered {len(solo)} requests with "
                   f"{len(lines)} line(s). The file mode has to write one response "
                   f"per request, in order, exactly as the pipe does."]

    # `parse_stream` builds a single-line `Response` with `raw=line`, so a frozen
    # single-line answer never carries its own terminator -- and every case in `solo`
    # is single-line by construction. Compared exactly, therefore, with no tolerance
    # for a trailing newline on either side: accepting both spellings here would make
    # the comparison unable to fail on framing, which is half of what the file mode
    # gets wrong. (Framing across the *whole* stream is `struct/probe-batch-matches-
    # serve`; this is framing per answer, and the two failures look different.)
    notes: list[str] = []
    for (index, case), line in zip(solo, lines):
        want = expected[index]
        if line == want:
            continue
        if len(notes) < 12:
            notes.append(
                f"{stem}/{case['id']}: `--batch` differs from the frozen answer. "
                f"file: {line[:200]!r} / frozen: {want[:200]!r}")
    log(f"{stem}: {len(solo) - len(notes)}/{len(solo)} through --batch in "
        f"{elapsed:.1f}s")
    return len(solo), notes


def clock_shim(work: Path) -> Path:
    """Write the preload that moves the probe's wall clock. Returns its path."""
    shim = work / "shifted-clock.js"
    shim.write_text(CLOCK_SHIM_JS % {"shift": CLOCK_SHIFT_MS}, encoding="utf-8")
    return shim


def clock_differential(probe: Path, node: str, cases: list[dict],
                       expected: list[bytes], work: Path, stem: str,
                       shim: Path) -> tuple[int, list[str]]:
    """The same stem again with the probe's clock moved, against the same answers.

    This is the pass that decides whether an expectation is a fact about State A or a
    fact about the day the image was built, and it is here because reading the corpus
    cannot answer that question.

    A frozen answer is captured on build day and compared on grading day. Every case
    that reads the clock is supposed to read it through `$now`, `$millis` or
    `$random`, which `clock: "pinned"` shadows by `registerFunction` -- the one hook a
    consumer has -- so those are stable by construction. The failure mode is a case
    that reaches the clock some other way, and there is one in this engine:
    `parseDateTime` fills the components to the left of the most significant one the
    picture names from `this.environment.timestamp`, which `evaluate` sets from a live
    `new Date()` on every call. `registerFunction` cannot reach a field. So
    `$toMillis('7 pm', '[h] [P]')` answers with the grading day's date, its
    expectation holds the build day's, and the case passes on exactly one day in
    history -- the day it was frozen, which is the day this test runs. Freeze and
    identity agreeing is what hid it: both read the same clock.

    Moving the clock breaks that agreement. Anything whose answer depends on when the
    process ran now says so, before a submission is ever graded, and the report can
    name the mechanism instead of presenting as a submission that got one date wrong.

    It is a build failure, not a warning. Such a case taxes every submission that ever
    runs against this corpus, and it taxes them silently: the port is correct, the
    expectation is unreachable, and nothing in a one-line diff between two epoch
    millisecond values says which side is wrong.

    What this does not catch is a clock-reading case whose answer is coarse enough to
    survive the shift -- `$substringBefore($now(), '-')` under a shift of exactly one
    year would agree with itself. Hence the shift moves every component, and hence
    `gen.py`'s `_self_check` also refuses the *spelling* statically: the static rule
    catches what no shift would move, and this catches what no pattern would predict.
    Neither subsumes the other.

    Returns (cases compared, notes); empty notes is a pass.
    """
    probe_log = vlib.Log(work / f"identity-{stem}-shifted.log")
    try:
        runner = executor.ProbeRunner(
            prefix=[node, "-r", str(shim), str(probe)], cwd=work, log=probe_log,
            label=f"state-a/{stem}/shifted-clock")
        requests = [executor.request_for(case) for case in cases]
        started = time.monotonic()
        outcome = runner.run_all(requests)
        elapsed = time.monotonic() - started
    finally:
        probe_log.close()

    if outcome.crashed:
        return 0, [
            f"{stem}: the probe died mid-batch under `-r {shim.name}` (exit "
            f"{outcome.returncode}). The preload wraps `Date` in a Proxy and changes "
            f"nothing else, and the unshifted pass over this same stem ran first, so "
            f"this is the shim failing to load or the engine reaching `Date` in a way "
            f"the traps do not answer -- not a finding about the corpus. stderr: "
            f"{outcome.stderr_tail[:400]}"]
    if len(outcome.ordered) != len(cases):
        return 0, [
            f"{stem}: the shifted run answered {len(cases)} cases in "
            f"{len(outcome.ordered)} slots"]

    notes: list[str] = []
    drifted = 0
    for case, want, got in zip(cases, expected, outcome.ordered):
        ok, detail, diff = executor.compare(want, got)
        if ok:
            continue
        drifted += 1
        if len(notes) < 12:
            notes.append(
                f"{stem}/{case.get('family', '?')}/{case['id']}: answers differently "
                f"when the clock moves by {CLOCK_SHIFT_MS} ms, so its frozen answer "
                f"is the build date and not a fact about State A. It reads the clock "
                f"through a path `clock: \"pinned\"` cannot shadow -- most likely a "
                f"`$toMillis` picture that names no year, which makes `parseDateTime` "
                f"default the date from `environment.timestamp`. Wrap the call so the "
                f"defaulted field does not reach the answer, or drop the case. "
                f"{detail}" + (f"\n{diff}" if diff else ""))

    log(f"{stem}: {len(cases) - drifted}/{len(cases)} stable under a shifted clock "
        f"in {elapsed:.1f}s ({outcome.spawns} process(es))")
    return len(cases), notes


def type_surface(build_dir: Path, consumers: Path, work: Path,
                 tsc: str) -> list[str]:
    """Compile all 24 consumers against the declarations State A's build emitted.

    The reference has to pass the `types` module for the same reason it has to pass
    the corpus: a row that State A fails is a row no submission can be asked to pass,
    and it would be discovered as an unreachable ceiling rather than as a bug here.
    Twelve of the rows are negatives, so this is not "does it compile" -- it is that
    State A's declarations *reject* the twelve misuses, each at its marked line with
    one of the codes the catalogue accepts for that reason.

    The parsing, the marker rule and the diagnostic vocabulary come from
    `types-surface.py` itself rather than being restated here. What this function
    does not share with the graded module is the driver: that one records checks and
    weights, and this one raises or returns notes. So the compile loop is written
    twice and the *judgement* is written once, which is the half that could drift
    without anybody noticing -- a marker rule that differed between the two would
    make the reference pass a row the submission is graded on differently.

    Returns notes; an empty list is a pass.
    """
    # Resolved through the manifest by the graded module's own resolver, not by
    # joining a constant. That is how a consumer finds types and therefore how the
    # `types` module finds them; State A resolving by a path this file happened to
    # know would leave the resolution itself ungraded until a submission hit it.
    declarations, how = types_surface._declarations(build_dir)
    if declarations is None:
        return [f"State A's own declarations do not resolve the way a consumer would "
                f"find them: {how}. Every type case resolves the module through the "
                f"manifest, so all 24 rows would fail and the `types` module's "
                f"{catalog.TYPES_BUDGET:g} points would be unreachable."]
    log(f"declarations: {declarations} ({how})")
    expected_at = (build_dir / DECLARATIONS).resolve()
    if declarations != expected_at:
        # Not a failure. `source-contract.json` records `dist/jsonata.d.ts` as what
        # State A's build emits, and a divergence means the contract and the tree
        # have drifted -- worth saying out loud, and not worth failing a build over,
        # since what the type cases actually need is that a consumer can find them.
        shown = (declarations.relative_to(build_dir)
                 if declarations.is_relative_to(build_dir) else declarations)
        log(f"NOTE: the contract names {DECLARATIONS}; the manifest resolved to "
            f"{shown}")

    tsconfig = consumers / "tsconfig.json"
    if not tsconfig.is_file():
        return [f"the harness tsconfig is missing at {tsconfig}"]

    notes: list[str] = []
    positives = negatives = 0
    for case_id, kind, _weight, accept, note in catalog.TYPE_CASES:
        consumer = consumers / f"{case_id}.ts"
        if not consumer.is_file():
            notes.append(f"{case_id}: no consumer file at {consumer}")
            continue

        case_work = work / "types" / case_id
        if case_work.exists():
            shutil.rmtree(case_work)
        case_work.mkdir(parents=True, exist_ok=True)
        shutil.copy2(tsconfig, case_work / "tsconfig.json")
        shutil.copy2(consumer, case_work / "case.ts")
        shutil.copy2(declarations, case_work / CONSUMER_LINK)

        result = vlib.run([tsc, "--pretty", "false", "--project", "tsconfig.json"],
                          cwd=case_work, timeout=180.0)
        diags = types_surface._diagnostics(result)

        if kind == "positive":
            positives += 1
            if result.ok and not diags:
                continue
            notes.append(
                f"{case_id}: must compile clean against State A's own declarations "
                f"and did not (tsc exit {result.returncode}). It asserts: {note}\n"
                f"{types_surface._describe(diags)}")
            continue

        negatives += 1
        want_line = types_surface._marker_line(
            consumer.read_text(encoding="utf-8"))
        on_line = [d for d in diags if d["line"] == want_line
                   and d["file"].startswith("case.ts")]
        if any(d["code"] in accept for d in on_line):
            continue
        if on_line:
            detail = (f"rejected at line {want_line} with "
                      f"TS{on_line[0]['code']}, which is not in the accept-set "
                      f"{list(accept)}")
        elif not diags:
            detail = (f"compiled clean, so State A's declarations accept this "
                      f"misuse and the row is unreachable for everyone")
        else:
            detail = (f"{len(diags)} diagnostic(s), none at the marked line "
                      f"{want_line}")
        notes.append(f"{case_id}: {detail}. It asserts: {note}\n"
                     f"{types_surface._describe(diags)}")

    log(f"type surface: {len(catalog.TYPE_CASES) - len(notes)}/"
        f"{len(catalog.TYPE_CASES)} rows ({positives} positive, {negatives} "
        f"negative)")
    return notes


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="grade State A against the frozen corpus and the type surface; "
                    "require 1.0 on both")
    ap.add_argument("--source", required=True, type=Path,
                    help="a State A tree, with package.json and tools/build.js in it")
    ap.add_argument("--corpus", required=True, type=Path,
                    help="the frozen corpus directory (<assets>/corpus)")
    ap.add_argument("--work", required=True, type=Path,
                    help="scratch directory for the logs and the type compiles")
    ap.add_argument("--consumers", type=Path, default=None,
                    help="the 24 consumer .ts files and their tsconfig "
                         "(<assets>/consumers). Omitted skips the type surface, "
                         "which the build-time invocation never does.")
    ap.add_argument("--probe", type=Path, default=None,
                    help="an already-built dist/probe.js; skips the build")
    ap.add_argument("--stems", default=",".join(STEMS),
                    help="comma-separated subset of stems, for debugging. The "
                         "build-time invocation runs all four; a partial run cannot "
                         "make the claim this test exists to make.")
    ap.add_argument("--max-notes", type=int, default=12,
                    help="failures to describe per stem before truncating")
    ap.add_argument("--skip-batch", action="store_true",
                    help="skip the --batch differential, for debugging. It is the "
                         "second reader and the second writer; without it this run "
                         "grades one code path twice.")
    ap.add_argument("--skip-clock-shift", action="store_true",
                    help="skip the shifted-clock differential, for debugging. It is "
                         "the only pass that can tell an expectation about State A "
                         "from an expectation about the day the image was built.")
    args = ap.parse_args(argv)

    stems = tuple(s for s in args.stems.split(",") if s)
    unknown = [s for s in stems if s not in STEMS]
    if unknown:
        raise SystemExit(f"identity: unknown stem(s): {', '.join(unknown)}")
    args.work.mkdir(parents=True, exist_ok=True)

    # Built by `freeze.build_reference`, which is the function that built the tree
    # the expectations were captured from. Calling it again here rather than
    # reimplementing `npm run build`'s effect means the two cannot drift: if the
    # freeze's idea of how State A builds changes, this test's does too, and a
    # mismatch between them is unrepresentable rather than merely unlikely.
    probe = args.probe or freeze.build_reference(args.source)
    if not probe.is_file():
        raise SystemExit(f"identity: {probe} does not exist")
    node = freeze.node_path()
    build_dir = probe.parent.parent

    shim = None if args.skip_clock_shift else clock_shim(args.work)

    total_passed = total_cases = total_batched = total_shifted = 0
    all_notes: list[str] = []
    for stem in stems:
        cases, expected = load(args.corpus, stem)
        passed, count, notes = grade(probe, node, cases, expected, args.work, stem,
                                     args.max_notes)
        total_passed += passed
        total_cases += count
        all_notes.extend(notes)
        if not args.skip_batch:
            batched, batch_notes = batch_differential(
                probe, node, cases, expected, args.work, stem)
            total_batched += batched
            all_notes.extend(batch_notes)
        if shim is not None:
            shifted, clock_notes = clock_differential(
                probe, node, cases, expected, args.work, stem, shim)
            total_shifted += shifted
            all_notes.extend(clock_notes)

    if total_cases == 0:
        raise SystemExit(
            "identity: no cases were graded. An empty corpus passes every check that "
            "divides by its size, so it is a failure here.")

    rate = total_passed / total_cases
    log(f"{total_passed}/{total_cases} cases, rate {rate:.6f}")

    # The stems no longer each demand to have exercised the file mode, because
    # `protocol` legitimately cannot. The demand belongs here instead: across every
    # stem that ran, `--batch` must have answered something. Zero means the second
    # invocation went entirely unmeasured and this run graded one code path twice --
    # which is the exact hole `--skip-batch` exists to open deliberately.
    if not args.skip_batch:
        if total_batched == 0:
            raise SystemExit(
                "identity: `--batch` compared 0 cases. Every stem that ran was "
                "all-multi-line, so the file mode was never exercised and this run "
                "cannot claim State A satisfies both invocations.")
        log(f"--batch compared {total_batched} case(s) across {len(stems)} stem(s)")

    # The same demand as `--batch`'s, for the same reason: a pass that silently
    # compared nothing reports as a pass. Here it can only happen if every stem was
    # empty, which `total_cases == 0` above has already refused -- so this is cheap
    # and it means the log line below is never a claim about zero cases.
    if shim is not None:
        if total_shifted == 0:
            raise SystemExit(
                "identity: the shifted-clock pass compared 0 cases, so this run "
                "cannot tell which expectations are facts about State A and which "
                "are facts about today's date.")
        # "compared", not "stable": the per-stem lines above carry the ratio, and a
        # run with findings reaches this line before it reports them. Saying "stable"
        # here would have this line contradict the finding printed under it.
        log(f"a shifted clock ({CLOCK_SHIFT_MS} ms) compared {total_shifted} case(s)")

    if args.consumers is not None:
        tsc = types_surface._tsc(vlib.Log(args.work / "identity-types.log"))
        all_notes.extend(
            type_surface(build_dir, args.consumers, args.work, tsc))
    else:
        log("WARNING: --consumers was not given, so the 24 type rows did not run; "
            "this run does not establish that the type surface is reachable")

    if all_notes:
        sys.stderr.write(
            f"\nidentity: {len(all_notes)} finding(s). State A is the definition of "
            f"correct for this corpus, so each of these is a bug in the corpus, the "
            f"request encoder, the comparison or the type table -- not a fact about "
            f"any port.\n\n")
        for note in all_notes[:60]:
            sys.stderr.write(f"  {note}\n")
        if len(all_notes) > 60:
            sys.stderr.write(f"  ... and {len(all_notes) - 60} more\n")

    if total_passed != total_cases:
        raise SystemExit(
            f"identity: rate {rate:.6f}, required 1.0. The suite grades submissions "
            f"against a corpus the original itself cannot answer.")
    if all_notes:
        raise SystemExit(
            f"identity: the corpus is answerable, and {len(all_notes)} other "
            f"finding(s) stand. Each one is a row or a mode the reference itself "
            f"does not satisfy, which makes it unreachable for every submission.")

    if len(stems) != len(STEMS):
        log(f"WARNING: only {len(stems)} of {len(STEMS)} stems ran; this run does "
            f"not establish that the whole corpus is answerable")

    invocations = ("one invocation (--batch was skipped)" if args.skip_batch
                   else "both invocations")
    clock = ("" if shim is None else ", on a clock 400 days from this one")
    surface = ("" if args.consumers is None
               else ", and satisfies every row of the type surface")
    log(f"State A answers every frozen case exactly, in {invocations}"
        f"{clock}{surface}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

