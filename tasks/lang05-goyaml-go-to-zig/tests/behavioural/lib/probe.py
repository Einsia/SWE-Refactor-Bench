#!/usr/bin/env python3
"""Runs the NDJSON differential: the submission's yaml-probe against gopkg.in/yaml.v3.

Four sources of truth, in decreasing order of how much the submission could have
anticipated them:

  frozen    expectations computed at image build time from the pinned Go
            reference, over the systematic cases.  The bulk of the grade.
  fresh     cases generated at grading time from a seed the submission has
            never seen, answered by the reference in the same container.  A
            submission tuned to the frozen set scores here only by being right.
  documents  twelve real-world YAML files, compared by digest because one of
            their responses is 600 KB of node tree.
  protocol  the transport rules -- the handshake, the argument errors, a
            malformed line, the empty-line rule, response ordering -- matched by
            position rather than by id.

The comparison is over raw bytes.  Not parsed JSON, not normalised JSON -- the
key order and the `\\uXXXX` escaping rule are part of the contract, and
re-serialising either side would hide exactly the failures this task is about.
"""

from __future__ import annotations

import difflib
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

# One request must not be able to hang the whole grading pass.  The slowest
# legitimate case is a depth-10,000 nesting or the 94 KB inventory document; the
# reference does either in well under a second.  This is enforced, not advisory:
# a probe that consumes a request and answers nothing would otherwise block until
# the harness killed the container, and the trial would be recorded as
# infrastructure failure rather than as the zero it has earned.
PER_REQUEST_TIMEOUT = 120.0
# The probe is a long-lived process fed thousands of lines.  This bounds the
# whole conversation, not one line.
SESSION_TIMEOUT = 2400.0
# The largest legitimate response is inventory.yaml under `stream`: 616 KB.  Two
# orders of magnitude above that is room for a submission that is wrong about
# sizes but still trying, and small enough that a probe emitting an unbounded
# stream is cut off rather than filling the container's memory.
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
# Enough stderr to diagnose a crash, not enough to matter.  It is drained
# continuously so a chatty probe cannot deadlock on a full stderr pipe and be
# misdiagnosed as one that stopped answering.
MAX_STDERR_BYTES = 64 * 1024
# Wall-clock ceiling for one shard.  The reference answers all 8,170 requests in
# about a second; a Zig implementation two orders of magnitude slower still fits
# comfortably.  The ceiling exists for the pathological case -- a probe that
# hangs on every request would otherwise spend 25 restarts x the per-request
# timeout, which is most of the verifier's budget, and the trial would time out
# instead of scoring.  Past the ceiling the remaining cases are marked failed,
# which is the same verdict reached more slowly.
CASE_TIME_BUDGET = 1200.0
# Restarts are also capped: a probe dying this often is not going to recover, and
# every restart re-pays process startup.
MAX_RESTARTS = 25


class ProbeDied(RuntimeError):
    """The probe exited, or stopped answering, mid-conversation."""


