#!/usr/bin/env python3
"""Drives the probe protocol and the CLI, for the reference and for a submission.

Two runners.  The probe runner speaks the line protocol to whichever program is
answering; the CLI runner invokes the installed binary once per case and reduces
the run to a comparable signature.

Three properties this module has to hold, all of them about a submission that
misbehaves rather than one that is merely wrong:

Attribution.  A crash must be blamed on the case that caused it and nothing else.
Requests and answers travel in lockstep -- one written, one read -- so when the
pipe closes there is exactly one outstanding case, and it is the victim.  A
batched protocol would leave a window of unanswered requests and no way to tell
which of them was fatal.

Containment.  A submission that segfaults on every input must not spawn 2000
processes, and one that hangs must not consume the verifier's whole budget.  A
restart budget bounds the first; a per-case read deadline bounds the second.

Isolation.  A tier whose binary did not compile marks its own cases and leaves
the other three tiers alone.  Neither submission is paid -- the stage pays for
every scored check or none -- but a port that finishes the root package and
abandons the filters must not report the same rate as an empty tree, and it does
not: one compile error stands for its own tier's cases and no others.
"""

from __future__ import annotations

import base64
import errno
import os
import selectors
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import vlib
from vlib import Log, Result

# Per-case read deadline.  Generous: the pathological documents take real
# time in the reference, and a slow-but-correct port must not be failed for it.
CASE_TIMEOUT = 90.0
# How long to wait for a session to come up and answer its first request.
STARTUP_TIMEOUT = 60.0
# Restarts allowed per tier before the rest of that tier is abandoned.  A
# submission crashing this often has failed the tier; continuing only costs time.
MAX_RESTARTS = 40
CLI_TIMEOUT = 90.0
# Answers can be large -- a reindented pathological document is hundreds of KB --
# so the line ceiling is high, but it is not unbounded.
MAX_LINE = 64 * 1024 * 1024


def slug(text: str) -> str:
    """Reduce a log label to something safe as a single filename component."""
    kept = [c if (c.isalnum() or c in "-_.") else "-" for c in text]
    return "".join(kept).strip("-") or "probe"


@dataclass
class Record:
    """One answered case: the status word and the decoded payload."""

    case_id: str
    status: str
    payload: bytes
    note: str = ""

    @property
    def answered(self) -> bool:
        """True when the program produced an answer, right or wrong.

        `err` counts: "this input raises" is behavior under test.  `defect`,
        `crash:*` and `timeout` do not -- those are the harness or the process
        failing rather than the implementation answering.
        """
        return self.status in ("ok", "err")


class ProtocolError(Exception):
    """The peer said something that is not the protocol."""


class LineChannel:
    """Reads newline-delimited records from a raw fd with a deadline.

    Built on os.read and selectors rather than a buffered file object: select
    reports readiness of the file descriptor, so bytes already sitting in a
    Python-level buffer would make a ready channel look idle and the read would
    block past its deadline.  Owning the buffer removes that gap.
    """

    def __init__(self, fd: int) -> None:
        self.fd = fd
        self.buf = bytearray()
        self.eof = False
        self._sel = selectors.DefaultSelector()
        self._sel.register(fd, selectors.EVENT_READ)

    def close(self) -> None:
        try:
            self._sel.unregister(self.fd)
        except (KeyError, ValueError):
            pass
        self._sel.close()

    def readline(self, timeout: float) -> bytes | None:
        """One line without its newline, b"" on EOF, or None on timeout."""
        deadline = vlib.now() + timeout
        while True:
            at = self.buf.find(b"\n")
            if at >= 0:
                line = bytes(self.buf[:at])
                del self.buf[: at + 1]
                return line
            if self.eof:
                if self.buf:
                    line = bytes(self.buf)
                    self.buf.clear()
                    return line
                return b""
            if len(self.buf) > MAX_LINE:
                raise ProtocolError(
                    f"answer exceeded {MAX_LINE} bytes with no newline"
                )
            remaining = deadline - vlib.now()
            if remaining <= 0:
                return None
            if not self._sel.select(min(remaining, 1.0)):
                continue
            try:
                chunk = os.read(self.fd, 1 << 16)
            except OSError as exc:
                if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    continue
                self.eof = True
                continue
            if not chunk:
                self.eof = True
                continue
            self.buf.extend(chunk)


