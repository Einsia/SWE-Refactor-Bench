#!/usr/bin/env python3
"""The release half of the `structure` module: the eight cases in
`catalog.STRUCT_CASES`.

This module has no corpus slice. It computes all eight from the built artifact, the
way `build` computes its ledger, and `_release` is in `catalog.NON_CORPUS_FAMILIES`
for exactly that reason.

Why these eight and not others
------------------------------
`catalog.STRUCT_DROPS` records the candidates that are not here, each with the
reason and the place it is really graded, so that reasoning is not repeated. What
is worth repeating is the property the eight share, because it is the reason they
are worth points at all: **the corpus cannot reach any of them.**

`executor.ProbeRunner._invoke` writes every request, closes stdin, waits for the
process to exit, and only then parses what it wrote, in the build tree, through the
argv prefix the ledger recorded. A port whose `dist/` only works while `src/` is
still sitting beside it, or whose `package.json` names an entry point the build no
longer produces, or that answers out of order, passes all 13,940 frozen cases -- the
bytes are all there, in the one place the corpus ever looks.

So the eight are measured by driving the build in ways `ProbeRunner` deliberately
cannot express: from somewhere else, with the tree behind it gone, through
`require` instead of through the probe, and with the responses read one line at a
time so their order is observable.

Every row was measured against the unpacked reference before it was written down,
and two candidates did not survive that -- `probe-flushes` and
`probe-skips-blank-lines`, both recorded in `catalog.STRUCT_DROPS` with what the
measurement said. A case here asserts something the reference does, not something a
reasonable probe ought to do.

Weights and reporting
---------------------
Weights come from `catalog.release_weights()`, keyed by case id, so this file never
decides what a case is worth. All eight are `required=False`: a failing required
check zeroes the whole module (`scoring.grade_behavioural`), and these are graded
findings, not gates. The gates are stage 1's, declared in `tests/evaluation.toml`.

Every check emits exactly once, whatever happens -- a check that silently does not
run leaves the module's denominator short, which reads as a smaller module rather
than as a missing measurement. `run()` asserts the count at the end.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import catalog
import executor
import vlib

# The deadline for the two live-pipe checks, which write everything, close stdin and
# read to EOF. The probe is already warm (`build` ran it) and a `hello` is
# microseconds of work, so this is orders of magnitude of headroom rather than a
# tight bound. It exists because one of the failures being measured *is* a hang: a
# probe that waits for more input after EOF never answers, and without a deadline
# that would consume the module's whole budget and be reported as the verifier
# timing out rather than as the finding it is.
BATCH_TIMEOUT = 60.0

# How many requests the drain check sends. Small, because the question is whether
# *every* line is answered rather than how many can be; the corpus already sends
# thousands.
DRAIN_REQUEST_COUNT = 5

# Ids for the ordering check: deliberately not ascending, so a probe that sorted its
# answers -- by id, or by anything correlated with id -- shows up as a difference
# rather than landing on the right sequence by luck. Ascending ids would make a sort
# indistinguishable from preserving order, which is the whole subject of the case.
ORDER_WIRE_IDS = (7, 3, 11, 1, 9, 5, 12, 2, 8, 4, 10, 6)

# The requests the relocation checks compare across two invocations. One per op, so a
# port that resolves something lazily -- a datetime table read on first `$fromMillis`,
# a regex engine required from a sibling path -- is exercised rather than assumed
# absent, and small enough that the comparison is legible when it fails.
#
# The answers are compared to *the same build's* answers in its own tree, never to a
# frozen expectation: what is being measured is whether relocation changed anything,
# and a port whose answers are wrong everywhere is graded for that by the corpus.
COMPARE_REQUESTS: tuple[dict, ...] = (
    {"id": 1, "op": "hello"},
    {"id": 2, "op": "ast", "expr": "a.b[0].c"},
    {"id": 3, "op": "eval", "expr": "$sum(a.b)", "input": {"a": {"b": [1, 2, 3]}}},
    {"id": 4, "op": "evalcb", "expr": "$string(1/3)"},
    {"id": 5, "op": "assign", "expr": "$x + $y",
     "assigns": [{"name": "x", "value": 1}, {"name": "y", "value": 2}]},
    {"id": 6, "op": "register", "expr": "$d(4)",
     "funcs": [{"name": "d", "impl": "double"}]},
)

# The consumer used by `library-requirable` and `package-main-resolves`. Written to
# the module's own work directory and pointed at the build with argv, never written
# inside the tree being graded: a file this module dropped into the submission's tree
# would then be part of what stage 3 reads.
#
# What it asserts, and why each line is here rather than in the Python:
#
#   * the export is callable. jsonata's published surface is a function, and a port
#     that shipped `{ default: fn }` -- which is what a TypeScript `export default`
#     compiles to without `esModuleInterop`-style downlevelling -- breaks every
#     `require('jsonata')` in existence while leaving `dist/probe.js` working, because
#     the probe imports its own sibling and can compensate.
#   * calling it yields something with `.evaluate`, and awaiting that yields a value.
#     `evaluate` returns a Promise in jsonata 2.x; `Promise.resolve` accepts a plain
#     value too, so a port that returned one synchronously is measured on its answer
#     rather than failed on its timing here. The corpus grades what `evaluate`
#     resolves to; this case grades that the path exists at all.
#   * the four other published methods are *present*, not called. `ast`, `assign`,
#     `registerFunction` and `errors` are all exercised for real by the corpus
#     through the probe's ops, so calling them here would double-charge; what the
#     corpus cannot see is a port that reaches them only through the probe's own
#     wiring and never put them on the object a consumer receives.
#
# The output is one JSON object on stdout, so the Python side grades fields instead of
# scraping prose.
CONSUMER_JS = r"""'use strict';
// Written by the behavioural verifier's `structure` module. Not part of the
// submission; lives in $SRB_WORK and is passed the require target on argv.
const target = process.argv[2];
const out = { target: target };
function finish() {
  process.stdout.write(JSON.stringify(out) + '\n');
  process.exit(0);
}
let lib;
try {
  lib = require(target);
} catch (err) {
  out.stage = 'require';
  out.error = String((err && err.stack) || err).split('\n').slice(0, 4).join(' | ');
  finish();
}
out.exportType = typeof lib;
if (typeof lib !== 'function') {
  out.stage = 'export';
  out.keys = (lib && typeof lib === 'object') ? Object.keys(lib).slice(0, 12) : [];
  finish();
}
let expr;
try {
  expr = lib('$sum(a.b) + $count(a.b)');
} catch (err) {
  out.stage = 'compile';
  out.error = String((err && err.message) || err).slice(0, 300);
  finish();
}
out.methods = ['ast', 'assign', 'errors', 'evaluate', 'registerFunction']
  .filter(function (name) { return expr && typeof expr[name] === 'function'; });