@dataclass
class Session:
    """A live yaml-probe process being fed one request at a time.

    The protocol says the probe must flush each response before reading the next
    request, and this class holds it to that: it writes one line, then blocks for
    one line back.  A probe that buffers its output deadlocks here, which is the
    correct outcome -- the instruction is explicit that it must not.
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

        Both halves are on a deadline.  Writing can block too: a document request
        carries a 94 KB source, more than a pipe will hold, so a probe that does
        not read blocks us in `write` rather than in `read`.
        """
        if self.proc is None or self.proc.poll() is not None:
            code = None if self.proc is None else self.proc.returncode
            raise ProbeDied(
                f"probe is not running (exit {code}) after {self.sent} requests")
        if time.monotonic() - self.started > SESSION_TIMEOUT:
            raise ProbeDied(
                f"probe session exceeded {SESSION_TIMEOUT}s after {self.sent} requests")
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
                raise ProbeDied(
                    f"writing request {self.sent + 1} failed: {exc}") from exc
            view = view[written:]

    def _read_line(self, deadline: float) -> bytes:
        """One newline-terminated response, or ProbeDied.

        Buffered by hand rather than via `readline` because a blocking readline on
        a live-but-silent child cannot be interrupted, and "silent" is a failure
        mode this task's protocol specifically forbids.
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
                    f"{PER_REQUEST_TIMEOUT:.0f}s "
                    f"({'still running' if alive else 'exited'}, "
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
                        f"probe exited ({code!r}) mid-response to request "
                        f"{self.sent}; {len(self.tail)} bytes without a terminating "
                        f"newline{note}"
                    )
                raise ProbeDied(
                    f"probe produced no response to request {self.sent} "
                    f"(exit {code!r}); it must answer every request and exit only "
                    f"on EOF{note}"
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


def _probe_env(env: dict) -> dict:
    """The environment the submission's probe runs in.

    No Go toolchain on PATH, and the probe is started with a cwd outside the
    repository, as the instruction says it will be.  The marker variable is not a
    switch the probe may read -- `guard/no-env-dispatch` exists precisely to
    reject a submission that behaves differently when it sees one -- it is here so
    that a process listing taken during grading says which processes are the
    probe.
    """
    out = dict(env)
    out["YAML_PROBE_UNDER_TEST"] = "1"
    return out


class Runner:
    """Feeds a set of cases through one probe and grades the answers."""

    def __init__(self, binary, *, log: Log, cwd: Path, env: dict) -> None:
        # A list is accepted as well as a path so the reference can be graded
        # against itself (`reference serve`) as an identity check.  If that does
        # not score 1.0, the harness is broken, not the submission.
        self.argv = [str(x) for x in binary] if isinstance(binary, (list, tuple)) \
            else [str(binary)]
        self.log = log
        self.cwd = cwd
        self.env = _probe_env(env)
        self.crashes: list[dict] = []
        self.restarts = 0

    def _new_session(self) -> Session:
        session = Session(argv=list(self.argv), cwd=self.cwd, env=self.env)
        session.start()
        return session

    def ask_batch(self, requests: list[dict]) -> tuple[list[dict | None], int]:
        """Put a short list of requests to one fresh session; parse the answers.

        For the callers that need an answer rather than a comparison -- the
        handshake case, and the spawn probe, which cares only that the process was
        driven through real work.  Returns one entry per request (None where the
        probe gave nothing usable) and the count answered before it stopped.

        A probe that dies partway leaves the rest None rather than raising: both
        callers treat silence as an answer, and neither should have to distinguish
        a crash from a refusal.
        """
        answers: list[dict | None] = [None] * len(requests)
        answered = 0
        session = Session(argv=list(self.argv), cwd=self.cwd, env=self.env)
        try:
            session.start()
        except OSError as exc:
            self.log.write(f"  could not start the probe: {exc}")
            return answers, 0
        try:
            for index, request in enumerate(requests):
                line = json.dumps(request, sort_keys=True).encode() + b"\n"
                try:
                    raw = session.ask(line)
                except ProbeDied as exc:
                    self.log.write(f"  probe died on request {index + 1}: {exc}")
                    self.crashes.append({
                        "label": "ask_batch", "after_requests": answered,
                        "detail": str(exc)[:600],
                    })
                    session.kill()
                    break
                answered += 1
                try:
                    answers[index] = json.loads(raw.decode("utf-8", "replace"))
                except (ValueError, UnicodeError):
                    # Answered, but not with JSON.  That is a failure of whichever
                    # case asked, not of this helper.
                    answers[index] = None
        finally:
            if session.proc is not None and session.proc.poll() is None:
                code, err = session.finish()
                if code not in (0, None):
                    self.log.write(
                        f"  probe exited {code} on EOF after "
                        f"{answered} one-shot request(s): "
                        f"{err.decode('utf-8', 'replace')[:200]}"
                    )
        return answers, answered

    def ask_one(self, request: dict) -> dict | None:
        """One request, one fresh session, one parsed answer or None."""
        answers, _ = self.ask_batch([request])
        return answers[0]

    def run_cases(
        self,
        requests_path: Path,
        expected_path: Path,
        cases: dict[int, dict],
        *,
        label: str,
        weights: dict[str, float],
        default_weight: float = 1.0,
    ) -> list[CaseOutcome]:
        """Grade a request file against an expectation file, line for line.

        Both files are read as streams and never held in memory: the frozen
        expectations are tens of megabytes.
        """
        outcomes: list[CaseOutcome] = []
        session = self._new_session()
        graded = 0
        t0 = time.monotonic()

        with requests_path.open("rb") as reqs, expected_path.open("rb") as exps:
            for raw_req in reqs:
                raw_req = raw_req.rstrip(b"\n")
                if not raw_req:
                    continue
                raw_exp = vlib.read_ndjson_line(exps)
                if raw_exp is None:
                    raise RuntimeError(
                        f"{label}: expectations ran out at request {graded + 1}; "
                        f"the requests and their expectations disagree"
                    )
                case_id = request_id(raw_req)
                meta = cases.get(case_id, {})
                family = meta.get("family", "unknown")
                weight = weights.get(family, default_weight)

                if time.monotonic() - t0 > CASE_TIME_BUDGET:
                    self.log.write(
                        f"  {label}: {CASE_TIME_BUDGET:.0f}s budget spent after "
                        f"{graded} cases; marking the remainder failed"
                    )
                    self.crashes.append({
                        "label": label, "after_requests": graded,
                        "detail": f"shard exceeded its {CASE_TIME_BUDGET:.0f}s budget",
                    })
                    outcomes.append(CaseOutcome(
                        case_id=f"{label}:{case_id}", family=family, kind="probe",
                        passed=False, weight=weight,
                        detail=f"not attempted: the shard exceeded its "
                               f"{CASE_TIME_BUDGET:.0f}s time budget",
                    ))
                    outcomes.extend(self._fail_remaining(
                        reqs, exps, cases, label, weights, default_weight,
                        "not attempted: the shard exceeded its time budget"))
                    break

                start = time.monotonic()
                try:
                    got = session.ask(raw_req)
                except ProbeDied as exc:
                    # A dead probe is not one failed case: it is every remaining
                    # case in this shard.  Record why, restart, and carry on so
                    # the report says how much of the surface works rather than
                    # stopping at the first crash.
                    self.crashes.append({
                        "label": label,
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
                            f"marking the rest of this shard failed without "
                            f"rerunning it"
                        )
                        outcomes.extend(self._fail_remaining(
                            reqs, exps, cases, label, weights, default_weight,
                            "probe repeatedly died; remaining cases not attempted"))
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
                    _off, ctx = vlib.first_difference(raw_exp, got)
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
                "label": label, "after_requests": graded,
                "detail": f"probe exited {code} on EOF, expected 0",
                "stderr": err.decode("utf-8", "replace")[:600],
            })
            self.log.write(f"  {label}: probe exited {code} on EOF (expected 0)")
        self.log.write(f"  {label}: {graded} cases in {time.monotonic() - t0:.1f}s")
        return outcomes

    def _fail_remaining(self, reqs, exps, cases, label, weights, default_weight,
                        reason) -> list[CaseOutcome]:
        out: list[CaseOutcome] = []
        for raw_req in reqs:
            raw_req = raw_req.rstrip(b"\n")
            if not raw_req:
                continue
            vlib.read_ndjson_line(exps)
            case_id = request_id(raw_req)
            meta = cases.get(case_id, {})
            family = meta.get("family", "unknown")
            out.append(CaseOutcome(
                case_id=f"{label}:{case_id}", family=family, kind="probe",
                passed=False, weight=weights.get(family, default_weight),
                detail=reason,
            ))
        return out

    def run_documents(
        self,
        manifest: list[dict],
        digests: dict[str, dict],
        document_dir: Path,
        *,
        weight: float,
    ) -> list[CaseOutcome]:
        """Grade the twelve real-world files by digest.

        Each request is built here rather than stored: inlining a 94 KB source
        into a case file six times over is pointless when both sides can read
        the same file.  The response is hashed as it arrives so a 600 KB answer
        never has to be kept.
        """
        outcomes: list[CaseOutcome] = []
        session = self._new_session()
        t0 = time.monotonic()
        for entry in manifest:
            key = entry["key"]
            if time.monotonic() - t0 > CASE_TIME_BUDGET:
                outcomes.append(CaseOutcome(
                    case_id=f"documents:{key}", family="documents", kind="document",
                    passed=False, weight=weight,
                    detail=f"not attempted: documents exceeded their "
                           f"{CASE_TIME_BUDGET:.0f}s time budget",
                ))
                continue
            want = digests.get(key)
            if want is None:
                self.log.write(f"  documents: no frozen digest for {key}, skipping")
                continue
            source_path = document_dir / entry["file"]
            if not source_path.is_file():
                self.log.write(f"  documents: {source_path} missing from the image")
                continue
            outcomes.append(self._one_document(session, entry, want, source_path,
                                              weight))
            if outcomes[-1].detail.startswith("probe died"):
                session = self._new_session()
        code, _err = session.finish()
        if code not in (0, None):
            self.log.write(f"  documents: probe exited {code} on EOF (expected 0)")
        return outcomes

    def _one_document(self, session: Session, entry: dict, want: dict,
                     source_path: Path, weight: float) -> CaseOutcome:
        key = entry["key"]
        request = {"id": 1, "op": entry["op"],
                   "source": source_path.read_text(encoding="utf-8")}
        if entry.get("indent") is not None:
            request["indent"] = entry["indent"]
        line = json.dumps(request, ensure_ascii=False,
                          separators=(",", ":")).encode("utf-8")
        start = time.monotonic()
        try:
            got = session.ask(line)
        except ProbeDied as exc:
            self.crashes.append({
                "label": "documents", "case_id": key, "detail": str(exc)[:600],
            })
            self.log.write(f"  probe died on document {key}: {exc}")
            session.kill()
            return CaseOutcome(
                case_id=f"documents:{key}", family="documents", kind="document",
                passed=False, weight=weight, detail=f"probe died: {exc}"[:400])
        duration = time.monotonic() - start
        got_digest = hashlib.sha256(got).hexdigest()
        if got_digest == want["sha256"] and len(got) == want["bytes"]:
            return CaseOutcome(
                case_id=f"documents:{key}", family="documents", kind="document",
                passed=True, weight=weight, duration=duration)
        indent = "" if entry.get("indent") is None else f" indent={entry['indent']}"
        return CaseOutcome(
            case_id=f"documents:{key}", family="documents", kind="document",
            passed=False, weight=weight, duration=duration,
            detail=(f"{entry['file']} {entry['op']}{indent}: reference "
                    f"{want['bytes']} bytes sha={want['sha256'][:16]}, submission "
                    f"{len(got)} bytes sha={got_digest[:16]}"),
            diff=_document_diff(want, got))

    def run_protocol(
        self,
        lines: list[bytes],
        expected: list[bytes],
        cases: list[dict],
        *,
        weight: float,
    ) -> list[CaseOutcome]:
        """Grade the transport rules, matched by position.

        This family is fed differently from every other: all the lines are written,
        stdin is closed, and the whole output is read back.  The reason is the
        empty-line rule.  One of these lines must produce no response, so a
        request-then-read loop would block on it -- and a probe that wrongly
        answers it would put every later response one position out of step, which
        a lockstep loop reports as a dozen mismatches rather than as the one
        protocol violation it is.

        Reading everything and then aligning the two lists lets a shift be
        attributed to the case that caused it.  Alignment is by difflib, on whole
        response lines: the matched blocks pass, a replaced block is a wrong
        answer, a deleted block is a missing answer, and an inserted block is a
        response to something that should not have been answered.

        Writing all thirteen lines before reading is safe -- they are under 500
        bytes in total, far inside one pipe buffer -- and it does not let a probe
        that buffers its output off the hook.  That rule is graded on its own, by
        `struct/probe-flushes`, which writes one line and waits.
        """
        outcomes: list[CaseOutcome] = []
        session = self._new_session()
        payload = b"".join(line + b"\n" for line in lines)
        try:
            session._write_all(payload, time.monotonic() + PER_REQUEST_TIMEOUT)
        except ProbeDied as exc:
            session.kill()
            self.crashes.append({"label": "protocol", "detail": str(exc)[:600]})
            return [CaseOutcome(
                case_id=f"protocol:{c['n']}", family="protocol", kind="protocol",
                passed=False, weight=weight,
                detail=f"probe would not accept the protocol requests: {exc}"[:400])
                for c in cases]

        got_lines, note = self._drain_responses(session)
        if note:
            self.crashes.append({"label": "protocol", "detail": note[:600]})
            self.log.write(f"  protocol: {note}")

        # Which case each expected response belongs to.  The unanswered case has
        # no expected response, so it is graded separately: it passes when nothing
        # was inserted at its position.
        answered = [c for c in cases if c["answered"]]
        if len(answered) != len(expected):
            raise RuntimeError(
                f"protocol: {len(answered)} cases expect a response but "
                f"{len(expected)} were frozen; the case index and the "
                f"expectations disagree")

        verdicts: dict[int, tuple[bool, str, str]] = {}
        extras = 0
        matcher = difflib.SequenceMatcher(None, expected, got_lines, autojunk=False)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                for k in range(i1, i2):
                    verdicts[answered[k]["n"]] = (True, "", "")
            elif tag == "replace":
                for k in range(i1, i2):
                    offset = j1 + (k - i1)
                    if offset < j2:
                        _off, ctx = vlib.first_difference(expected[k],
                                                         got_lines[offset])
                        verdicts[answered[k]["n"]] = (False, "wrong response", ctx)
                    else:
                        verdicts[answered[k]["n"]] = (
                            False, "no response at this position", "")
                extras += max(0, (j2 - j1) - (i2 - i1))
            elif tag == "delete":
                for k in range(i1, i2):
                    verdicts[answered[k]["n"]] = (
                        False, "the probe produced no response here", "")
            elif tag == "insert":
                extras += j2 - j1

        for case in cases:
            n = case["n"]
            label = case["label"]
            if case["answered"]:
                passed, detail, diff = verdicts.get(
                    n, (False, "not graded: alignment produced no verdict", ""))
                outcomes.append(CaseOutcome(
                    case_id=f"protocol:{n}", family="protocol", kind="protocol",
                    passed=passed, weight=weight,
                    detail="" if passed else f"{label}: {detail}", diff=diff))
            else:
                # The empty-line rule.  It is the only case whose evidence is the
                # absence of a response, so it is graded on the count: an extra
                # response anywhere means a line was answered that should not have
                # been, and this is the only line in the file that must not be.
                outcomes.append(CaseOutcome(
                    case_id=f"protocol:{n}", family="protocol", kind="protocol",
                    passed=extras == 0, weight=weight,
                    detail="" if extras == 0 else
                           f"{label}: the probe wrote {extras} more response "
                           f"line(s) than there were requests to answer; an empty "
                           f"line is skipped, not answered"))

        code, err = session.finish()
        if code not in (0, None):
            self.log.write(f"  protocol: probe exited {code} on EOF (expected 0)")
            self.crashes.append({
                "label": "protocol",
                "detail": f"probe exited {code} on EOF, expected 0",
                "stderr": err.decode("utf-8", "replace")[:600]})
        return outcomes

    def _drain_responses(self, session: Session) -> tuple[list[bytes], str]:
        """Read every response line after stdin is closed.

        Returns the lines and a note about anything irregular -- a partial final
        line, or a probe that had to be killed.  Both are recorded rather than
        raised: the responses that did arrive are still evidence.
        """
        proc = session.proc
        assert proc is not None
        try:
            proc.stdin.close()
        except OSError:
            pass
        deadline = time.monotonic() + PER_REQUEST_TIMEOUT
        buf = session.tail
        session.tail = b""
        out_fd = proc.stdout.fileno()
        err_fd = proc.stderr.fileno() if proc.stderr else None
        note = ""
        while time.monotonic() < deadline:
            watch = [out_fd] + ([err_fd] if err_fd is not None else [])
            try:
                ready = select.select(watch, [], [], 0.5)[0]
            except OSError:
                break
            if err_fd is not None and err_fd in ready:
                session._drain_stderr(err_fd)
            if out_fd in ready:
                try:
                    chunk = os.read(out_fd, 1 << 20)
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                if len(buf) > MAX_RESPONSE_BYTES:
                    note = f"protocol output passed {MAX_RESPONSE_BYTES} bytes"
                    break
            elif not ready and proc.poll() is not None:
                break
        else:
            note = f"the probe did not close its stdout within {PER_REQUEST_TIMEOUT:.0f}s"
        if proc.poll() is None and note:
            session.kill()
        lines = buf.split(b"\n")
        if lines and lines[-1] == b"":
            lines.pop()
        elif lines:
            note = (note + "; " if note else "") + \
                f"the last response line was not newline-terminated ({len(lines[-1])} bytes)"
        return lines, note


def _document_diff(want: dict, got: bytes) -> str:
    """Where a document response first departs from the reference.

    The reference bytes are not kept -- one of these responses is 616 KB -- so the
    frozen record carries a digest per 32 KB block instead.  Comparing block by
    block finds the first block that disagrees, which is enough for a submitter to
    reproduce the case locally and diff it themselves.
    """
    blocks = want.get("blocks") or []
    size = want.get("block") or 32768
    if not blocks:
        return "no block digests were frozen for this document"
    want_bytes = want["bytes"]
    for index, digest in enumerate(blocks):
        at = index * size
        # Clipped to the reference's length, not the submission's.  A submission
        # that is byte-identical for the whole reference and then keeps going would
        # otherwise fail on its last block and be reported as a divergence there,
        # hiding the far more useful fact that it simply did not stop.
        chunk = got[at: min(at + size, want_bytes)]
        if not chunk:
            return (f"submission ends at byte {len(got)}; the reference continues "
                    f"to {want_bytes}, so the response is truncated "
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


def request_id(raw: bytes) -> int:
    """The `id` of a request, without parsing the whole line.

    Requests can carry a 24 KB source; json.loads on every line to read one
    integer is measurable across 8,000 of them.  The generator always writes `id`
    first, so the fast path is a prefix match, with a real parse as the fallback so
    a hand-edited case file still works.
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
    if "indent" in req:
        parts.append(f"indent={json.dumps(req['indent'])}")
    if meta.get("label"):
        parts.append(f"case={json.dumps(meta['label'])}")
    source = req.get("source")
    if isinstance(source, str):
        shown = source if len(source) <= 220 else \
            source[:220] + f"...[{len(source)} chars]"
        parts.append("source=" + json.dumps(shown))
    return " ".join(parts)[:1800]


def load_cases(path: Path) -> dict[int, dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {int(c["id"]): c for c in payload["cases"]}


def load_protocol(cases_path: Path, requests_path: Path,
                  expected_path: Path) -> tuple[list[bytes], list[bytes], list[dict]]:
    """The protocol family: request lines, frozen responses, case index.

    The request file is read as bytes and split on newlines without stripping,
    because one of its lines is empty and that is the case.  `splitlines()` on the
    whole blob would be equivalent here, but reading line by line and discarding
    empties -- which is what every other case loader in this file does -- would
    silently drop the very case this family exists to grade.
    """
    payload = json.loads(cases_path.read_text(encoding="utf-8"))
    cases = payload["cases"]
    raw = requests_path.read_bytes()
    lines = raw.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    if len(lines) != payload["count"]:
        raise RuntimeError(
            f"protocol: {requests_path.name} has {len(lines)} lines but "
            f"{cases_path.name} declares {payload['count']} cases")
    expected = [ln for ln in expected_path.read_bytes().split(b"\n") if ln]
    return lines, expected, cases