def build_request(case: dict) -> str:
    """The wire line for a case: id, op, then the already-tagged arguments."""
    parts = [case["id"], case["op"]]
    parts.extend(case.get("args", []))
    for part in parts:
        if "\t" in part or "\n" in part:
            raise ProtocolError(
                f"case {case['id']}: argument contains a wire delimiter: {part!r}"
            )
    return "\t".join(parts)


def parse_response(line: bytes, expect_id: str) -> Record:
    """Decode `id \\t status \\t base64` and confirm it answers this request."""
    try:
        text = line.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProtocolError(f"answer is not UTF-8: {exc}") from exc
    parts = text.split("\t")
    if len(parts) < 2:
        raise ProtocolError(f"answer has {len(parts)} field(s): {text[:200]!r}")
    got_id, status = parts[0], parts[1]
    if got_id != expect_id:
        # Lockstep means this cannot happen from lag; it means the peer answered
        # the wrong question, and grading it against this case's expectation
        # would compare two different things.
        raise ProtocolError(
            f"answer id mismatch: asked {expect_id!r}, got {got_id!r}"
        )
    if status not in ("ok", "err", "defect"):
        raise ProtocolError(f"unknown status {status!r} for case {expect_id}")
    body = parts[2] if len(parts) > 2 else ""
    try:
        payload = base64.b64decode(body, validate=True) if body else b""
    except (ValueError, TypeError) as exc:
        raise ProtocolError(f"case {expect_id}: payload is not base64: {exc}") from exc
    return Record(case_id=expect_id, status=status, payload=payload)


class ProbeSession:
    """One live probe process, restartable."""

    def __init__(
        self,
        argv: list[str],
        *,
        cwd: Path | None,
        env: dict,
        log: Log,
        label: str,
    ) -> None:
        self.argv = list(argv)
        self.cwd = cwd
        self.env = env
        self.log = log
        self.label = label
        self.proc: subprocess.Popen | None = None
        self.chan: LineChannel | None = None
        self.stderr_path: Path | None = None
        self.starts = 0

    @staticmethod
    def stderr_path_for(label: str, pid: int, starts: int) -> Path:
        """Where a session's stderr goes.

        stderr goes to a file rather than a pipe: nothing reads it during the
        exchange, so a pipe would fill and block a chatty program forever.

        A function rather than an expression inside `start`, because the label is
        prose for the log ("submission/fresh") and must not reach a path
        unfiltered: a `/` in it names a directory nobody created, `open` raises
        ENOENT, and run_tier reports that as the submission failing to start --
        every case in the tier marked crashed over a verifier bug.  Measured, that
        cost every probe case in the suite: 0/391 on the smallest probe module,
        which is a failing scored check, so no submission could be paid for this
        stage at all.  slug() is what makes the filename a filename, and
        driver._check_probe_labels opens one of these for every label the driver
        actually constructs, so the property is tested rather than asserted.
        """
        return Path("/tmp") / f"probe-{slug(label)}-{pid}-{starts}.err"

    def start(self) -> None:
        self.stop()
        self.stderr_path = self.stderr_path_for(
            self.label, os.getpid(), self.starts)
        handle = open(self.stderr_path, "wb")
        try:
            self.proc = subprocess.Popen(
                self.argv,
                cwd=str(self.cwd) if self.cwd else None,
                env=self.env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=handle,
                bufsize=0,
            )
        finally:
            handle.close()
        assert self.proc.stdout is not None
        self.chan = LineChannel(self.proc.stdout.fileno())
        self.starts += 1

    def stop(self) -> None:
        if self.chan is not None:
            self.chan.close()
            self.chan = None
        proc = self.proc
        self.proc = None
        if proc is None:
            return
        for stream in (proc.stdin, proc.stdout):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        if proc.poll() is None:
            proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stderr_tail(self, limit: int = 400) -> str:
        if self.stderr_path is None or not self.stderr_path.exists():
            return ""
        try:
            blob = self.stderr_path.read_bytes()[-4096:]
        except OSError:
            return ""
        text = blob.decode("utf-8", "replace").strip()
        return " | ".join(text.splitlines()[-4:])[-limit:]

    def exit_note(self) -> str:
        """Why the process is gone, in the form the status word carries."""
        proc = self.proc
        if proc is None:
            return "not-started"
        code = proc.poll()
        if code is None:
            return "alive"
        if code < 0:
            return f"signal{-code}"
        return f"exit{code}"

    def ask(self, case: dict, timeout: float) -> Record:
        """Send one request, read one answer.

        Raises BrokenPipeError if the peer is gone and ProtocolError if it
        answered something that is not the protocol; the caller decides what
        those mean.
        """
        if not self.alive or self.chan is None or self.proc is None:
            raise BrokenPipeError(f"session not running ({self.exit_note()})")
        line = build_request(case).encode("utf-8") + b"\n"
        assert self.proc.stdin is not None
        try:
            self.proc.stdin.write(line)
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise BrokenPipeError(f"write failed: {exc}") from exc
        answer = self.chan.readline(timeout)
        if answer is None:
            raise TimeoutError(f"no answer within {timeout:.0f}s")
        if answer == b"":
            raise BrokenPipeError(f"peer closed the stream ({self.exit_note()})")
        return parse_response(answer, case["id"])


