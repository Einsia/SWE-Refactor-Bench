#!/usr/bin/env python3
"""Runs the NDJSON differential: the submission's acorn-probe against acorn 8.14.0.

Three sources of truth, in decreasing order of how much the submission could
have anticipated them:

  frozen    expectations computed at image build time from the pinned JavaScript
            reference, over a corpus with a fixed seed.  The bulk of the grade.
  fresh     a corpus generated at grading time from a seed the submission has
            never seen, answered by the reference in the same container.  A
            submission tuned to the frozen set scores here only by being right.
  fixtures  the six real-world bundles, compared by digest because one of their
            responses is 13 MB of JSON.

The comparison is over raw bytes.  Not parsed JSON, not normalised JSON --
`JSON.stringify` property order is part of the contract, and re-serialising
either side would hide exactly the failure this task is about.
"""

from __future__ import annotations

import hashlib
import json
import os
import select
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import vlib
from vlib import CaseOutcome, Log

# One request must not be able to hang the whole grading pass.  A fixture parse
# of ember.js is the slowest legitimate case; a native parser should be well
# under a second, and the reference itself takes ~300 ms per fixture op.
# This is enforced, not advisory: a probe that consumes a request and answers
# nothing would otherwise block until the harness killed the container, and the
# trial would be recorded as infrastructure failure rather than as the zero it
# has earned.
PER_REQUEST_TIMEOUT = 120.0
# The probe is a long-lived process fed thousands of lines.  This bounds the
# whole conversation, not one line.
SESSION_TIMEOUT = 2400.0
# The largest legitimate response is ember.js under parse_collect: 56 MB.  Twice
# that is room for a submission that is wrong about sizes but still trying.
MAX_RESPONSE_BYTES = 192 * 1024 * 1024
# Enough stderr to diagnose a crash, not enough to matter.  It is drained
# continuously so a chatty probe cannot deadlock on a full stderr pipe and be
# misdiagnosed as one that stopped answering.
MAX_STDERR_BYTES = 64 * 1024
# Wall-clock ceiling for one corpus.  The reference answers all 15,975 requests
# in 1.3 s; a native implementation two orders of magnitude slower still fits in
# a couple of minutes.  The ceiling exists for the pathological case -- a probe
# that hangs on every request would otherwise spend 40 restarts x the per-request
# timeout, which is most of the verifier's budget, and the trial would time out
# instead of scoring.  Past the ceiling the remaining cases are marked failed,
# which is the same verdict reached more slowly.
CORPUS_TIME_BUDGET = 1500.0
# Restarts are also capped: a probe dying this often is not going to recover, and
# every restart re-pays process startup.
MAX_RESTARTS = 25


class ProbeDied(RuntimeError):
    """The probe exited, or stopped answering, mid-conversation."""


