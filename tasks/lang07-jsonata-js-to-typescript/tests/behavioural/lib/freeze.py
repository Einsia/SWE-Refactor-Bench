#!/usr/bin/env python3
"""Build State A, generate the corpus, and capture the answers.

Runs once, when the verifier image is built. Afterwards the reference tree is
removed from the image, so this is the only moment at which anything in this task
can answer a JSONata question authoritatively. Everything the grading stage
compares against is produced here.

The reference is State A itself, built by its own `tools/build.js` from the
extracted tarball. Nothing about it is written for the verifier: it is the
repository shipped to the agent, which is what makes the expectations answers
about the tree under test rather than about a lookalike kept beside this module.
`identity.py` then grades that same build against this same corpus and requires
every case to match, which is the check that the freeze produced a passable target
at all.

Why the answers are captured at build time rather than at grading time
---------------------------------------------------------------------
The obvious alternative -- ship the reference and run it beside the submission --
is what the verification stage does, and it is right there because the two trees
must be compared under identical conditions. It is wrong here, and more sharply
wrong for this task than for its siblings. A JavaScript reference is the answer to
a JavaScript-to-TypeScript port: not a hint, not a head start, the artifact
itself. And the grading image cannot be made unable to run it, because `node` is
the runtime the *submission* needs. Every other task in this family can hide its
reference behind a missing toolchain; this one has to hide it by not shipping it.
Freezing means the grading image contains inputs and outputs and no engine.

The cost is that the corpus cannot be regenerated at grading time. The `fresh`
families are the answer to that: they are generated here too, from seeds that
appear in no published document, so they are new to the submission without being
new to the image. "Fresh" is about what the submission has seen, not about when
the file was written.

What this file refuses to do
----------------------------
It never writes an expectation it cannot account for. Three things are checked
against `catalog.py` before anything is written, and each aborts the build:

  - the set of JSONata error codes the corpus reaches must be exactly
    `CODES_REACHED`. A code that appears and is not listed means the inventory
    published in `instruction.md` is fiction; one that is listed and does not
    appear means it is a claim about coverage the corpus does not have.
  - the set of panic causes must be exactly `PANIC_CAUSES`. The cause tokens are a
    closed set in the probe, and the two the corpus reaches are the two a port can
    be asked to reproduce; a third appearing means the generator wandered into
    `stack-overflow` or `thrown-object` territory, where the answer is a fact about
    the machine.
  - no `Panic` may carry a message the fixtures did not author. `classifyThrown`
    already drops the host runtime's wording at the source, so this is the check
    that it still does -- the failure it catches is a probe edited to be chattier,
    which would silently make V8's sentences part of every expectation.

Positional pairing, and the two answer shapes
---------------------------------------------
Expectations pair to cases by position, never by the `id` in the response: the
protocol family collapses onto id 0 by design. So the expectation file is written
one line per case, in corpus order, and the count is asserted at both ends.

A case's answer is not always one response line. `executor.ProbeRunner._solo_answer`
gives a multi-line protocol case the whole of stdout, verbatim, because what those
cases assert lives in the relationship between response lines -- that a malformed
line draws exactly one answer and does not consume the good request behind it.
This module applies the same two rules through the same code: it drives the
reference with `executor.ProbeRunner` rather than with a loop of its own, so a
change to how an answer is assembled cannot land on one side of the differential
only.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import catalog  # noqa: E402
import executor  # noqa: E402
import gen  # noqa: E402
import vlib  # noqa: E402

LIB_DIR = Path(__file__).resolve().parent

# State A's build entry point and the two paths it produces. All three are
# contract, recorded in source-contract.json under `build_contract`, and
# `build.py` names the same constants -- it has to build the same tree this
# captured from, the same way.
BUILD_SCRIPT = "tools/build.js"
BUILD_OUTPUT = "dist"
PROBE_JS = "dist/probe.js"

STEMS = ("main", "fresh", "protocol", "fixture")

# How long the reference gets for one stem. `main` is 10,598 cases through a
# single node process; measured at about three seconds, so this is not a threshold
# anything is graded against -- it is the point at which a hung build should stop
# being someone's afternoon.
CAPTURE_TIMEOUT = 1800.0


def log(message: str) -> None:
    print(f"freeze: {message}", flush=True)


def node_path() -> str:
    """The absolute path to `node`, resolved once.

    `vlib.base_env` replaces the environment rather than extending it, and its PATH
    is the grading image's -- which is right, and is what keeps a variable the agent
    phase exported out of a graded run. It also means a bare `node` in an argv is
    resolved against that PATH and not against the one this script was started
    with, so the freeze finds nothing on a machine that installs node anywhere
    else. Resolving here and passing the absolute path costs nothing in the image,
    where the two agree, and is the difference between this script running and not
    running everywhere else.
    """
    found = shutil.which("node")
    if not found:
        raise SystemExit(
            "freeze: no `node` on PATH. The reference is JavaScript and is built "
            "and driven by node; the verifier image installs it before this runs.")
    return found


def run(argv: list[str], *, cwd: Path | None = None,
        timeout: float = 1800.0) -> subprocess.CompletedProcess:
    started = time.monotonic()
    proc = subprocess.run(argv, cwd=str(cwd) if cwd else None,
                          capture_output=True, timeout=timeout)
    elapsed = time.monotonic() - started
    log(f"$ {' '.join(argv)}  ({elapsed:.1f}s, exit {proc.returncode})")
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout.decode("utf-8", "replace"))
        sys.stderr.write(proc.stderr.decode("utf-8", "replace"))
        raise SystemExit(f"freeze: command failed: {' '.join(argv)}")
    return proc


def build_reference(source: Path) -> Path:
    """Run State A's own build and return the probe it produced.

    The reference *is* State A, built in place by the script it ships with. There
    is no separate reference tree beside this module and there must not be: a copy
    would let the expectations be captured from something that merely resembled
    what the agent was given, and the resemblance would go stale the first time
    either side was edited.

    `dist/` is not in the tarball -- see `pack.sh` -- so this step is also the
    check that the shipped tree builds at all. `tools/build.js` runs the probe's
    own self-check before it writes anything, so a build that exits 0 has already
    proved every advertised op dispatches and answers.
    """
    script = source / BUILD_SCRIPT
    if not script.is_file():
        raise SystemExit(
            f"freeze: {script} does not exist. The Dockerfile unpacks "
            f"data/original.tar.gz -- State A -- and its {BUILD_SCRIPT} is what "
            f"the expectations are captured from, so this means that step was "
            f"skipped or failed.")
    run([node_path(), BUILD_SCRIPT], cwd=source, timeout=600.0)
    probe = source / PROBE_JS
    if not probe.is_file():
        raise SystemExit(
            f"freeze: the build produced no {probe}. `tools/build.js` writes "
            f"{BUILD_OUTPUT}/ and the probe is one of its members; a build that "
            f"exited 0 without it means the script was edited and this constant "
            f"was not.")
    log(f"reference built: {probe} ({probe.stat().st_size} bytes)")
    return probe


def capture(probe: Path, source: Path, cases: list[dict],
            log_sink: vlib.Log, stem: str) -> list[bytes]:
    """Run the reference over one stem's cases and return its answers in order.

    Driven through `executor.ProbeRunner`, not through a loop written here. That is
    the whole point: the runner decides what may share a process, how a multi-line
    case's answer is assembled, and what happens after a crash, and every one of
    those decisions has to be identical on the two sides of the differential. A
    capture loop of its own would be a second implementation of the grading path,
    and the cases where the two disagreed would be exactly the cases nothing else
    can see.
    """
    requests = [executor.request_for(case) for case in cases]
    runner = executor.ProbeRunner([node_path(), str(probe)], source, log_sink,
                                  label=f"reference/{stem}")
    outcome = runner.run_all(requests)

    if outcome.crashed:
        raise SystemExit(
            f"freeze: {stem}: the reference crashed while answering "
            f"(exit {outcome.returncode}). Its stderr tail was:\n"
            f"{outcome.stderr_tail}\n"
            f"An expectation cannot be captured from a run that died: the cases "
            f"behind the crash were re-driven in a fresh process and the victim "
            f"has no answer at all.")
    for complaint in outcome.complaints:
        log(f"  WARN {stem}: {complaint}")

    answers: list[bytes] = []
    for case, response in zip(cases, outcome.ordered):
        if response is None:
            raise SystemExit(
                f"freeze: {stem}: case {case['id']} drew no answer from the "
                f"reference. Pairing is positional, so there is no way to write "
                f"a file that skips it -- every case must have an expectation or "
                f"leave the corpus.")
        answers.append(response.raw)
    return answers


def check_answers(stem: str, cases: list[dict],
                  answers: list[bytes]) -> tuple[set[str], set[str]]:
    """Reject anything that cannot serve as an expectation.

    Returns `(codes, causes)` -- the JSONata error codes and the panic cause
    tokens this stem reached, for the inventory checks in `main`.

    Per-case validity only, and deliberately so: coverage is decided once, over
    every stem together, because a code reached by `main` and not by `fresh` is not
    a problem and a code reached by neither is.
    """
    codes: set[str] = set()
    causes: set[str] = set()
    multi_line = 0

    for case, blob in zip(cases, answers):
        cid = case["id"]
        if not blob.strip():
            # `driver.load_expected` pairs by position over non-blank lines, so a
            # blank expectation would shift every later pairing by one and grade
            # each case against its neighbour's answer.
            raise SystemExit(
                f"freeze: {stem}: case {cid} produced an empty response; the "
                f"positional pairing cannot survive a blank line")

        # A multi-line answer is a whole stdout stream: several JSON objects, each
        # on its own line, with the trailing newline kept. Every line still has to
        # be a well-formed response, and the count still has to be the one the
        # case was written to draw -- `raw_lines` says how many requests went in,
        # and the whole subject of these cases is how many answers came back.
        raw = blob.rstrip(b"\n")
        lines = raw.split(b"\n")
        is_multi = len(lines) > 1
        if is_multi:
            multi_line += 1
            if "raw_lines" not in case:
                raise SystemExit(
                    f"freeze: {stem}: case {cid} is not a protocol case but drew "
                    f"{len(lines)} response lines. An ordinary case is one "
                    f"request and its answer is one line; more than one means the "
                    f"probe wrote something extra, and it would be frozen as "
                    f"though it were the answer.")
            if not blob.endswith(b"\n"):
                raise SystemExit(
                    f"freeze: {stem}: case {cid} drew {len(lines)} response lines "
                    f"and the last has no terminator. The expectation is compared "
                    f"verbatim, so freezing this would require every submission "
                    f"to reproduce a missing newline.")

        for offset, line in enumerate(lines):
            where = f"{cid} line {offset + 1}" if is_multi else cid
            try:
                payload = json.loads(line)
            except ValueError as exc:
                raise SystemExit(
                    f"freeze: {stem}: {where} produced unparseable JSON "
                    f"({exc}): {line[:200]!r}")
            if not isinstance(payload, dict):
                raise SystemExit(
                    f"freeze: {stem}: {where} produced {type(payload).__name__}, "
                    f"not an object: {line[:200]!r}")
            if "ok" not in payload:
                raise SystemExit(
                    f"freeze: {stem}: {where} produced no `ok` field: "
                    f"{line[:200]!r}")
            if payload.get("id") is None:
                raise SystemExit(
                    f"freeze: {stem}: {where} produced no `id` field. It is part "
                    f"of the response and the comparison is on bytes, so an "
                    f"expectation without one can never be met; it is also what "
                    f"lets the executor batch requests and attribute a crash to a "
                    f"case. (Grading pairs answers to requests by position, not "
                    f"by this field -- see BatchOutcome -- so this is not about "
                    f"pairing.)")

            error = payload.get("error")
            if not isinstance(error, dict):
                continue
            kind = error.get("kind")
            if kind == "JsonataError":
                code = error.get("code")
                if not code:
                    raise SystemExit(
                        f"freeze: {stem}: {where} is a JsonataError with no "
                        f"`code`. The kind is defined by that field -- see "
                        f"`classifyThrown` -- so an error reported under it "
                        f"without one means the probe was edited and the two no "
                        f"longer agree.")
                codes.add(str(code))
            elif kind == "Panic":
                cause = error.get("cause")
                if not cause:
                    raise SystemExit(
                        f"freeze: {stem}: {where} is a Panic with no `cause`. The "
                        f"cause token is the only portable thing about a panic and "
                        f"the expectation would otherwise say nothing about why it "
                        f"happened.")
                causes.add(str(cause))
                # The message check. `classifyThrown` publishes a Panic's message
                # only when `fixtures.authoredError` raised it, and the fixtures
                # all begin theirs the same way. This does not grade the prefix --
                # nothing compares against it -- it is the assertion that the
                # normalisation at the source is still happening, because a probe
                # edited to pass the message through unconditionally would put
                # V8's own sentences ("Cannot create property 'n' on number '1'")
                # into three expectations, and no reader of the corpus would see
                # it.
                if "message" in error:
                    message = str(error["message"])
                    if not message.startswith(AUTHORED_PREFIX):
                        raise SystemExit(
                            f"freeze: {stem}: {where} panicked with the message "
                            f"{message!r}, which the fixtures did not author. A "
                            f"host runtime composes that sentence about its own "
                            f"failure and names the values it happened to be "
                            f"given, so no port can be asked to reproduce it. "
                            f"`classifyThrown` drops it; if this fires, that stopped "
                            f"being true. Normalise it at the source -- do not "
                            f"exempt the case from comparison.")

    if multi_line:
        log(f"  {stem}: {multi_line} multi-line answer(s), frozen verbatim")
    return codes, causes


# Every message the fixtures write begins with this. See `authoredError` in
# probe-fixtures.js: the marker that decides whether a message reaches the wire is
# a property on the error, not this string, and this is only used to check that the
# decision was made -- see the comment at its use.
AUTHORED_PREFIX = "probe fixture: "


# -- the expectation file ---------------------------------------------------
#
# One JSON envelope per line, in corpus order: `{"case": <id>, "answer": <text>}`.
#
# The envelope earns its keep twice. An answer can be several lines -- ten protocol
# cases are graded on their whole stdout stream -- and a bare NDJSON file of
# answers cannot hold one: the newlines inside it would read back as separate
# expectations and shift every later case onto its neighbour's answer. And the
# `case` field lets the pairing be *checked* rather than trusted. Pairing is still
# positional, because it has to be (the protocol family collapses onto id 0), but a
# file that has drifted out of order now says so at load time instead of failing a
# hundred cases against the wrong answers.
#
# `answer` is text, not bytes. Everything the probe writes is ASCII by
# construction: `encodeString` escapes every codepoint above 0x7e, which is also
# why a lone surrogate in a result survives the round trip -- it arrives as
# `\ud800` and stays that way.


def write_expected(path: Path, cases: list[dict], answers: list[bytes]) -> None:
    """Write one envelope per case, in corpus order."""
    if len(cases) != len(answers):
        raise SystemExit(
            f"freeze: {path.name}: {len(cases)} cases and {len(answers)} answers")
    with path.open("w", encoding="utf-8") as fh:
        for case, blob in zip(cases, answers):
            try:
                text = blob.decode("ascii")
            except UnicodeDecodeError as exc:
                # Not a limitation being worked around: the probe's own
                # `encodeString` escapes everything above 0x7e, so a non-ASCII byte
                # in a response means it emitted a string some other way.
                raise SystemExit(
                    f"freeze: {path.name}: case {case['id']} answered with a "
                    f"non-ASCII byte ({exc}). Every string the probe writes goes "
                    f"through `encodeString`, which escapes above 0x7e, so this "
                    f"means some response was built by another route.")
            fh.write(json.dumps({"case": case["id"], "answer": text},
                                ensure_ascii=True, sort_keys=True) + "\n")


def load_expected(path: Path) -> list[tuple[str, bytes]]:
    """Read an expectation file back as `(case_id, answer_bytes)`, in order.

    The one reader. `driver.py` calls this too, so a format the freeze can write and
    the driver cannot read is impossible rather than merely unlikely.
    """
    out: list[tuple[str, bytes]] = []
    with path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                envelope = json.loads(line)
                case_id = envelope["case"]
                answer = envelope["answer"]
            except (ValueError, KeyError, TypeError) as exc:
                raise SystemExit(
                    f"{path}:{lineno}: not an expectation envelope ({exc}). Each "
                    f"line is {{'case': <id>, 'answer': <text>}}; see "
                    f"freeze.write_expected.")
            out.append((str(case_id), answer.encode("ascii")))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="freeze the lang07 expectations")
    ap.add_argument("--source", required=True, type=Path,
                    help="extracted State A; it is built in place and is the "
                         "reference")
    ap.add_argument("--assets", required=True, type=Path,
                    help="asset root; the corpus goes in <assets>/corpus")
    ap.add_argument("--work", required=True, type=Path,
                    help="scratch directory for the log")
    ap.add_argument("--data", type=Path, default=None,
                    help="directory holding the vendored jsonata test suite")
    ap.add_argument("--suite", type=Path, default=None,
                    help="suite.toml, to check its weights against the budgets")
    args = ap.parse_args(argv)

    data_dir = args.data or (LIB_DIR.parent / "data")
    corpus = args.assets / "corpus"
    args.work.mkdir(parents=True, exist_ok=True)
    corpus.mkdir(parents=True, exist_ok=True)

    probe = build_reference(args.source)

    log("generating the corpus")
    gen._self_check(data_dir)
    counts = gen.write_corpus(corpus, data_dir)
    for stem in STEMS:
        log(f"  {stem}: {counts[stem]} cases")

    log_sink = vlib.Log(args.work / "freeze.log")
    reached: set[str] = set()
    causes: set[str] = set()

    for stem in STEMS:
        cases = [json.loads(line) for line in
                 (corpus / f"{stem}-cases.jsonl").read_text(
                     encoding="utf-8").splitlines() if line.strip()]
        log(f"capturing {stem} ({len(cases)} cases)")
        answers = capture(probe, args.source, cases, log_sink, stem)
        stem_codes, stem_causes = check_answers(stem, cases, answers)
        reached |= stem_codes
        causes |= stem_causes

        expected_path = corpus / f"{stem}-expected.jsonl"
        write_expected(expected_path, cases, answers)
        log(f"  wrote {expected_path.name} "
            f"({expected_path.stat().st_size} bytes)")

        # Read back what was just written, through the same loader the driver
        # uses. A file that is correct as a list and wrong as a file -- an answer
        # containing a bare newline, an envelope that does not round-trip -- would
        # otherwise only fail at grading time, against a submission, as a case
        # that mysteriously cannot pass.
        reread = load_expected(expected_path)
        if len(reread) != len(cases):
            raise SystemExit(
                f"freeze: {stem}: wrote {len(answers)} expectations but the file "
                f"reads back as {len(reread)} against {len(cases)} cases")
        for index, (case, (cid, blob)) in enumerate(zip(cases, reread)):
            if cid != case["id"]:
                raise SystemExit(
                    f"freeze: {stem}: expectation {cid!r} sits where case "
                    f"{case['id']!r} does; the file does not round-trip in order")
            if blob != answers[index]:
                raise SystemExit(
                    f"freeze: {stem}: expectation for {cid!r} does not read back "
                    f"as the bytes captured; the envelope loses something")

    problems: list[str] = []

    missing = sorted(set(catalog.CODES_REACHED) - reached)
    surprise = sorted(reached - set(catalog.CODES_REACHED))
    if missing:
        problems.append(
            f"catalog.CODES_REACHED lists {len(missing)} code(s) the corpus does "
            f"not reach: {missing}. The coverage instruction.md publishes would "
            f"be a claim about cases that do not exist.")
    if surprise:
        problems.append(
            f"the corpus reaches {len(surprise)} code(s) catalog.CODES_REACHED "
            f"does not list: {surprise}. Either add them, or move them out of "
            f"CODES_UNREACHED where they are recorded as unreachable with a "
            f"measured reason.")

    cause_missing = sorted(set(catalog.PANIC_CAUSES) - causes)
    cause_surprise = sorted(causes - set(catalog.PANIC_CAUSES))
    if cause_missing:
        problems.append(
            f"catalog.PANIC_CAUSES lists cause(s) no case reaches: "
            f"{cause_missing}")
    if cause_surprise:
        problems.append(
            f"the corpus reaches panic cause(s) not in catalog.PANIC_CAUSES: "
            f"{cause_surprise}. `panicCause` in probe.js has a closed set and two "
            f"of its tokens are deliberately unreachable -- `stack-overflow` "
            f"because the depth at which a runtime exhausts its stack is a "
            f"property of the machine. A third token appearing means a case is "
            f"measuring the host.")

    family_counts: dict[str, int] = {}
    for stem in STEMS:
        for family, n in catalog.family_counts_in(
                corpus / f"{stem}-cases.jsonl").items():
            family_counts[family] = family_counts.get(family, 0) + n
    problems.extend(catalog.check_catalog(family_counts, args.suite))

    if problems:
        for problem in problems:
            sys.stderr.write(f"freeze: {problem}\n")
        raise SystemExit(f"freeze: {len(problems)} problem(s)")

    log(f"error codes reached: {len(reached)} of {catalog.CODES_DECLARED_TOTAL} "
        f"declared")
    log(f"panic causes reached: {sorted(causes)}")
    log("catalog checks passed")

    inventory = {
        "case_totals": counts,
        "family_counts": family_counts,
        "reached_error_codes": sorted(reached),
        "panic_causes": sorted(causes),
        "module_weights": catalog.module_weights(),
        "family_weights": catalog.family_weights(family_counts),
        "release_weights": catalog.release_weights(),
        "type_weights": catalog.type_weights(),
        "weight_total": catalog.budget_total(),
    }
    (args.assets / "inventory.json").write_text(
        json.dumps(inventory, indent=1, sort_keys=True) + "\n", encoding="utf-8")

    # The reference build is removed from the image by the Dockerfile, not here.
    # Deleting it in this script would make the deletion invisible to anyone
    # reading the image definition, and for this task that deletion is the single
    # most important line in the file: `dist/` is a working JavaScript jsonata and
    # the grading image has a `node` that will happily run it.
    shutil.rmtree(args.work / "scratch", ignore_errors=True)

    total = sum(counts.values())
    log(f"done: {total} cases frozen")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