class ProbeDriver:
    """Answers every probe case, routing each to the tier that can serve it.

    Constructed with a resolver rather than a path: `argv_for(tier)` returns the
    command line for that tier, or None when there is no program for it.  The
    reference passes a resolver that returns the same Python probe for all four
    tiers -- the reference answers every op from one implementation, and driving
    it as four sessions in the same case order as the submission keeps the two
    halves as similar as possible, so a difference is attributable to the port
    rather than to how it was driven.
    """

    def __init__(
        self,
        argv_for,
        *,
        cwd: Path | None,
        env: dict,
        log: Log,
        label: str,
        progress_every: int = 250,
    ) -> None:
        self.argv_for = argv_for
        self.cwd = cwd
        self.env = env
        self.log = log
        self.label = label
        self.progress_every = progress_every
        self.crashes: list[dict] = []
        self.tier_notes: dict[str, str] = {}

    # -- driving ---------------------------------------------------------

    def run(self, cases: list[dict]) -> dict[str, Record]:
        import oracle

        out: dict[str, Record] = {}
        for tier, group in sorted(oracle.cases_by_tier(cases).items()):
            out.update(self.run_tier(tier, group))
        return out

    def run_tier(self, tier: str, cases: list[dict]) -> dict[str, Record]:
        argv = self.argv_for(tier)
        if argv is None:
            self.tier_notes[tier] = "no program for this tier"
            self.log.write(
                f"probe[{self.label}/{tier}]: no program; "
                f"{len(cases)} case(s) unanswerable"
            )
            return {
                case["id"]: Record(case["id"], "crash:tier-not-built", b"",
                                   note="no program for this tier")
                for case in cases
            }

        session = ProbeSession(
            argv, cwd=self.cwd, env=self.env, log=self.log,
            label=f"{self.label}-{tier}",
        )
        records: dict[str, Record] = {}
        restarts = 0
        exhausted = False
        try:
            try:
                session.start()
            except OSError as exc:
                self.tier_notes[tier] = f"cannot start: {exc}"
                self.log.write(f"probe[{self.label}/{tier}]: cannot start: {exc}")
                return {
                    case["id"]: Record(case["id"], "crash:no-session", b"",
                                       note=f"cannot start: {exc}")
                    for case in cases
                }

            for index, case in enumerate(cases):
                if exhausted:
                    records[case["id"]] = Record(
                        case["id"], "crash:restart-budget", b"",
                        note=f"tier abandoned after {MAX_RESTARTS} restarts",
                    )
                    continue

                timeout = STARTUP_TIMEOUT if index == 0 else CASE_TIMEOUT
                if not session.alive:
                    try:
                        session.start()
                        timeout = STARTUP_TIMEOUT
                    except OSError as exc:
                        records[case["id"]] = Record(
                            case["id"], "crash:no-session", b"",
                            note=f"restart failed: {exc}")
                        exhausted = True
                        continue

                try:
                    records[case["id"]] = session.ask(case, timeout)
                except TimeoutError as exc:
                    records[case["id"]] = self._victim(
                        case, tier, "timeout", str(exc), session)
                    restarts += 1
                    session.stop()
                except BrokenPipeError as exc:
                    note = session.exit_note()
                    records[case["id"]] = self._victim(
                        case, tier, f"crash:{note}", str(exc), session)
                    restarts += 1
                    session.stop()
                except ProtocolError as exc:
                    # The stream is no longer trustworthy: an extra or malformed
                    # line desynchronizes every later answer, so the session is
                    # replaced rather than continued.
                    records[case["id"]] = self._victim(
                        case, tier, "crash:protocol", str(exc), session)
                    restarts += 1
                    session.stop()

                if restarts > MAX_RESTARTS and not exhausted:
                    exhausted = True
                    self.tier_notes[tier] = (
                        f"abandoned after {MAX_RESTARTS} restarts"
                    )
                    self.log.write(
                        f"probe[{self.label}/{tier}]: {MAX_RESTARTS} restarts, "
                        f"abandoning {len(cases) - index - 1} remaining case(s)"
                    )

                if self.progress_every and (index + 1) % self.progress_every == 0:
                    self.log.write(
                        f"probe[{self.label}/{tier}]: {index + 1}/{len(cases)}"
                    )
        finally:
            session.stop()

        answered = sum(1 for r in records.values() if r.answered)
        self.log.write(
            f"probe[{self.label}/{tier}]: {answered}/{len(cases)} answered, "
            f"{restarts} restart(s)"
        )
        return records

    def _victim(
        self, case: dict, tier: str, status: str, detail: str,
        session: ProbeSession,
    ) -> Record:
        note = detail
        tail = session.stderr_tail()
        if tail:
            note = f"{detail}; stderr: {tail}"
        self.crashes.append(
            {"case_id": case["id"], "tier": tier, "op": case.get("op", ""),
             "status": status, "note": note}
        )
        if len(self.crashes) <= 12:
            self.log.write(
                f"probe[{self.label}/{tier}]: {case['id']} -> {status}: {note}"
            )
        return Record(case["id"], status, b"", note=note)