let settled;
try {
  settled = Promise.resolve(expr.evaluate({ a: { b: [10, 20, 12] } }));
} catch (err) {
  out.stage = 'evaluate';
  out.error = String((err && err.message) || err).slice(0, 300);
  finish();
}
settled.then(function (value) {
  out.stage = 'done';
  out.value = value;
  out.ok = value === 45;
  finish();
}, function (err) {
  out.stage = 'await';
  out.error = String((err && err.message) || err).slice(0, 300);
  finish();
});
"""

# What the consumer must resolve to. `$sum([10,20,12]) + $count([10,20,12])` is 45,
# and the arithmetic is here so a port that returned the sequence, or the count, or a
# string, is a different number rather than a coincidence.
CONSUMER_EXPECTED = 45

# The five methods the consumer looks for on the expression object. Named here rather
# than only inside the JavaScript so the report can say which were missing.
CONSUMER_METHODS = ("ast", "assign", "errors", "evaluate", "registerFunction")

# How long the consumer gets. It requires a library, compiles one expression and
# awaits one evaluation; the reference does it in tens of milliseconds. Generous
# because a port that pulled in a large module graph pays for it once.
CONSUMER_TIMEOUT = 60.0


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _weights() -> dict[str, float]:
    """Per-case weights, keyed by case id, from the catalog.

    Read once and looked up per case rather than passed around, so a case id that
    drifts from the table raises in `_add` instead of quietly scoring a default --
    which, in a module whose other half carries 22 weight over 117 protocol cases,
    would be a single release case silently worth more than fifteen of them.
    """
    return catalog.release_weights()


def _add(driver, weights: dict[str, float], case_id: str, passed: bool,
         summary: str, detail: str = "") -> None:
    """Record one release case at its catalogued weight."""
    if case_id not in weights:
        raise AssertionError(
            f"structure: {case_id!r} is not in catalog.release_weights(); the "
            f"handler and STRUCT_CASES disagree about which cases exist. "
            f"check_release_coverage() asserts this at image build time, so "
            f"reaching it here means this file and the catalog were changed apart.")
    driver.add(case_id, passed, summary, weight=weights[case_id],
               required=False, detail=detail)


def _tail_file(path: Path, limit: int = 600) -> str:
    if not path.is_file():
        return "<none>"
    try:
        blob = path.read_bytes()[-limit:]
    except OSError as exc:
        return f"<unreadable: {exc}>"
    return blob.decode("utf-8", "replace").strip() or "<empty>"


def _hello_request(request_id: int) -> bytes:
    return executor.encode_request({"id": request_id, "op": "hello"})


def _parse_ids(lines: list[bytes]) -> list[str]:
    """The id each response line echoes, in the order the lines arrived.

    Non-JSON and id-less lines are dropped rather than reported: the callers that
    use this are asking about *order*, and a malformed line is a different finding
    that the `_protocol` corpus cases already own.
    """
    out: list[str] = []
    for line in lines:
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict) and "id" in payload:
            out.append(str(payload["id"]))
    return out


class _LivePipe:
    """One probe process with stdin and stdout held open, read line by line.

    `executor.ProbeRunner` cannot express this, and not by oversight: `_invoke`
    hands the whole request stream to `subprocess.run`, which writes it all, waits
    for exit, and returns the output as one blob. That is the right shape for
    grading 13,940 frozen cases and the wrong shape for asking what order the lines
    arrived in or whether the process ended on its own -- by the time `run` returns,
    every answer is present and the process is gone either way.

    Both checks that use this could *almost* be done with `ProbeRunner`: the ids are
    in the blob and could be re-read in order. What cannot be done through it is
    distinguishing "answered everything and exited" from "answered everything and
    then hung", because `subprocess.run` waits, and a hang there is the module timing
    out rather than a finding.

    stderr goes to a *file*, never to a pipe. With stderr as a pipe and this loop
    reading only stdout, a probe that writes more than a pipe buffer to stderr blocks
    in `write` while this side blocks in `read`, and the check reports a hang the
    probe did not have. The file also survives for the report.
    """

    def __init__(self, prefix: list[str], cwd: Path, stderr_path: Path,
                 env: dict[str, str]) -> None:
        self.prefix = list(prefix)
        self.cwd = Path(cwd)
        self.stderr_path = stderr_path
        self.env = env
        self.proc: subprocess.Popen | None = None
        self._err = None

    def __enter__(self) -> "_LivePipe":
        self.stderr_path.parent.mkdir(parents=True, exist_ok=True)
        self._err = self.stderr_path.open("wb")
        self.proc = subprocess.Popen(
            self.prefix, cwd=str(self.cwd), env=self.env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._err,
            bufsize=0)
        return self

    def __exit__(self, *exc_info) -> None:
        proc = self.proc
        if proc is not None:
            for closer in (proc.stdin, proc.stdout):
                try:
                    if closer is not None:
                        closer.close()
                except OSError:
                    pass
            if proc.poll() is None:
                proc.kill()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
        if self._err is not None:
            self._err.close()

    # -- one direction at a time -------------------------------------------

    def send(self, line: bytes) -> None:
        assert self.proc is not None and self.proc.stdin is not None
        self.proc.stdin.write(line)
        self.proc.stdin.flush()

    def close_stdin(self) -> None:
        assert self.proc is not None
        if self.proc.stdin is not None:
            try:
                self.proc.stdin.close()
            except OSError:
                pass

    def read_line(self, deadline: float) -> bytes | None:
        """One line of stdout, or None if the deadline passed first.

        Reads a byte at a time up to the newline. That is slower than `readline` and
        it is the point: `readline` on a `bufsize=0` pipe blocks until a newline
        arrives with no way to give up, so a probe that stops mid-stream would hang
        this check rather than fail it, and a hang inside a module is reported as the
        verifier timing out.

        Uses `os.read` on the raw descriptor with `select` for the wait, so the
        deadline is honoured even when the probe writes a partial line and stops.
        """
        import select

        assert self.proc is not None and self.proc.stdout is not None
        fd = self.proc.stdout.fileno()
        chunks: list[bytes] = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            ready, _, _ = select.select([fd], [], [], min(remaining, 0.25))
            if not ready:
                if self.proc.poll() is not None and not chunks:
                    return b""      # exited with nothing more to say
                continue
            try:
                byte = os.read(fd, 1)
            except OSError:
                return b"".join(chunks) if chunks else b""
            if not byte:            # EOF
                return b"".join(chunks) if chunks else b""
            if byte == b"\n":
                return b"".join(chunks)
            chunks.append(byte)

    def read_to_eof(self, deadline: float) -> tuple[list[bytes], bool]:
        """Every remaining line, and whether EOF was reached before the deadline."""
        lines: list[bytes] = []
        while True:
            line = self.read_line(deadline)
            if line is None:
                return lines, False
            if line == b"":
                return lines, True
            lines.append(line)


# --------------------------------------------------------------------------- #
# the seven checks
# --------------------------------------------------------------------------- #


def _drain_once(prefix: list[str], cwd: Path, stderr_path: Path,
                env: dict[str, str], count: int) -> tuple[list[str], bool, int | None]:
    """Send `count` hellos, close stdin, read to EOF. Ids, at_eof, exit status."""
    with _LivePipe(prefix, cwd, stderr_path, env) as pipe:
        for index in range(1, count + 1):
            pipe.send(_hello_request(index))
        pipe.close_stdin()
        lines, at_eof = pipe.read_to_eof(time.monotonic() + BATCH_TIMEOUT)
        proc = pipe.proc
        assert proc is not None
        exited: int | None = None
        if at_eof:
            try:
                exited = proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                exited = None
        return _parse_ids(lines), at_eof, exited


def _check_probe_drains_stdin(driver, weights: dict[str, float],
                              prefix: list[str], cwd: Path,
                              env: dict[str, str]) -> None:
    """Every line answered, then exit 0 -- and the same with no lines at all.

    Two runs, because the empty one is the half a port is most likely to get wrong
    and the half nothing else grades. `run_all` never sends an empty batch, so a
    probe that blocks forever on an empty stdin, or that exits non-zero because it
    read no requests, is invisible to all 13,940 corpus cases and to every other
    module.

    Unlike the rest of the release cases this one asserts the exit status. That is a
    published rule -- `instruction.md` states exit 0 on a clean EOF -- and it is
    measured: the reference exits 0 for five requests and for none. A non-zero exit
    is not cosmetic either, since `executor.ProbeRunner` treats it as a crashed
    batch and resumes, so a port that exits 1 after answering correctly turns every
    batch into a resume storm.
    """
    case = "struct/probe-drains-stdin"
    try:
        ids, at_eof, exited = _drain_once(
            prefix, cwd, driver.work / "drain.stderr.log", env,
            DRAIN_REQUEST_COUNT)
        empty_ids, empty_eof, empty_exit = _drain_once(
            prefix, cwd, driver.work / "drain-empty.stderr.log", env, 0)
    except OSError as exc:
        _add(driver, weights, case, False,
             f"the probe could not be started for the drain check: {exc}")
        return

    want = [str(i) for i in range(1, DRAIN_REQUEST_COUNT + 1)]
    answered_all = ids == want
    clean_exit = at_eof and exited == 0
    empty_clean = empty_eof and empty_exit == 0 and not empty_ids
    passed = answered_all and clean_exit and empty_clean

    if passed:
        summary = (f"all {DRAIN_REQUEST_COUNT} requests were answered and the "
                   f"process exited 0 at EOF; an empty stdin also drew no answers "
                   f"and exited 0")
    elif not answered_all:
        summary = (f"{len(ids)} of {DRAIN_REQUEST_COUNT} requests were answered "
                   f"before the stream ended (ids {ids})")
    elif not clean_exit:
        summary = ("all requests were answered but the process "
                   + (f"exited {exited}" if at_eof and exited is not None else
                      f"did not end within {BATCH_TIMEOUT:g}s of stdin closing"))
    elif empty_ids:
        summary = (f"an empty stdin drew {len(empty_ids)} response line(s), which "
                   f"no request asked for")
    else:
        summary = ("requests are answered, but an empty stdin "
                   + (f"exited {empty_exit}" if empty_eof and empty_exit is not None
                      else f"did not end within {BATCH_TIMEOUT:g}s"))

    _add(driver, weights, case, passed, summary,
         detail=(f"ids answered: {ids} (expected {want})\n"
                 f"reached EOF on stdout: {at_eof}; exit status: "
                 f"{exited if exited is not None else 'still running'}\n"
                 f"empty stdin: {len(empty_ids)} line(s), EOF {empty_eof}, exit "
                 f"{empty_exit if empty_exit is not None else 'still running'}\n"
                 f"stderr: {_tail_file(driver.work / 'drain.stderr.log')}\n"
                 f"stderr (empty run): "
                 f"{_tail_file(driver.work / 'drain-empty.stderr.log')}"))


def _check_probe_answers_in_order(driver, weights: dict[str, float],
                                  prefix: list[str], cwd: Path,
                                  env: dict[str, str]) -> None:
    """Responses arrive in request order.

    Unreachable from the corpus, and for a reason worth stating precisely, because it
    is not the usual "the blob is all there by the time anything looks": this suite
    pairs answers to cases *positionally*, by `BatchOutcome.ordered`, exactly so that
    the protocol cases -- which all echo id 0 -- can be graded at all. Positional
    pairing means a probe that answered in some order of its own would be compared
    against its neighbours' expectations and fail nearly everywhere, at whatever
    module the reordering happened to fall in, reported as thousands of wrong answers
    rather than as one broken rule.

    So the corpus cannot grade this: it either cannot see it (one request per solo
    case, where order is vacuous) or it sees it as something else entirely. Graded
    here, once, where the report says what actually went wrong.

    The ids are not ascending (`ORDER_WIRE_IDS`), so preserving order and sorting are
    distinguishable.
    """
    case = "struct/probe-answers-in-order"
    stderr_path = driver.work / "order.stderr.log"
    try:
        with _LivePipe(prefix, cwd, stderr_path, env) as pipe:
            for wire_id in ORDER_WIRE_IDS:
                pipe.send(_hello_request(wire_id))
            pipe.close_stdin()
            lines, at_eof = pipe.read_to_eof(time.monotonic() + BATCH_TIMEOUT)
    except OSError as exc:
        _add(driver, weights, case, False,
             f"the probe could not be started for the ordering check: {exc}")
        return

    stream_order = _parse_ids(lines)
    want = [str(i) for i in ORDER_WIRE_IDS]
    passed = stream_order == want
    if passed:
        summary = f"{len(want)} responses arrived in request order"
    elif sorted(stream_order) == sorted(want):
        first_wrong = next(
            (i for i, (a, b) in enumerate(zip(stream_order, want)) if a != b), 0)
        summary = (f"all {len(want)} requests were answered but out of order: "
                   f"position {first_wrong} echoed id {stream_order[first_wrong]}, "
                   f"expected {want[first_wrong]}"
                   + (" -- the answers are in ascending id order, which the sent "
                      "order deliberately is not"
                      if stream_order == sorted(want, key=int) else ""))
    else:
        summary = (f"{len(stream_order)} of {len(want)} requests answered "
                   f"(order: {stream_order[:8]})")
    _add(driver, weights, case, passed, summary,
         detail=(f"sent ids: {want}\n"
                 f"answered, in stream order: {stream_order}\n"
                 f"reached EOF: {at_eof}\n"
                 f"stderr: {_tail_file(stderr_path)}"))


def _check_probe_batch_matches_serve(driver, weights: dict[str, float],
                                     prefix: list[str], cwd: Path,
                                     env: dict[str, str]) -> None:
    """The same bytes through `--batch <in> <out>` and through stdin.

    The probe publishes two invocations. Everything else in this suite drives one of
    them: fifteen corpus modules, the two relocation cases, the drain and ordering
    cases and the freeze all write NDJSON to a pipe and read stdout. So without this
    case the file mode is a published contract clause that nothing measures, and a
    port could omit it outright, or answer differently there, for free.

    Both streams are compared as *whole blobs*, byte for byte, not response by
    response. The framing is part of what the two modes have to agree on, and it is
    the part a file writer gets wrong: a port that accumulates its answers and writes
    them on exit -- entirely reasonable for a file, and what most rewrites of `batch`
    would produce -- loses its tail if it exits without flushing. Over a pipe that
    same bug is invisible, because the reader drains to EOF after the process ends and
    the OS has already delivered everything written. Parsing both sides into responses
    first would discard exactly the difference: a missing trailing newline, a short
    final line, two answers concatenated onto one line.

    Deliberately not compared against a frozen expectation. The subject is whether the
    two modes of *this* build agree; a port whose answers are all wrong is graded for
    that 13,940 times by the corpus, and charging it again here would report one defect
    twice. This is the same reasoning as the two relocation cases, and unlike those
    this one cannot share their baseline -- theirs is a `BatchOutcome`, which holds
    parsed responses and has already dropped the framing this case is about.

    A batch run is one process for all six requests. `_LivePipe` is not used: nothing
    is being sent incrementally and there is no stdin to close, so the serve side runs
    through `vlib.run` with the same bytes on stdin and the comparison stays between
    two blobs of stdout.
    """
    case = "struct/probe-batch-matches-serve"
    payload = b"".join(executor.encode_request(r) for r in COMPARE_REQUESTS)
    in_path = driver.work / "batch-in.ndjson"
    out_path = driver.work / "batch-out.ndjson"
    in_path.write_bytes(payload)
    if out_path.exists():
        # A previous run's output would be indistinguishable from a port that wrote
        # nothing, and "the file was already there" is the one way this case can pass
        # while measuring nothing at all.
        out_path.unlink()

    batch = vlib.run([*prefix, "--batch", str(in_path), str(out_path)],
                     cwd=cwd, env=env, timeout=BATCH_TIMEOUT, log=driver.log,
                     label="struct:batch")
    served = vlib.run(list(prefix), cwd=cwd, env=env, timeout=BATCH_TIMEOUT,
                      stdin_data=payload, log=driver.log, label="struct:serve",
                      full_capture=True)

    wrote = out_path.is_file()
    produced = out_path.read_bytes() if wrote else b""
    expected = served.stdout
    passed = (batch.ok and served.ok and wrote and produced == expected
              and bool(expected))

    if passed:
        summary = (f"`--batch` and stdin produced the same {len(produced)} bytes for "
                   f"{len(COMPARE_REQUESTS)} requests")
    elif not served.ok:
        # The serve side is the reference half of this differential, and every other
        # case in this module has already run the probe over a pipe -- so this is
        # reported as what it is rather than blamed on the file mode.
        summary = (f"the probe failed over stdin (exit {served.returncode}"
                   f"{', timed out' if served.timed_out else ''}), so there is "
                   f"nothing to compare the file mode against")
    elif not batch.ok:
        summary = (f"`--batch` exited {batch.returncode}"
                   f"{' after timing out' if batch.timed_out else ''} while stdin "
                   f"answered {len(expected)} bytes")
    elif not wrote:
        summary = ("`--batch` exited 0 without creating its output file")
    elif not produced:
        summary = (f"`--batch` wrote an empty file where stdin produced "
                   f"{len(expected)} bytes")
    elif produced.rstrip(b"\n") == expected.rstrip(b"\n"):
        # The measured failure mode of a rewritten `batch`: same answers, joined
        # instead of terminated. Reported as framing rather than as a wrong answer,
        # because a port told "line 6 differs" would go looking at `register`.
        file_newlines = len(produced) - len(produced.rstrip(b"\n"))
        pipe_newlines = len(expected) - len(expected.rstrip(b"\n"))
        answers = len(expected.rstrip(b"\n").splitlines())
        summary = (f"the two modes agree on all {answers} answers but not on their "
                   f"framing: the file ends with {file_newlines} newline(s) and "
                   f"stdout with {pipe_newlines}")
    else:
        summary = (f"`--batch` wrote {len(produced)} bytes where stdin produced "
                   f"{len(expected)}; the two modes answer the same requests "
                   f"differently")

    _add(driver, weights, case, passed, summary,
         detail=(f"requests: {len(COMPARE_REQUESTS)} ({len(payload)} bytes)\n"
                 f"--batch: exit {batch.returncode}, wrote {len(produced)} byte(s) "
                 f"in {len(produced.splitlines())} line(s)\n"
                 f"stdin:   exit {served.returncode}, wrote {len(expected)} byte(s) "
                 f"in {len(expected.splitlines())} line(s)\n"
                 f"first differing line: {_first_line_diff(produced, expected)}\n"
                 f"--batch stderr: "
                 f"{batch.stderr.decode('utf-8', 'replace')[:400] or '<empty>'}\n"
                 f"stdin stderr: "
                 f"{served.stderr.decode('utf-8', 'replace')[:400] or '<empty>'}"))


def _first_line_diff(produced: bytes, expected: bytes) -> str:
    """Where the two blobs first disagree, by line, for the report.

    Truncated per side, because a divergent answer to `ast` is a whole syntax tree and
    the finding is the position, not the payload.
    """
    def show(blob: bytes | None) -> str:
        if blob is None:
            return "<absent>"
        return blob.decode("utf-8", "replace")[:200] or "<empty line>"

    if produced == expected:
        return "<none>"
    got, want = produced.split(b"\n"), expected.split(b"\n")
    for index in range(max(len(got), len(want))):
        mine = got[index] if index < len(got) else None
        theirs = want[index] if index < len(want) else None
        if mine != theirs:
            return f"line {index + 1}: --batch {show(mine)} / stdin {show(theirs)}"
    return "<none>"


def _answers(prefix: list[str], cwd: Path, env: dict[str, str], driver,
             label: str) -> executor.BatchOutcome:
    """`COMPARE_REQUESTS` through one probe invocation."""
    return executor.ProbeRunner(prefix, cwd=cwd, log=driver.log, env=env,
                                label=label).run_all(list(COMPARE_REQUESTS))


def _compare_to_baseline(baseline: executor.BatchOutcome,
                         moved: executor.BatchOutcome) -> tuple[list[int], int]:
    """Positions where the moved run disagrees with the baseline, and how many
    positions the baseline itself answered.

    Compared to the baseline rather than to a frozen expectation on purpose: the
    subject is whether *relocating* changed the answer. A port whose answers are
    wrong everywhere has that graded 13,940 times by the corpus, and charging it
    again here would turn one defect into two findings.
    """
    answered = [i for i, resp in enumerate(baseline.ordered) if resp is not None]
    differing = [i for i in answered
                 if moved.ordered[i] is None
                 or moved.ordered[i].raw != baseline.ordered[i].raw]
    return differing, len(answered)


def _check_probe_standalone(driver, weights: dict[str, float], prefix: list[str],
                            cwd: Path, env: dict[str, str],
                            baseline: executor.BatchOutcome) -> None:
    """The same probe, invoked from a working directory that is not the build tree.

    `instruction.md` publishes that `node dist/probe.js` speaks the protocol wherever
    it is run from. The corpus never tests it: `driver.probe_prefix()` hands every
    module the same `cwd`, the build tree, so a port that resolves a data file as
    `./src/datetime-tables.json` or reads `package.json` from `process.cwd()` answers
    all 13,940 cases and fails the first time anything invokes it normally.

    Nothing is copied and nothing is deleted here -- same argv, same build, different
    cwd -- which is what separates this from `dist-self-contained` below: this one
    catches cwd-relative resolution, that one catches resolution relative to the
    probe's own location that reaches outside `dist/`. A port can fail either without
    the other.
    """
    case = "struct/probe-standalone"
    elsewhere = driver.work / "elsewhere"
    elsewhere.mkdir(parents=True, exist_ok=True)
    if not Path(prefix[-1]).is_absolute():
        # The ledger records an absolute path to the probe, so this is a
        # can't-happen; if it ever becomes relative, changing the cwd would fail to
        # find the probe at all and the case would read as a broken port.
        _add(driver, weights, case, False,
             "the build ledger recorded a relative path to the probe, so it cannot "
             "be invoked from another directory",
             detail=f"argv prefix: {prefix}")
        return

    moved = _answers(prefix, elsewhere, env, driver, "struct:elsewhere")
    differing, answered = _compare_to_baseline(baseline, moved)
    passed = answered > 0 and not differing
    if not answered:
        summary = (f"the probe answered none of the {len(COMPARE_REQUESTS)} requests "
                   f"in its own build tree, so relocating it measures nothing")
    elif passed:
        summary = (f"all {answered} responses are byte-identical when the probe is "
                   f"run from an unrelated working directory")
    else:
        summary = (f"{len(differing)} of {answered} responses changed when the probe "
                   f"was run from an unrelated working directory")
    _add(driver, weights, case, passed, summary,
         detail=(f"cwd used: {elsewhere} (empty; the build tree is untouched)\n"
                 f"argv: {prefix}\n"
                 f"changed positions: {differing[:20]}\n"
                 + (f"stderr: {moved.stderr_tail[:800]}"
                    if moved.stderr_tail else "")))


def _check_dist_self_contained(driver, weights: dict[str, float],
                               prefix: list[str], env: dict[str, str],
                               baseline: executor.BatchOutcome,
                               ledger: dict) -> None:
    """`dist/` alone in an empty directory, with the tree it was built in deleted.

    The published rule is that `dist/` is the delivered surface. This measures it: put
    `dist/` somewhere with nothing beside it, remove the tree it came from, and
    require byte-identical answers.

    This is the case with teeth for a TypeScript port, and the reason it survived the
    cull that removed nine file-shaped assertions. `tsc` emits one output file per
    input file, and an import written `../src/utils` -- or a `require` a hand-rolled
    bundler left pointing at the source it was built from -- resolves perfectly well
    while `src/` is still there. Every module in this suite runs the probe inside the
    build tree, so every one of them would see that work. A consumer installing the
    package would not.

    Distinct from `probe-standalone`: that one changes the cwd and leaves the tree
    standing, so it catches `process.cwd()`-relative resolution. A path resolved from
    `__dirname` moves with `__dirname` and survives a cwd change; it does not survive
    its siblings being gone.

    The tree that gets deleted is a *copy*. The shared build tree in `$SRB_SUITE_WORK`
    belongs to `build` and is read by thirteen other modules that may run after this
    one; deleting it would turn one finding here into thirteen modules reporting a
    missing artifact.
    """
    case = "struct/dist-self-contained"
    scratch = driver.work / "selfcontained"
    if scratch.exists():
        shutil.rmtree(scratch)
    tree_copy = scratch / "tree"
    solo = scratch / "solo"
    build_dir = Path(ledger["build_dir"])
    dist = build_dir / "dist"
    if not dist.is_dir():
        _add(driver, weights, case, False,
             "the build recorded no dist/ directory to isolate",
             detail="`build/build-artifact` reports the same absence as a "
                    "required check.")
        return

    # A copy of the whole build tree, so the deletion below is this module's own.
    shutil.copytree(build_dir, tree_copy, symlinks=True)
    solo.mkdir(parents=True, exist_ok=True)
    shutil.copytree(tree_copy / "dist", solo / "dist", symlinks=True)
    shutil.rmtree(tree_copy)

    solo_probe = solo / "dist" / Path(ledger["probe_js"]).name
    if not solo_probe.is_file():
        _add(driver, weights, case, False,
             f"{solo_probe.name} did not survive being copied on its own",
             detail=f"copied {dist} to {solo / 'dist'}")
        return

    moved = _answers([prefix[0], str(solo_probe)], solo, env, driver,
                     "struct:selfcontained")
    differing, answered = _compare_to_baseline(baseline, moved)
    passed = answered > 0 and not differing
    if not answered:
        summary = (f"the probe answered none of the {len(COMPARE_REQUESTS)} requests "
                   f"in its own build tree, so isolating dist/ measures nothing")
    elif passed:
        summary = (f"all {answered} responses are byte-identical with dist/ alone in "
                   f"an empty directory and the tree it was built in deleted")
    else:
        summary = (f"{len(differing)} of {answered} responses changed when dist/ was "
                   f"separated from the tree it was built in")
    _add(driver, weights, case, passed, summary,
         detail=(f"isolated at: {solo / 'dist'}\n"
                 f"the copied build tree was deleted before the re-run; the shared "
                 f"build tree in $SRB_SUITE_WORK is untouched, because fourteen "
                 f"other modules read it\n"
                 f"changed positions: {differing[:20]}\n"
                 + (f"stderr: {moved.stderr_tail[:800]}"
                    if moved.stderr_tail else "")))
    driver.metadata["self_contained"] = {
        "isolated_dist": str(solo / "dist"),
        "requests": len(COMPARE_REQUESTS),
        "differing": len(differing),
    }


def _library_entry(build_dir: Path) -> tuple[Path | None, str]:
    """Where a consumer's `require` would land, and how that was decided.

    Node's own algorithm for a directory: the manifest's `main`, and `index.js` when
    there is none. Nothing else is tried, because a fallback this file invented would
    let a port that wired its manifest wrongly still be found -- and being found is
    half of what `package-main-resolves` grades.

    The manifest is read to know *what to run*, not to grade what it says. No case
    asserts the value of `main`; two cases run whatever it points at and grade how
    that behaves. A port free to name its entry point `dist/index.js` stays free to.
    """
    manifest = build_dir / "package.json"
    if manifest.is_file():
        try:
            declared = json.loads(manifest.read_text(encoding="utf-8", errors="replace"))
        except ValueError as exc:
            return None, f"package.json is not valid JSON ({exc})"
        main = declared.get("main") if isinstance(declared, dict) else None
        if isinstance(main, str) and main.strip():
            candidate = (build_dir / main).resolve()
            if candidate.is_file():
                return candidate, f"package.json main = {main!r}"
            # `require` also accepts an extensionless main, so try what node tries.
            for suffix in (".js", ".cjs", ".json"):
                with_suffix = Path(str(candidate) + suffix)
                if with_suffix.is_file():
                    return with_suffix, f"package.json main = {main!r} (+{suffix})"
            if candidate.is_dir() and (candidate / "index.js").is_file():
                return candidate / "index.js", f"package.json main = {main!r} (dir)"
            return None, (f"package.json declares main = {main!r}, and nothing is "
                          f"there")
    fallback = build_dir / "index.js"
    if fallback.is_file():
        return fallback, "no main declared; node's fallback index.js"
    return None, ("no main declared and no index.js, so a consumer's require has "
                  "nothing to resolve to")


def _run_consumer(driver, env: dict[str, str], node: str, target: str,
                  label: str) -> tuple[dict, vlib.Result]:
    """The consumer script against one require target. Parsed payload, raw result."""
    script = driver.work / "consumer.js"
    if not script.is_file():
        script.write_text(CONSUMER_JS, encoding="utf-8")
    result = vlib.run([node, str(script), target], cwd=driver.work, env=env,
                      timeout=CONSUMER_TIMEOUT, log=driver.log, label=label)
    payload: dict = {}
    first = result.stdout.strip().split(b"\n")[0] if result.stdout.strip() else b""
    if first:
        try:
            parsed = json.loads(first)
            if isinstance(parsed, dict):
                payload = parsed
        except ValueError:
            pass
    return payload, result


def _consumer_detail(target: str, how: str, payload: dict,
                     result: vlib.Result) -> str:
    return (f"require target: {target}\n"
            f"chosen because: {how}\n"
            f"consumer stage: {payload.get('stage', '<no JSON on stdout>')}\n"
            f"export type: {payload.get('exportType', '<unknown>')}\n"
            f"methods found: {payload.get('methods', '<none>')}\n"
            f"value: {payload.get('value', '<none>')!r} "
            f"(expected {CONSUMER_EXPECTED})\n"
            + (f"error: {payload['error']}\n" if payload.get("error") else "")
            + f"exit {result.returncode}; stderr: {result.tail(lines=6)}")


def _check_library_requirable(driver, weights: dict[str, float], node: str,
                              env: dict[str, str], build_dir: Path) -> None:
    """The built library loads, compiles an expression and evaluates it -- no probe.

    The probe is this suite's instrument, not the repository's product. Every other
    module in the suite reaches the library *through* it, so a port that wired its
    parser and evaluator into `dist/probe.js` and left `dist/jsonata.js` broken, or
    exporting the wrong shape, scores 13,940 out of 13,940 and is not a JSONata
    library.

    What is graded is behaviour on the public surface: the export is callable, calling
    it yields an object with the five published methods, and awaiting `evaluate`
    resolves to the right number. `evaluate` is the only one called -- the other four
    are exercised for real by the corpus through the probe's `ast`, `assign` and
    `register` ops, so calling them here would charge twice for what is already
    measured; that they are *present on the object a consumer receives* is what the
    corpus cannot see.
    """
    case = "struct/library-requirable"
    entry, how = _library_entry(build_dir)
    if entry is None:
        _add(driver, weights, case, False,
             f"there is no library file to require: {how}",
             detail=f"looked in {build_dir}")
        return

    payload, result = _run_consumer(driver, env, node, str(entry),
                                    "struct:library")
    missing = [m for m in CONSUMER_METHODS if m not in payload.get("methods", [])]
    passed = bool(payload.get("ok")) and not missing
    if passed:
        summary = (f"the built library required by path is callable and evaluated an "
                   f"expression to {CONSUMER_EXPECTED} without the probe")
    elif payload.get("stage") == "require":
        summary = "the built library could not be required at all"
    elif payload.get("stage") == "export":
        summary = (f"the built library exports a {payload.get('exportType')}, not a "
                   f"callable")
    elif payload.get("stage") in ("compile", "evaluate", "await"):
        summary = (f"the library loaded but failed at {payload.get('stage')}: "
                   f"{str(payload.get('error', ''))[:160]}")
    elif missing:
        summary = (f"the expression object is missing {len(missing)} published "
                   f"method(s): {missing}")
    elif not payload:
        summary = "the consumer produced no JSON on stdout"
    else:
        summary = (f"the library evaluated the expression to "
                   f"{payload.get('value')!r}, not {CONSUMER_EXPECTED}")
    _add(driver, weights, case, passed, summary,
         detail=_consumer_detail(str(entry), how, payload, result))


def _check_package_main_resolves(driver, weights: dict[str, float], node: str,
                                 env: dict[str, str], build_dir: Path) -> None:
    """`require('<the package directory>')` gives the library.

    This is the resolution half, and the way a consumer actually gets the library:
    `require('jsonata')` after an install is node resolving a directory, which means
    reading the manifest and loading what it names. A port whose manifest still names
    a file the build no longer produces -- an entry point renamed during the port, a
    `main` left pointing at a path that only existed while the sources were
    JavaScript -- installs into a consumer's tree and throws on import, while every
    module in this suite that runs `node dist/probe.js` by absolute path is perfectly
    happy.

    Correlated with `library-requirable` in one case and not the rest: a manifest that
    names no entry point at all fails both, because there is then neither a library to
    require nor a package to resolve. Every other failure mode separates them -- a
    working file behind a stale `main` fails only this one, and a resolvable `main`
    whose module exports the wrong shape fails only that one. The two summaries say
    which happened.
    """
    case = "struct/package-main-resolves"
    payload, result = _run_consumer(driver, env, node, str(build_dir),
                                    "struct:package")
    passed = bool(payload.get("ok"))
    if passed:
        summary = ("requiring the package directory resolved to the library and "
                   "evaluated an expression")
    elif payload.get("stage") == "require":
        summary = ("requiring the package directory failed to resolve to a loadable "
                   "module")
    elif payload.get("stage") == "export":
        summary = (f"the package directory resolved to a "
                   f"{payload.get('exportType')}, not a callable library")
    elif not payload:
        summary = "the consumer produced no JSON on stdout"
    else:
        summary = (f"the package resolved but failed at {payload.get('stage')}: "
                   f"{str(payload.get('error', ''))[:160]}")
    _add(driver, weights, case, passed, summary,
         detail=_consumer_detail(str(build_dir), "the package directory, the way a "
                                 "consumer's require reaches it", payload, result))


def _check_build_idempotent(driver, weights: dict[str, float],
                            ledger: dict) -> None:
    """Building twice from the same tree produces byte-identical `dist/`.

    Measured by `build`, graded here. `build` deletes `dist/`, runs the build again,
    compares a digest per emitted file and restores the first build's output; doing
    the second build there rather than here is what keeps the suite to two builds
    instead of three, and `build` holds no points of its own.

    Worth a case because this suite compares built artifacts across relocations, and
    stage 3 compares two builds of two different trees. A build that embeds a
    timestamp, or that emits in `readdir` order, makes every one of those comparisons
    depend on when and where it ran -- and the failure surfaces as an unrelated case
    disagreeing rather than as the build being unreproducible. Graded once, here,
    where the report can say which files changed.

    Not a style rule and not free: the reference's build script sorts its module graph
    and writes no timestamps, and a port that reaches for a bundler with a default
    banner, or that emits a `.d.ts` from a `Map` it built by walking the filesystem,
    fails this without doing anything else wrong.
    """
    case = "struct/build-idempotent"
    if "rebuild_deterministic" not in ledger:
        _add(driver, weights, case, False,
             "the build module recorded no second build, so idempotence was never "
             "measured",
             detail="`build` writes `rebuild_deterministic` into the ledger; its "
                    "absence means the build did not reach that point, which its "
                    "own required checks report.")
        return
    deterministic = bool(ledger.get("rebuild_deterministic"))
    changed = ledger.get("rebuild_changed") or []
    if deterministic:
        summary = (f"two builds from the same tree emitted byte-identical output "
                   f"({ledger.get('rebuild_files', '?')} file(s))")
    elif changed:
        summary = (f"{len(changed)} emitted file(s) differed between two builds of "
                   f"the same tree: {changed[:6]}")
    else:
        summary = "the second build of the same tree did not reproduce the first"
    _add(driver, weights, case, deterministic, summary,
         detail=(f"measured by the `build` module, which builds twice and restores "
                 f"the first output\n"
                 f"files compared: {ledger.get('rebuild_files', '?')}\n"
                 f"changed: {changed[:20]}\n"
                 f"{ledger.get('rebuild_detail', '')}").strip())


# --------------------------------------------------------------------------- #
# dispatch and coverage
# --------------------------------------------------------------------------- #
#
# The handlers, keyed by the `check` name in `catalog.STRUCT_CASES`. A dict of names
# rather than a chain of `if`s so `check_release_coverage` can compare the two tables
# without executing anything.

HANDLERS: dict[str, str] = {
    "probe-drains-stdin": "_check_probe_drains_stdin",
    "probe-answers-in-order": "_check_probe_answers_in_order",
    "probe-batch-matches-serve": "_check_probe_batch_matches_serve",
    "probe-standalone": "_check_probe_standalone",
    "library-requirable": "_check_library_requirable",
    "dist-self-contained": "_check_dist_self_contained",
    "package-main-resolves": "_check_package_main_resolves",
    "build-idempotent": "_check_build_idempotent",
}

# What each case needs to be able to run. Not decoration: `run()` reads these to
# decide the order, and `check_release_coverage` requires every case to appear in
# exactly one of the three, so a handler added later cannot quietly end up in none.
#
#   NEEDS_PROBE   -- spawns `dist/probe.js`.
#   NEEDS_LIBRARY -- spawns node against the built library, never the probe.
#   the rest      -- read the build ledger only.
NEEDS_PROBE = frozenset({
    "probe-drains-stdin", "probe-answers-in-order", "probe-batch-matches-serve",
    "probe-standalone", "dist-self-contained",
})
NEEDS_LIBRARY = frozenset({
    "library-requirable", "package-main-resolves",
})

# The third rule for being in `STRUCT_CASES` is that the requirement is *published*:
# grading a rule the submission was never told is unfair however reasonable the rule
# is.  That was a claim in a comment, and a comment about another file is worth
# nothing until something compares them -- so each case cites the sentence in
# `instruction.md` that states it, and `check-task.py`'s `structure` group requires
# every one of these to appear there verbatim, modulo where the prose wraps.
#
# This file cannot do that check itself: `instruction.md` sits outside all three
# Docker build contexts, so the grading image has never seen it.  Hence the split --
# the table lives here, beside the handlers, and the host-side checker reads it.
STATED_IN_INSTRUCTION: dict[str, str] = {
    "probe-drains-stdin":
        "one JSON response per line on stdout, in order, until stdin reaches EOF, "
        "then exit 0",
    "probe-answers-in-order":
        "in order",
    "probe-batch-matches-serve":
        "`--batch <in> <out>` must write to the output file exactly what the same "
        "requests on stdin write to stdout",
    "probe-standalone":
        "must speak the protocol whatever the working directory is",
    "library-requirable":
        "the library has to work when it is required directly, with no probe "
        "involved",
    "dist-self-contained":
        "`dist/` is the delivered surface: copied anywhere on its own, it still "
        "works",
    "package-main-resolves":
        "requiring the package directory has to give the library",
    "build-idempotent":
        "two builds of the same tree must emit byte-identical output",
}


def check_release_coverage() -> list[str]:
    """Assert that `STRUCT_CASES` and `HANDLERS` name the same eight checks.

    A table of case ids is a *claim* that each one is measured, and a claim about
    another file is worth exactly nothing until something compares them -- so this
    compares them, and the Dockerfile runs it at image build time.

    Called as a script (`python3 lib/structure.py --check`) rather than imported by
    `catalog.py`: catalog runs `_self_check()` at import and is itself executed as
    `__main__` by the Dockerfile, so a `catalog` -> `structure` -> `catalog` import
    would run that self-check twice and make a circular import out of a one-way
    assertion.

    Returns the problems it found, so a caller can report all of them rather than the
    first.
    """
    problems: list[str] = []
    table = {check for check, _w, _note in catalog.STRUCT_CASES}
    handled = set(HANDLERS)

    for missing in sorted(table - handled):
        problems.append(
            f"catalog.STRUCT_CASES names the check {missing!r}, which "
            f"structure.HANDLERS does not implement: the case would be budgeted and "
            f"never measured")
    for extra in sorted(handled - table):
        problems.append(
            f"structure.HANDLERS implements {extra!r}, which is not in "
            f"catalog.STRUCT_CASES: it would run unweighted or not at all")

    for check, name in sorted(HANDLERS.items()):
        if not callable(globals().get(name)):
            problems.append(
                f"HANDLERS maps {check!r} to {name!r}, which is not a function in "
                f"this module")

    # Case ids and weights have to line up with what `release_weights` produces,
    # since that is the dict the handlers look themselves up in.
    weights = catalog.release_weights()
    for check, _w, _note in catalog.STRUCT_CASES:
        if f"struct/{check}" not in weights:
            problems.append(
                f"'struct/{check}' has no entry in catalog.release_weights()")
    total = sum(weights.values())
    if abs(total - catalog.RELEASE_BUDGET) > 1e-9:
        problems.append(
            f"the release case weights sum to {total}, not RELEASE_BUDGET="
            f"{catalog.RELEASE_BUDGET}")

    # Every case in exactly one capability set, so a handler added without a decision
    # about what it needs is a failure here rather than a case that runs before the
    # thing it depends on exists.
    for check in sorted(NEEDS_PROBE | NEEDS_LIBRARY):
        if check not in handled:
            problems.append(
                f"the capability sets name {check!r}, which is not a handler")
    both = NEEDS_PROBE & NEEDS_LIBRARY
    if both:
        problems.append(
            f"{sorted(both)} are in both NEEDS_PROBE and NEEDS_LIBRARY, so `run()` "
            f"would have no single answer for what they need")

    for missing in sorted(table - set(STATED_IN_INSTRUCTION)):
        problems.append(
            f"{missing!r} has no entry in STATED_IN_INSTRUCTION, so the third rule "
            f"for being in this table -- that the requirement is published -- is "
            f"unevidenced for it")
    for extra in sorted(set(STATED_IN_INSTRUCTION) - table):
        problems.append(
            f"STATED_IN_INSTRUCTION cites a sentence for {extra!r}, which is not a "
            f"case in catalog.STRUCT_CASES")
    return problems


def run(driver) -> int:
    """The whole module: eight release cases and nothing else.

    No corpus slice. `_release` is the only family this module owns and it is in
    `catalog.NON_CORPUS_FAMILIES` -- there are no `_release` cases in any
    `*-cases.jsonl` and never will be, because each one is computed by running the
    delivered tree. The `_protocol` family, which reads like it might belong here,
    belongs to `api`: those cases grade the request/response envelope that carries
    every op, which is an API contract, while this module grades the artifact as an
    artifact.

    The baseline for the two relocation cases is run once and shared. Both ask the
    same question -- did moving this change the answer -- and re-running the reference
    side twice would double the probe spawns to say the same thing.
    """
    weights = _weights()

    prefix, cwd, ledger = driver.probe_prefix()
    env = vlib.base_env()
    node = prefix[0]
    build_dir = Path(ledger["build_dir"])
    driver.log.section("release contract")

    _check_probe_drains_stdin(driver, weights, prefix, cwd, env)
    _check_probe_answers_in_order(driver, weights, prefix, cwd, env)
    _check_probe_batch_matches_serve(driver, weights, prefix, cwd, env)

    baseline = _answers(prefix, cwd, env, driver, "struct:baseline")
    _check_probe_standalone(driver, weights, prefix, cwd, env, baseline)
    _check_dist_self_contained(driver, weights, prefix, env, baseline, ledger)

    _check_library_requirable(driver, weights, node, env, build_dir)
    _check_package_main_resolves(driver, weights, node, env, build_dir)
    _check_build_idempotent(driver, weights, ledger)

    release_ids = set(weights)
    release_checks = [c for c in driver.checks if c["id"] in release_ids]
    release_passed = sum(1 for c in release_checks if c["verdict"] == "pass")
    if len(release_checks) != len(catalog.STRUCT_CASES):
        # Every case emits exactly once, whatever happened. A short count means a
        # handler returned without recording, which would shrink the module's
        # denominator and read as a smaller module rather than a missing check.
        raise AssertionError(
            f"structure: {len(release_checks)} release checks recorded, expected "
            f"{len(catalog.STRUCT_CASES)}: "
            f"{sorted(release_ids - {c['id'] for c in release_checks})}")

    driver.metadata["release"] = {
        "cases": len(release_checks),
        "passed": release_passed,
        "budget": catalog.RELEASE_BUDGET,
    }
    return driver.emit(
        "ok", f"{release_passed}/{len(release_checks)} release cases")


def main(argv: list[str]) -> int:
    if "--check" not in argv:
        print("structure: nothing to do; --check asserts the case table against the "
              "handlers. The module itself runs through driver.py.")
        return 0
    problems = check_release_coverage()
    if problems:
        print(f"structure: {len(problems)} coverage problem(s):")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print(f"structure: {len(catalog.STRUCT_CASES)} release case(s), each with a "
          f"handler; weights sum to {catalog.RELEASE_BUDGET:g}")
    return 0


if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main(sys.argv[1:]))