@dataclass
class Session:
    """A live acorn-probe process being fed one request at a time.

    The protocol says the probe must flush each response before reading the next
    request, and this class holds it to that: it writes one line, then blocks
    for one line back.  A probe that buffers its output deadlocks here, which is
    the correct outcome -- the instruction is explicit that it must not.
    """

    argv: list[str]
    cwd: Path
    env: dict
    proc: subprocess.Popen | None = None
    sent: int = 0
    started: float = 0.0
    # Bytes read past the end of the last response line.  A probe is free to
    # write its next response before we ask for it; what it may not do is
    # withhold the current one.
    tail: bytes = b""
    stderr_seen: bytes = b""

    def start(self) -> None:
        self.proc = subprocess.Popen(
            self.argv,
            cwd=str(self.cwd),
            env=self.env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.sent = 0
        self.started = time.monotonic()
        self.tail = b""

    def ask(self, request_line: bytes) -> bytes:
        """Send one request, return one response line.

        Both halves are on a deadline.  Writing can block too: a fixture request
        carries a 1.8 MB source, far more than a pipe will hold, so a probe that
        does not read blocks us in `write` rather than in `read`.
        """
        if self.proc is None or self.proc.poll() is not None:
            code = None if self.proc is None else self.proc.returncode
            raise ProbeDied(f"probe is not running (exit {code}) after {self.sent} requests")
        if time.monotonic() - self.started > SESSION_TIMEOUT:
            raise ProbeDied(f"probe session exceeded {SESSION_TIMEOUT}s after {self.sent} requests")
        deadline = time.monotonic() + PER_REQUEST_TIMEOUT
        self._write_all(request_line + b"\n", deadline)
        self.sent += 1
        return self._read_line(deadline)

    def _write_all(self, payload: bytes, deadline: float) -> None:
        fd = self.proc.stdin.fileno()
        view = memoryview(payload)
        while view:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProbeDied(
                    f"probe stopped reading its stdin during request {self.sent + 1} "
                    f"({len(payload) - len(view)} of {len(payload)} bytes accepted "
                    f"in {PER_REQUEST_TIMEOUT:.0f}s)"
                )
            try:
                ready = select.select([], [fd], [], min(remaining, 1.0))[1]
            except OSError as exc:
                raise ProbeDied(f"probe stdin is unusable: {exc}") from exc
            if not ready:
                if self.proc.poll() is not None:
                    raise ProbeDied(
                        f"probe exited {self.proc.returncode} while request "
                        f"{self.sent + 1} was being written"
                    )
                continue
            try:
                written = os.write(fd, view[: 1 << 20])
            except BrokenPipeError:
                raise ProbeDied(
                    f"probe closed its stdin after {self.sent} requests; it must "
                    f"read until EOF"
                ) from None
            except OSError as exc:
                raise ProbeDied(f"writing request {self.sent + 1} failed: {exc}") from exc
            view = view[written:]

    def _read_line(self, deadline: float) -> bytes:
        """One newline-terminated response, or ProbeDied.

        Buffered by hand rather than via `readline` because a blocking readline
        on a live-but-silent child cannot be interrupted, and "silent" is a
        failure mode this task's protocol specifically forbids.
        """
        out_fd = self.proc.stdout.fileno()
        err_fd = self.proc.stderr.fileno() if self.proc.stderr else None
        while True:
            nl = self.tail.find(b"\n")
            if nl >= 0:
                line, self.tail = self.tail[:nl], self.tail[nl + 1:]
                return line
            if len(self.tail) > MAX_RESPONSE_BYTES:
                raise ProbeDied(
                    f"response to request {self.sent} passed "
                    f"{MAX_RESPONSE_BYTES} bytes with no newline"
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                alive = self.proc.poll() is None
                raise ProbeDied(
                    f"probe did not answer request {self.sent} within "
                    f"{PER_REQUEST_TIMEOUT:.0f}s ({'still running' if alive else 'exited'}, "
                    f"{len(self.tail)} bytes of a partial response); every request "
                    f"must get exactly one response line, flushed before the next "
                    f"request is read"
                )
            watch = [out_fd] + ([err_fd] if err_fd is not None else [])
            try:
                ready = select.select(watch, [], [], min(remaining, 1.0))[0]
            except OSError as exc:
                raise ProbeDied(f"probe stdout is unusable: {exc}") from exc
            if err_fd is not None and err_fd in ready:
                self._drain_stderr(err_fd)
            if out_fd not in ready:
                continue
            chunk = os.read(out_fd, 1 << 20)
            if not chunk:
                code = self.proc.poll()
                note = self._stderr_note()
                if self.tail:
                    raise ProbeDied(
                        f"probe exited ({code!r}) mid-response to request {self.sent}; "
                        f"{len(self.tail)} bytes without a terminating newline{note}"
                    )
                raise ProbeDied(
                    f"probe produced no response to request {self.sent} "
                    f"(exit {code!r}); it must answer every request and exit "
                    f"only on EOF{note}"
                )
            self.tail += chunk

    def _drain_stderr(self, err_fd: int) -> None:
        """Keep the stderr pipe from filling.

        A probe blocked writing diagnostics into a 64 KB pipe looks exactly like
        one that hung, and would be reported as the wrong failure.
        """
        try:
            chunk = os.read(err_fd, 1 << 16)
        except OSError:
            return
        if chunk and len(self.stderr_seen) < MAX_STDERR_BYTES:
            self.stderr_seen += chunk[: MAX_STDERR_BYTES - len(self.stderr_seen)]

    def _stderr_note(self) -> str:
        if not self.stderr_seen:
            return ""
        text = self.stderr_seen.decode("utf-8", "replace").strip()
        return "; stderr: " + " ".join(text.split())[-400:]

    def finish(self) -> tuple[int | None, bytes]:
        """Close stdin and collect the exit status: the probe must exit 0 on EOF."""
        if self.proc is None:
            return None, b""
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        # Drain both pipes while waiting.  Waiting first and reading after would
        # deadlock against a probe that exits only once its output is consumed.
        deadline = time.monotonic() + 60.0
        err_fd = self.proc.stderr.fileno() if self.proc.stderr else None
        out_fd = self.proc.stdout.fileno()
        while self.proc.poll() is None and time.monotonic() < deadline:
            watch = [out_fd] + ([err_fd] if err_fd is not None else [])
            try:
                ready = select.select(watch, [], [], 0.5)[0]
            except OSError:
                break
            for fd in ready:
                try:
                    chunk = os.read(fd, 1 << 16)
                except OSError:
                    continue
                if fd == err_fd and chunk and len(self.stderr_seen) < MAX_STDERR_BYTES:
                    self.stderr_seen += chunk[: MAX_STDERR_BYTES - len(self.stderr_seen)]
        if self.proc.poll() is None:
            self.proc.kill()
            try:
                self.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                pass
            return None, b"probe did not exit within 60s of EOF"
        return self.proc.returncode, self.stderr_seen[:MAX_STDERR_BYTES]

    def kill(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            try:
                self.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                pass


def _watchdog_env(env: dict) -> dict:
    """The environment the submission's probe runs in.

    No `node` on PATH, and the reference tree is not readable from here by any
    relative path -- the probe is started with a cwd outside the repository, as
    the instruction says it will be.
    """
    out = dict(env)
    out["ACORN_PROBE_UNDER_TEST"] = "1"
    return out


class Runner:
    """Feeds a corpus through one probe and grades the answers."""

    def __init__(self, binary, *, log: Log, cwd: Path, env: dict) -> None:
        # A list is accepted as well as a path so the reference can be graded
        # against itself (`node reference.js`) as an identity check.  If that
        # does not score 1.0, the harness is broken, not the submission.
        self.argv = [str(x) for x in binary] if isinstance(binary, (list, tuple)) \
            else [str(binary)]
        self.log = log
        self.cwd = cwd
        self.env = _watchdog_env(env)
        self.crashes: list[dict] = []
        self.restarts = 0

    def _new_session(self) -> Session:
        session = Session(argv=list(self.argv), cwd=self.cwd, env=self.env)
        session.start()
        return session

    def run_corpus(
        self,
        requests_path: Path,
        expected_path: Path,
        cases: dict[int, dict],
        *,
        label: str,
        weights: dict[str, float],
        corpus_lines: int,
        excluded: dict[int, str],
        default_weight: float = 1.0,
    ) -> list[CaseOutcome]:
        """Grade one module's slice of the corpus, line for line.

        Both files are read as streams and never held in memory: the fixture
        expectations alone are hundreds of megabytes.

        `cases` is the slice, not a metadata table.  One frozen request file
        holds the corpus for all nine differential modules, so a line this
        module does not own is skipped -- its expectation is still read, to keep
        the two files in lockstep, but it is neither asked nor scored.  Grading
        it here instead would score every case nine times over, at the fallback
        family and weight, which flattens the module partition the suite's
        weights are built on.

        `corpus_lines` is how many cases the corpus declares (`cases.json`'s
        `count`), which is what the whole file's non-blank lines must join
        against -- not a line count of the file on disk.

        `excluded` maps case id to reason for the cases whose frozen answer has
        no portable form -- see freeze.check_unportable.  They are in the slice
        and leave an outcome like any other, but the probe is never asked: the
        answer being compared against is a crash inside the reference, and the
        message the schema requires is V8's wording for the code shape that
        crashed.  Not asked rather than not present, so the transcript says so
        and the module's rate is over what it did ask.
        """
        outcomes: list[CaseOutcome] = []
        session = self._new_session()
        graded = 0
        skipped = 0
        blank = 0
        unasked = 0
        t0 = time.monotonic()

        with requests_path.open("rb") as reqs, expected_path.open("rb") as exps:
            for raw_req in reqs:
                raw_req = raw_req.rstrip(b"\n")
                if not raw_req:
                    # Counted for the log only, and deliberately kept out of the
                    # accounting below: `corpus_lines` is how many *cases* the
                    # corpus declares, so a blank line -- which consumes no
                    # expectation and names no case -- must not be added to a sum
                    # compared against it.  Today the frozen file has none, which
                    # is exactly why this has to be right by construction rather
                    # than by the corpus happening not to exercise it.
                    blank += 1
                    continue
                raw_exp = vlib.read_ndjson_line(exps)
                if raw_exp is None:
                    raise RuntimeError(
                        f"{label}: expectations ran out at request "
                        f"{graded + skipped + 1}; the frozen corpus and its "
                        f"expectations disagree"
                    )
                case_id = _request_id(raw_req)
                if case_id not in cases:
                    skipped += 1
                    continue
                meta = cases[case_id]
                family = meta.get("family", "unknown")
                weight = weights.get(family, default_weight)

                reason = excluded.get(case_id)
                if reason is not None:
                    # Before the time budget as well as before the ask: a case
                    # nobody will be asked must not be able to consume the budget
                    # that decides whether the rest get asked.
                    outcomes.append(CaseOutcome(
                        case_id=f"{label}:{case_id}", family=family, kind="probe",
                        passed=False, weight=weight, not_applicable=reason,
                    ))
                    graded += 1
                    unasked += 1
                    continue

                if time.monotonic() - t0 > CORPUS_TIME_BUDGET:
                    self.log.write(
                        f"  {label}: {CORPUS_TIME_BUDGET:.0f}s budget spent after "
                        f"{graded} cases; marking the remainder failed"
                    )
                    self.crashes.append({
                        "corpus": label, "after_requests": graded,
                        "detail": f"corpus exceeded its {CORPUS_TIME_BUDGET:.0f}s budget",
                    })
                    outcomes.append(CaseOutcome(
                        case_id=f"{label}:{case_id}", family=family, kind="probe",
                        passed=False, weight=weight,
                        detail=f"not attempted: corpus exceeded its "
                               f"{CORPUS_TIME_BUDGET:.0f}s time budget",
                    ))
                    graded += 1
                    rest, rest_skipped, rest_unasked = self._fail_remaining(
                        reqs, exps, cases, label, weights, default_weight,
                        "not attempted: corpus exceeded its time budget",
                        excluded)
                    outcomes.extend(rest)
                    graded += len(rest)
                    skipped += rest_skipped
                    unasked += rest_unasked
                    break

                start = time.monotonic()
                try:
                    got = session.ask(raw_req)
                except ProbeDied as exc:
                    # A dead probe is not one failed case: it is every remaining
                    # case in this corpus.  Record why, restart, and carry on so
                    # the report says how much of the surface works rather than
                    # stopping at the first crash.
                    self.crashes.append({
                        "corpus": label,
                        "after_requests": graded,
                        "case_id": case_id,
                        "family": family,
                        "detail": str(exc)[:600],
                        "request": raw_req[:400].decode("utf-8", "replace"),
                    })
                    self.log.write(f"  probe died in {label} at case {case_id}: {exc}")
                    session.kill()
                    outcomes.append(CaseOutcome(
                        case_id=f"{label}:{case_id}", family=family, kind="probe",
                        passed=False, weight=weight,
                        detail=f"probe died: {exc}"[:400],
                    ))
                    graded += 1
                    if self.restarts >= MAX_RESTARTS:
                        self.log.write(
                            f"  {label}: probe has died {self.restarts} times; "
                            f"marking the rest of this corpus failed without rerunning it"
                        )
                        rest, rest_skipped, rest_unasked = self._fail_remaining(
                            reqs, exps, cases, label, weights, default_weight,
                            "probe repeatedly died; remaining cases not attempted",
                            excluded)
                        outcomes.extend(rest)
                        graded += len(rest)
                        skipped += rest_skipped
                        unasked += rest_unasked
                        break
                    self.restarts += 1
                    session = self._new_session()
                    continue

                duration = time.monotonic() - start
                if got == raw_exp:
                    outcomes.append(CaseOutcome(
                        case_id=f"{label}:{case_id}", family=family, kind="probe",
                        passed=True, weight=weight, duration=duration,
                    ))
                else:
                    off, ctx = vlib.first_difference(raw_exp, got)
                    outcomes.append(CaseOutcome(
                        case_id=f"{label}:{case_id}", family=family, kind="probe",
                        passed=False, weight=weight, duration=duration,
                        detail=_case_detail(meta, raw_req),
                        diff=ctx,
                    ))
                graded += 1
                if graded % 2000 == 0:
                    rate = graded / max(0.001, time.monotonic() - t0)
                    self.log.write(f"  {label}: {graded} cases, {rate:.0f}/s")

        code, err = session.finish()
        if code not in (0, None):
            self.crashes.append({
                "corpus": label, "after_requests": graded,
                "detail": f"probe exited {code} on EOF, expected 0",
                "stderr": err.decode("utf-8", "replace")[:600],
            })
            self.log.write(f"  {label}: probe exited {code} on EOF (expected 0)")
        self.log.write(
            f"  {label}: {graded - unasked} cases asked in "
            f"{time.monotonic() - t0:.1f}s ({skipped} corpus cases belong to "
            f"other modules"
            + (f", {unasked} have no portable answer and were skipped"
               if unasked else "")
            + ")"
        )

        # Every case in the slice must leave exactly one outcome, on every exit
        # path: the early ones hand the rest to _fail_remaining.  Without this,
        # a join key that stopped matching -- a renamed id field, an id that
        # arrived as a string -- would silently grade nothing and report a
        # perfect score over an empty set.
        if len(outcomes) != len(cases):
            raise RuntimeError(
                f"{label}: the module owns {len(cases)} frozen case(s) and "
                f"grading produced {len(outcomes)} outcome(s). The request "
                f"file and this module's slice do not join on case id."
            )
        # And the parts of the split must add back up to the whole corpus.
        # Skipping is what makes another module's line cheap, so the count that
        # proves nothing was lost has to span the cases this module declined as
        # well as the ones it answered.  The unasked ones are inside `graded`,
        # because they are in this module's slice and leave an outcome; what
        # separates them is that the outcome carries a reason instead of a
        # verdict.  `blank` is not in the sum: see the note at the blank-line
        # branch above.
        if graded + skipped != corpus_lines:
            raise RuntimeError(
                f"{label}: graded {graded} case(s) and skipped {skipped} "
                f"belonging to other modules, which is {graded + skipped} of "
                f"the {corpus_lines} case(s) the frozen corpus declares"
                + (f" ({blank} blank line(s) were read and are not counted)"
                   if blank else "")
            )
        # The exclusion has to have reached the cases it names.  An id that
        # stopped joining -- a corpus regenerated without re-freezing the list, an
        # id read as a string -- would leave the module asking a question with no
        # answer and reporting it as the submission's fault, which is the failure
        # the whole mechanism exists to prevent.  Counted from the outcomes rather
        # than from the counter, so this is a second measurement and not the same
        # one twice.
        owed = sorted(set(excluded) & set(cases))
        got = sorted(int(c.case_id.rsplit(":", 1)[1]) for c in outcomes
                     if c.not_applicable)
        if got != owed:
            raise RuntimeError(
                f"{label}: the exclusion list names {len(owed)} case(s) in this "
                f"module's slice and {len(got)} outcome(s) were skipped. Missing "
                f"{sorted(set(owed) - set(got))[:6]}, unexpected "
                f"{sorted(set(got) - set(owed))[:6]}."
            )
        return outcomes

    def _fail_remaining(self, reqs, exps, cases, label, weights, default_weight,
                        reason, excluded) -> tuple[list[CaseOutcome], int, int]:
        """Record the slice's untried remainder, skipping other modules' lines.

        Returns the outcomes, how many lines were skipped as another module's, and
        how many were not asked because they have no portable answer.  The caller
        owes an accounting of every line in the file and this consumes the tail of
        it.

        The exclusion is honoured here too.  This runs when the probe has died or
        the budget is spent, and neither is a reason to charge a submission for a
        case it was never going to be asked: without this, a slow port would be
        told it failed the very cases the image had decided are unanswerable.
        """
        out: list[CaseOutcome] = []
        skipped = 0
        unasked = 0
        for raw_req in reqs:
            raw_req = raw_req.rstrip(b"\n")
            if not raw_req:
                continue
            vlib.read_ndjson_line(exps)
            case_id = _request_id(raw_req)
            if case_id not in cases:
                skipped += 1
                continue
            family = cases[case_id].get("family", "unknown")
            excluded_reason = excluded.get(case_id)
            if excluded_reason is not None:
                unasked += 1
            out.append(CaseOutcome(
                case_id=f"{label}:{case_id}", family=family, kind="probe",
                passed=False, weight=weights.get(family, default_weight),
                detail="" if excluded_reason else reason,
                not_applicable=excluded_reason or "",
            ))
        return out, skipped, unasked

    def run_fixtures(
        self,
        manifest: list[dict],
        digests: dict[str, dict],
        fixture_dir: Path,
        *,
        weight: float,
    ) -> list[CaseOutcome]:
        """Grade the six bundles by digest.

        Each request is built here rather than stored: inlining a 1.8 MB source
        into a corpus file six times over is pointless when both sides can read
        the same file.  The response is hashed as it arrives so a 13 MB answer
        never lands in memory whole.
        """
        outcomes: list[CaseOutcome] = []
        session = self._new_session()
        t0 = time.monotonic()
        for entry in manifest:
            key = entry["key"]
            if time.monotonic() - t0 > CORPUS_TIME_BUDGET:
                outcomes.append(CaseOutcome(
                    case_id=f"fixtures:{key}", family="fixtures", kind="fixture",
                    passed=False, weight=weight,
                    detail=f"not attempted: fixtures exceeded their "
                           f"{CORPUS_TIME_BUDGET:.0f}s time budget",
                ))
                continue
            want = digests.get(key)
            if want is None:
                self.log.write(f"  fixtures: no frozen digest for {key}, skipping")
                continue
            source_path = fixture_dir / entry["fixture"]
            if not source_path.is_file():
                self.log.write(f"  fixtures: {source_path} missing from the image")
                continue
            source = source_path.read_text(encoding="utf-8")
            request = {"id": 1, "op": entry["op"], "source": source,
                       "options": entry["options"]}
            line = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode()

            start = time.monotonic()
            try:
                got = session.ask(line)
            except ProbeDied as exc:
                self.crashes.append({
                    "corpus": "fixtures", "case_id": key,
                    "detail": str(exc)[:600],
                })
                self.log.write(f"  probe died on fixture {key}: {exc}")
                session.kill()
                outcomes.append(CaseOutcome(
                    case_id=f"fixtures:{key}", family="fixtures", kind="fixture",
                    passed=False, weight=weight, detail=f"probe died: {exc}"[:400],
                ))
                session = self._new_session()
                continue
            duration = time.monotonic() - start
            got_digest = hashlib.sha256(got).hexdigest()
            if got_digest == want["sha256"] and len(got) == want["bytes"]:
                outcomes.append(CaseOutcome(
                    case_id=f"fixtures:{key}", family="fixtures", kind="fixture",
                    passed=True, weight=weight, duration=duration,
                ))
            else:
                detail = (
                    f"{entry['fixture']} {entry['op']} "
                    f"opts={json.dumps(entry['options'], sort_keys=True)}: "
                    f"reference {want['bytes']} bytes sha={want['sha256'][:16]}, "
                    f"submission {len(got)} bytes sha={got_digest[:16]}"
                )
                outcomes.append(CaseOutcome(
                    case_id=f"fixtures:{key}", family="fixtures", kind="fixture",
                    passed=False, weight=weight, duration=duration, detail=detail,
                    diff=_fixture_diff(want, got),
                ))
        code, err = session.finish()
        if code not in (0, None):
            self.log.write(f"  fixtures: probe exited {code} on EOF (expected 0)")
        return outcomes


def _fixture_diff(want: dict, got: bytes) -> str:
    """Where a fixture response first departs from the reference.

    The reference bytes are not kept -- one of these responses is 56 MB -- so the
    frozen record carries a digest per 32 KB block instead.  Comparing block by
    block finds the first block that disagrees, which is enough for a submitter
    to reproduce the case locally and diff it themselves.
    """
    blocks = want.get("blocks") or []
    size = want.get("block") or 32768
    if not blocks:
        return "no block digests were frozen for this fixture"
    want_bytes = want["bytes"]
    for index, digest in enumerate(blocks):
        at = index * size
        # Clipped to the reference's length, not the submission's.  A submission
        # that is byte-identical for the whole reference and then keeps going
        # would otherwise fail on its last block and be reported as a divergence
        # there, hiding the far more useful fact that it simply did not stop.
        chunk = got[at : min(at + size, want_bytes)]
        if not chunk:
            return (f"submission ends at byte {len(got)}; the reference "
                    f"continues to {want['bytes']}, so the response is truncated "
                    f"({len(blocks) - index} of {len(blocks)} blocks missing)")
        if hashlib.sha256(chunk).hexdigest()[:16] != digest:
            window = chunk[:220].decode("utf-8", "replace")
            note = ""
            if at + len(chunk) == len(got) < want_bytes:
                note = (f" (this is also where the submission's response ends, "
                        f"{want_bytes - len(got)} bytes early)")
            return (f"first divergence is in the {size // 1024} KB block at byte "
                    f"{at}{note}; the submission has there: {window}")
    if len(got) > want_bytes:
        return (f"the submission matches the reference for all {want_bytes} bytes "
                f"and then continues for {len(got) - want_bytes} more: "
                f"{got[want_bytes:want_bytes + 200].decode('utf-8', 'replace')}")
    if len(got) < want_bytes:
        return (f"the submission is {want_bytes - len(got)} bytes short at "
                f"{len(got)}, with no block disagreeing before that")
    return "the blocks match but the whole-response digest does not"


def _request_id(raw: bytes) -> int:
    """The `id` of a request, without parsing the whole line.

    Requests can carry a 1.8 MB source; json.loads on every line to read one
    integer is measurable across 16,000 of them.  The generator always writes
    `id` first, so the fast path is a prefix match, with a real parse as the
    fallback so a hand-edited corpus still works.
    """
    if raw.startswith(b'{"id":'):
        end = raw.find(b",", 6)
        if end > 6:
            try:
                return int(raw[6:end])
            except ValueError:
                pass
    try:
        return int(json.loads(raw)["id"])
    except Exception:
        return -1


def _case_detail(meta: dict, raw_req: bytes) -> str:
    """What a reader needs to reproduce one failing case."""
    try:
        req = json.loads(raw_req)
    except Exception:
        return f"unparseable request: {raw_req[:200]!r}"
    parts = [f"op={req.get('op')}"]
    if "options" in req:
        parts.append(f"options={json.dumps(req['options'], sort_keys=True)}")
    for key in ("pos", "start", "end", "test", "offset", "code", "astral",
                "visitors", "stop_at", "text"):
        if key in req:
            parts.append(f"{key}={json.dumps(req[key])}")
    source = req.get("source")
    if isinstance(source, str):
        shown = source if len(source) <= 220 else source[:220] + f"...[{len(source)} chars]"
        parts.append("source=" + json.dumps(shown))
    if meta.get("kind"):
        parts.append(f"kind={meta['kind']}")
    return " ".join(parts)[:1800]


def load_cases(path: Path) -> dict[int, dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {int(c["id"]): c for c in payload["cases"]}