def reference_argv(probe_py: Path, docs: Path, docsmeta: Path, spec_json: Path):
    """Resolver for the Python reference: one program answers every tier.

    The interpreter is named by absolute path through vlib.PYTHON rather than as
    `python3`, because the graded build installs a refusing shim under that name at
    the front of PATH; a PATH lookup here would find the shim.
    """

    def resolve(tier: str) -> list[str] | None:
        return [
            vlib.PYTHON, str(probe_py),
            "--docs", str(docs),
            "--documents", str(docsmeta),
            "--spec", str(spec_json),
            "--tier", tier,
        ]

    return resolve


def submission_argv(bin_dir: Path, docs: Path, docsmeta: Path, spec_json: Path):
    """Resolver for the compiled tiers: one binary per tier, absent if unbuilt."""

    def resolve(tier: str) -> list[str] | None:
        binary = bin_dir / f"probe-{tier}"
        if not binary.exists() or not os.access(binary, os.X_OK):
            return None
        return [
            str(binary),
            "--docs", str(docs),
            "--documents", str(docsmeta),
            "--spec", str(spec_json),
            "--tier", tier,
        ]

    return resolve


class CliRunner:
    """Runs the installed `sqlformat` once per case and reduces it to a signature.

    The signature is what gets compared, and how much of the run it includes is
    the case's `grade`.  Not everything a CLI emits is a portable contract: an
    argument parser's prose about a bad flag is an implementation detail of the
    parser, while the exit status and the formatted output are the interface.
    Grading the prose would fail a correct port; grading nothing but the status
    would pass one that prints garbage.  Each case says which it is.
    """

    GRADES = ("full", "status-and-diag", "status-and-stderr",
              "status-and-prefix", "status-and-shape")

    def __init__(
        self,
        binary: Path,
        docs: Path,
        log: Log,
        *,
        label: str,
        work_dir: Path,
        env: dict | None = None,
        timeout: float = CLI_TIMEOUT,
    ) -> None:
        self.binary = Path(binary)
        self.docs = Path(docs)
        self.log = log
        self.label = label
        self.work_dir = Path(work_dir)
        self.env = env if env is not None else vlib.base_env()
        self.timeout = timeout
        self.work_dir.mkdir(parents=True, exist_ok=True)

    # -- invocation ------------------------------------------------------

    def doc_path(self, doc_id: str) -> Path:
        return self.docs / doc_id

    def outfile_path(self, case: dict) -> Path:
        """Where an {out} case writes.  Named by case id so it is re-readable."""
        return self.work_dir / f"out-{case['id']}.sql"

    def argv_for(self, case: dict) -> list[str]:
        argv = [str(self.binary)]
        doc = case.get("file_doc")
        out = self.outfile_path(case)
        for arg in case.get("argv", []):
            if "{doc}" in arg:
                if doc is None:
                    raise RuntimeError(
                        f"cli case {case['id']} uses {{doc}} with no file_doc")
                arg = arg.replace("{doc}", str(self.doc_path(doc)))
            if "{out}" in arg:
                arg = arg.replace("{out}", str(out))
            argv.append(arg)
        return argv

    def run_case(self, case: dict) -> Result:
        stdin_doc = case.get("stdin_doc")
        stdin_data = b""
        if stdin_doc is not None:
            stdin_data = self.doc_path(stdin_doc).read_bytes()

        # Remove any previous output file first: a port that fails to write one
        # must not inherit the reference's, or the case would pass on a stale
        # artifact.
        out = self.outfile_path(case)
        writes_file = any("{out}" in a for a in case.get("argv", []))
        if writes_file and out.exists():
            out.unlink()

        return vlib.run(
            self.argv_for(case),
            cwd=self.work_dir,
            env=self.env,
            timeout=self.timeout,
            stdin_data=stdin_data,
            full_capture=True,
        )

    # -- reduction -------------------------------------------------------

    def normalize(self, blob: bytes) -> bytes:
        """Replace machine-specific paths with stable placeholders.

        The binary and the document set live at different absolute paths in the freeze
        image and in a graded submission's tree, and both appear in error
        messages.  Without this, every error-path case would fail on the path
        rather than on the behavior.
        """
        for needle, token in (
            (str(self.binary).encode(), b"<BIN>"),
            (str(self.docs).encode(), b"<DOCS>"),
            (str(self.work_dir).encode(), b"<WORK>"),
            (self.binary.name.encode(), b"<PROG>"),
        ):
            if needle:
                blob = blob.replace(needle, token)
        return blob

    @staticmethod
    def diagnostic(result: Result) -> bytes:
        """Which channel the failure went to, and whether it opened with usage.

        For the cases the argument parser rejects, the exit status alone is not an
        assertion: the reference exits 2, and so does any binary that panics on
        startup, so a submission that implements nothing passes by failing.  Seven
        cases did.  The prose is still not gradable -- "invalid choice: 'bogus'
        (choose from ...)" is argparse's phrasing and no port would reproduce it --
        so what is compared is the part of the diagnostic that is a property of the
        program rather than of the parser that wrote it:

          stdout-empty   an argument error must not write to the output stream
          stderr-empty   it must say something
          usage-first    whether the first line opens with usage, which is the
                         conventional shape and what the reference does

        `usage-first` is matched case-insensitively on the first word alone, so
        argparse's "usage: sqlformat [OPTIONS]..." and Go's flag package's "Usage
        of sqlformat:" both satisfy it.  A panic, a silent exit and a diagnostic
        on the wrong stream each fail it.
        """
        first = result.stderr.split(b"\n", 1)[0].lstrip().lower()
        return (b"stdout-empty=%s stderr-empty=%s usage-first=%s" % (
            b"true" if not result.stdout.strip() else b"false",
            b"true" if not result.stderr.strip() else b"false",
            b"true" if first.startswith(b"usage") else b"false"))

    @staticmethod
    def error_prefix(stderr: bytes) -> bytes:
        """The stable head of an error line, without the OS's own wording.

        `[ERROR] Failed to read /x: [Errno 2] No such file or directory: '/x'`
        keeps everything up to the first `": "`.  The template and the path are
        the library's; the errno prose is CPython's and has no Go equivalent.
        """
        first = stderr.split(b"\n", 1)[0]
        at = first.find(b": ")
        return first[:at] if at > 0 else first

    @staticmethod
    def shape(stdout: bytes) -> bytes:
        """A structural summary of help output.

        --help layout is argparse's, not sqlparse's: column widths and wrapping
        would differ in any reimplementation and mean nothing about correctness.
        What is contractual is that every documented flag is advertised, so the
        summary is the sorted set of option strings plus whether a usage line
        opened it.
        """
        text = stdout.decode("utf-8", "replace")
        flags: set[str] = set()
        for token in text.replace(",", " ").replace("=", " ").split():
            token = token.strip("[]()<>.'\"")
            if token.startswith("--") and len(token) > 2:
                flags.add(token)
            elif (
                len(token) == 2
                and token[0] == "-"
                and (token[1].isalpha())
            ):
                flags.add(token)
        has_usage = text.lstrip().lower().startswith("usage")
        return ("usage=%s flags=%s" % (has_usage, ",".join(sorted(flags)))).encode()

    def signature(self, case: dict, result: Result) -> bytes:
        """The comparable reduction of one CLI run, per the case's grade."""
        grade = case.get("grade", "full")
        if grade not in self.GRADES:
            raise RuntimeError(f"cli case {case['id']}: unknown grade {grade!r}")

        parts = [f"rc={result.returncode}".encode()]
        if result.timed_out:
            parts.append(b"timeout=1")

        if grade == "status-and-diag":
            parts.append(b"--diag--")
            parts.append(self.diagnostic(result))
            return b"\n".join(parts) + b"\n"

        if grade == "status-and-stderr":
            parts.append(b"--stderr--")
            parts.append(self.normalize(result.stderr))
            return b"\n".join(parts) + b"\n"

        if grade == "status-and-prefix":
            parts.append(b"--stderr-prefix--")
            parts.append(self.error_prefix(self.normalize(result.stderr)))
            return b"\n".join(parts) + b"\n"

        if grade == "status-and-shape":
            parts.append(b"--stdout-shape--")
            parts.append(self.shape(result.stdout))
            return b"\n".join(parts) + b"\n"

        parts.append(b"--stdout--")
        parts.append(self.normalize(result.stdout))
        parts.append(b"--stderr--")
        parts.append(self.normalize(result.stderr))
        if any("{out}" in a for a in case.get("argv", [])):
            out = self.outfile_path(case)
            parts.append(b"--outfile--")
            if out.exists():
                parts.append(b"present")
                parts.append(self.normalize(out.read_bytes()))
            else:
                parts.append(b"absent")
        return b"\n".join(parts) + b"\n"


def which(name: str) -> str | None:
    return shutil.which(name)


@dataclass
class ProbeSummary:
    """What a probe run produced, for the report's structural half."""

    total: int = 0
    answered: int = 0
    by_status: dict[str, int] = field(default_factory=dict)
    tier_notes: dict[str, str] = field(default_factory=dict)

    @classmethod
    def of(cls, records: dict[str, Record], tier_notes: dict[str, str]) -> ProbeSummary:
        summary = cls(total=len(records), tier_notes=dict(tier_notes))
        for record in records.values():
            summary.by_status[record.status] = (
                summary.by_status.get(record.status, 0) + 1
            )
            if record.answered:
                summary.answered += 1
        summary.by_status = dict(sorted(summary.by_status.items()))
        return summary
