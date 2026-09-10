#!/usr/bin/env python3
"""Shared plumbing for the lang02-zlib-c-to-java verifier.

Process execution, logging and the evidence ledger.  Every command the verifier
runs goes through `run()` so that the transcript in the report accounts for all
of it -- a reader can reconstruct exactly what was executed, in what
environment, and what came back.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

LOG_DIR = Path("/logs/verifier")

# Build and tool output is capped: a runaway compiler can emit megabytes of
# warnings and none of it is graded.  Measurement output must never be capped --
# a truncated probe transcript is indistinguishable from a probe that stopped
# early -- so callers whose stdout *is* the data pass full_capture=True.
MAX_CAPTURE = 256 * 1024


def now() -> float:
    return time.monotonic()


@dataclass
class Result:
    argv: list[str]
    cwd: str
    returncode: int
    stdout: bytes
    stderr: bytes
    duration: float
    timed_out: bool = False
    #: True when the child was killed for going quiet rather than for exceeding
    #: `timeout`.  Both set `timed_out`, because both are the same thing to a
    #: caller deciding whether the output is complete; they differ only in what
    #: the caller may then say about *which* work stalled, so the distinction is
    #: a second flag rather than a different value of the first one.  Defaulted,
    #: so every existing `Result(...)` construction and every reader of `ok`
    #: keeps its meaning untouched.
    idle_timeout_hit: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def text(self) -> str:
        return self.stdout.decode("utf-8", "replace")

    def tail(self, lines: int = 12, limit: int = 1200) -> str:
        """The end of the output, which is where a tool says why it failed."""
        blob = (self.stderr or self.stdout).decode("utf-8", "replace").rstrip()
        if not blob:
            blob = (self.stdout or b"").decode("utf-8", "replace").rstrip()
        excerpt = " | ".join(blob.splitlines()[-lines:])
        return excerpt[-limit:]

    def brief(self, limit: int = 4000) -> dict:
        def clip(data: bytes) -> str:
            text = data.decode("utf-8", "replace")
            if len(text) > limit:
                head = text[: limit // 2]
                tail = text[-limit // 2 :]
                return f"{head}\n...[{len(text) - limit} chars elided]...\n{tail}"
            return text

        return {
            "argv": self.argv,
            "cwd": self.cwd,
            "returncode": self.returncode,
            "timed_out": self.timed_out,
            "duration_sec": round(self.duration, 3),
            "stdout": clip(self.stdout),
            "stderr": clip(self.stderr),
        }


class Log:
    """Verifier log: one stream to stderr, one durable file, one command ledger."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self.commands: list[dict] = []
        self._fh = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = path.open("a", encoding="utf-8")

    def write(self, message: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {message}"
        print(line, file=sys.stderr, flush=True)
        if self._fh:
            self._fh.write(line + "\n")
            self._fh.flush()

    def section(self, title: str) -> None:
        self.write("=" * 4 + f" {title} " + "=" * 4)

    def record(self, result: Result, label: str) -> None:
        entry = result.brief()
        entry["label"] = label
        self.commands.append(entry)

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None


def run(
    argv: list[str],
    *,
    cwd: Path | str | None = None,
    env: dict | None = None,
    timeout: float = 600.0,
    stdin_data: bytes | None = None,
    log: Log | None = None,
    label: str = "",
    check: bool = False,
    full_capture: bool = False,
) -> Result:
    """Run a command with a hard timeout and fully captured output.

    The child gets a scrubbed environment by default: no inherited state means a
    submission cannot influence the verifier's own tooling through a variable it
    set during the agent phase.

    Output is truncated to MAX_CAPTURE unless full_capture is set.  Pass it when
    the child's stdout is the measurement rather than a log.
    """
    start = now()
    full_env = base_env() if env is None else env
    cap = None if full_capture else MAX_CAPTURE
    try:
        completed = subprocess.run(
            argv,
            cwd=str(cwd) if cwd else None,
            env=full_env,
            input=stdin_data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        result = Result(
            argv=list(argv),
            cwd=str(cwd or os.getcwd()),
            returncode=completed.returncode,
            stdout=completed.stdout[:cap],
            stderr=completed.stderr[:cap],
            duration=now() - start,
        )
    except subprocess.TimeoutExpired as exc:
        result = Result(
            argv=list(argv),
            cwd=str(cwd or os.getcwd()),
            returncode=-9,
            stdout=(exc.stdout or b"")[:cap],
            stderr=(exc.stderr or b"")[:cap],
            duration=now() - start,
            timed_out=True,
        )
    except (FileNotFoundError, PermissionError, OSError) as exc:
        result = Result(
            argv=list(argv),
            cwd=str(cwd or os.getcwd()),
            returncode=-1,
            stdout=b"",
            stderr=str(exc).encode(),
            duration=now() - start,
        )
    if log is not None:
        log.record(result, label or argv[0])
        status = "ok" if result.ok else f"rc={result.returncode}"
        if result.timed_out:
            status = "TIMEOUT"
        log.write(f"  $ {' '.join(argv[:6])}{' ...' if len(argv) > 6 else ''} -> {status}")
    if check and not result.ok:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(argv)}\n"
            f"{result.stderr.decode('utf-8', 'replace')[:4000]}"
        )
    return result


def run_streaming(
    argv: list[str],
    *,
    cwd: Path | str | None = None,
    env: dict | None = None,
    timeout: float = 600.0,
    idle_timeout: float | None = None,
    stdin_data: bytes | None = None,
    log: Log | None = None,
    label: str = "",
    full_capture: bool = False,
) -> Result:
    """`run`, plus a bound on how long the child may produce nothing.

    For a child whose stdout is a stream of self-delimiting records: it is killed
    when it exceeds `timeout` overall, as `run` does, and *also* when `idle_timeout`
    passes with no byte written.  Everything written before either kill is returned,
    so the caller can attribute the stall to the record that never arrived.

    Why not `run(timeout=idle_timeout)`: the wall clock cannot tell a child stuck on
    one case from a child working steadily through 250 of them.  Bounding the wall
    at 30s would fail every honest batch; bounding it at 300s lets one stalled case
    spend the batch's whole window, which is what cost lang02/high its `streaming`
    module.  Idleness separates the two without the runner having to know how long
    the work should take.

    A read loop rather than `subprocess.run`: the timeout has to be re-armed on each
    byte, so something has to observe the bytes as they arrive.

    Not a general replacement for `run`.  Everything here that runs one process per
    case, or that treats stdout as a log rather than as a stream, wants the simpler
    wall bound and keeps it.
    """
    start = now()
    full_env = base_env() if env is None else env
    cap = None if full_capture else MAX_CAPTURE
    if idle_timeout is None:
        idle_timeout = timeout

    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd) if cwd else None,
            env=full_env,
            stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            # The child of a JVM-hosted probe is not always the process doing the
            # work.  Killed by pid, a wrapper dies and its worker keeps the CPU
            # and the pipe; the read below would then see a pipe that never closes
            # on a process that is already gone.  Its own group makes the kill
            # cover whatever it started.
            start_new_session=True,
        )
    except (FileNotFoundError, PermissionError, OSError) as exc:
        return Result(argv=list(argv), cwd=str(cwd or os.getcwd()), returncode=-1,
                      stdout=b"", stderr=str(exc).encode(), duration=now() - start)

    # Resolved now, while the pid still names a process.  `proc.poll()` reaps the
    # child, and after that `os.getpgid(proc.pid)` raises ProcessLookupError -- so a
    # group kill attempted later silently does nothing, which is exactly the case
    # that needs it: the child has exited and something it started still holds the
    # pipe.  The group outlives the leader, so the saved id stays killable.
    # `start_new_session` makes the child its own group leader, hence pid == pgid;
    # asked for anyway rather than assumed, and falling back to the pid if the host
    # would not give it a session of its own.
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, PermissionError, OSError):
        pgid = proc.pid

    chunks: dict[int, list[bytes]] = {1: [], 2: []}
    last_output = [now()]
    lock = threading.Lock()

    def drain(stream, which: int) -> None:
        # read1() rather than read(): read() blocks until the buffer is full, so a
        # record flushed on its own would not be seen until 8 KB more arrived and
        # every case would look like a stall.
        try:
            while True:
                data = stream.read1(65536)
                if not data:
                    return
                with lock:
                    chunks[which].append(data)
                    last_output[0] = now()
        except (ValueError, OSError):
            return
        finally:
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass

    def feed() -> None:
        # In its own thread: the payload is larger than a pipe buffer, so writing it
        # inline deadlocks against a child that is already writing records back.
        try:
            proc.stdin.write(stdin_data)
            proc.stdin.close()
        except (BrokenPipeError, ValueError, OSError):
            pass

    threads = [threading.Thread(target=drain, args=(proc.stdout, 1), daemon=True),
               threading.Thread(target=drain, args=(proc.stderr, 2), daemon=True)]
    if stdin_data is not None:
        threads.append(threading.Thread(target=feed, daemon=True))
    for t in threads:
        t.start()

    timed_out = idle_hit = False
    deadline = start + timeout
    readers = threads[:2]
    while True:
        # Both conditions, not just the exit status.  A child that exits while
        # something it started still holds the write end leaves the pipes open, and
        # waiting only on the exit status returns here with the output incomplete
        # and no timeout recorded -- the reads then block on a pipe nothing will
        # close.  Waiting for the readers instead keeps the bounds below in force
        # over exactly the thing that can still hang.
        if proc.poll() is not None and not any(t.is_alive() for t in readers):
            break
        with lock:
            quiet_for = now() - last_output[0]
        if quiet_for >= idle_timeout:
            timed_out = idle_hit = True
            break
        if now() >= deadline:
            timed_out = True
            break
        # Wake often enough that the bound is the bound and not the bound plus a
        # poll interval, and never sleep past either limit.
        time.sleep(min(0.2, max(0.01, idle_timeout - quiet_for), max(0.01, deadline - now())))

    if timed_out:
        _kill_group(proc, pgid)
    proc.wait()
    # Let the readers drain what is already in the pipes, so a record written just
    # before the kill is not lost with the process.  If they are still blocked after
    # that, the write end is held by something the kill did not reach: killing the
    # group again once the child is reaped is the last thing that can close it, and
    # after that the pipes are abandoned rather than waited on -- a reader thread is
    # a daemon, and hanging here would cost the module its whole result file.
    for t in readers:
        t.join(timeout=2.0)
    if any(t.is_alive() for t in readers):
        _kill_group(proc, pgid)
        for t in readers:
            t.join(timeout=2.0)

    with lock:
        out, err = b"".join(chunks[1]), b"".join(chunks[2])
    result = Result(
        argv=list(argv),
        cwd=str(cwd or os.getcwd()),
        # -9 whether the kill was for idleness or for the wall clock: callers
        # branch on `timed_out`, and a distinct code here would be a second
        # encoding of the same fact for them to disagree about.
        returncode=-9 if timed_out else proc.returncode,
        stdout=out[:cap],
        stderr=err[:cap],
        duration=now() - start,
        timed_out=timed_out,
        idle_timeout_hit=idle_hit,
    )
    if log is not None:
        log.record(result, label or argv[0])
        status = "ok" if result.ok else f"rc={result.returncode}"
        if idle_hit:
            status = f"IDLE>{idle_timeout:g}s"
        elif timed_out:
            status = "TIMEOUT"
        log.write(f"  $ {' '.join(argv[:6])}{' ...' if len(argv) > 6 else ''} -> {status}")
    return result


def _kill_group(proc, pgid: int) -> None:
    """SIGKILL the process group captured at spawn, then the child itself.

    `pgid` is passed in rather than looked up here: by the time this is called the
    child may already have been reaped, and `os.getpgid` on a reaped pid raises
    instead of returning the group its children are still running in.

    Both signals, because a host that refused the new session leaves the child in
    this process's own group -- where killing the group would kill the verifier --
    so the group send is skipped in that case and only the child is signalled.
    Guarded throughout: a kill that finds nothing is the expected outcome half the
    time and must not become the module's reported error.
    """
    if pgid and pgid != os.getpgrp():
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        proc.kill()
    except (ProcessLookupError, PermissionError, OSError, ValueError):
        pass


def synthetic(label: str, ok: bool, detail: str = "") -> Result:
    """A Result for an observation that was made without running a command.

    Some findings -- a generated file existing, a probe being skipped because its
    prerequisite failed -- belong in the same ledger as command output so the
    report accounts for them uniformly.  Recording them as Results keeps one code
    path for consumption instead of two.
    """
    return Result(
        argv=["<observation>", label],
        cwd="",
        returncode=0 if ok else 1,
        stdout=detail.encode() if ok else b"",
        stderr=b"" if ok else detail.encode(),
        duration=0.0,
    )


def base_env(**overrides) -> dict:
    """A minimal, deterministic environment for child processes."""
    env = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": "/root",
        "LC_ALL": "C.UTF-8",
        "LANG": "C.UTF-8",
        "TZ": "UTC",
        "SOURCE_DATE_EPOCH": "0",
        "PYTHONHASHSEED": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
        # The JDK the build and the graded runs both use.  Set here rather than
        # inherited so a submission cannot be graded against a second JDK that
        # happened to be earlier on the image's PATH.
        "JAVA_HOME": "/usr/lib/jvm/java-17-openjdk-amd64",
        "TERM": "dumb",
    }
    env.update({k: v for k, v in overrides.items() if v is not None})
    return env


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, indent=1, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


def copy_tree(src: Path, dst: Path, *, skip_names: set[str] | None = None) -> int:
    """Copy a directory, skipping named entries, and count regular files.

    Symlinks are copied as symlinks, never followed: following one is how a
    submission could redirect the verifier's read of its own tree.
    """
    skip = skip_names or set()
    count = 0
    dst.mkdir(parents=True, exist_ok=True)
    for entry in sorted(src.iterdir(), key=lambda p: p.name):
        if entry.name in skip:
            continue
        target = dst / entry.name
        if entry.is_symlink():
            os.symlink(os.readlink(entry), target)
        elif entry.is_dir():
            count += copy_tree(entry, target, skip_names=skip)
        elif entry.is_file():
            shutil.copy2(entry, target)
            count += 1
    return count


@dataclass
class CaseOutcome:
    """One workflow's verdict, with enough context to be actionable."""

    case_id: str
    family: str
    kind: str
    passed: bool
    weight: float
    detail: str = ""
    diff: str = ""
    duration: float = 0.0
    # As on GateOutcome: the check had no answer because its evidence is absent,
    # which is a different statement from `passed=False`.  See vlib.NotApplicable.
    not_applicable: str = ""

    def to_dict(self) -> dict:
        payload = {
            "id": self.case_id,
            "family": self.family,
            "kind": self.kind,
            "passed": self.passed,
            "weight": self.weight,
        }
        if self.not_applicable:
            payload["not_applicable"] = self.not_applicable[:600]
        if self.detail:
            payload["detail"] = self.detail[:2000]
        if self.diff:
            payload["diff"] = self.diff[:2000]
        if self.duration:
            payload["duration_sec"] = round(self.duration, 4)
        return payload


@dataclass
class GateOutcome:
    """One audit gate's verdict."""

    gate_id: str
    check: str
    mandatory: bool
    passed: bool
    detail: str = ""
    evidence: list[str] = field(default_factory=list)
    # Set when the check could not be answered because the evidence it reads does
    # not exist in this install.  Distinct from `passed=False`, which asserts that
    # the evidence was read and disagreed.  See NotApplicable.
    not_applicable: str = ""

    def to_dict(self) -> dict:
        payload = {
            "id": self.gate_id,
            "check": self.check,
            "mandatory": self.mandatory,
            "passed": self.passed,
            "detail": self.detail[:4000],
            "evidence": [item[:600] for item in self.evidence[:40]],
        }
        if self.not_applicable:
            payload["not_applicable"] = self.not_applicable[:600]
        return payload


class NotApplicable(Exception):
    """The check's evidence does not exist in this install, so it has no answer.

    Two different statements were being made with one word.  "The jar does not
    export org.zlib" and "there is no jar" both arrived as `passed=False`, and the
    second is not a finding about the submission -- it is the absence of the thing
    the finding would have been about.  Eight gates made that worse by reporting a
    substantive conclusion for it: when the LD_PRELOAD observer could not run
    because there was no jar to run it against, `no-file-access` said "the
    compression paths reach the filesystem" and `no-env-dispatch` said "the
    library's behavior depends on the environment".  Both are assertions about
    behaviour that was never observed, and a reader of the report cannot tell them
    from the real thing.

    Raising this instead is what the two evaluators catch to say "not answered,
    and here is what was missing".  What the *caller* then does with it depends on
    the run, and only the caller knows:

      * Grading a submission -- the verdict is still a failure, because a
        submission that installed no jar has not delivered a Java library and the
        absence is its own doing.  The detail now names the absence rather than
        inventing a behavioural claim, which is the whole gain here.
      * The reference self-test (`c_self_test` below) -- the verdict is `skip`.
        The operator deliberately handed the stage a C install to find out whether
        the corpus is answerable at all, and "there is no jar" is a true statement
        about the operator's own choice rather than a defect in what they handed
        over.

    The reason is a sentence, not a code.  It ends up verbatim in the report, and
    a skip whose reason is `EVIDENCE_MISSING` would need this docstring to read.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def shim_enforced() -> bool:
    """Must a repository C source fail to compile in this run?

    True for every graded run, and the reason this is a function rather than one
    `os.environ` read is that the obvious spelling could not be switched off.

    suite.toml sets `SRB_SHIM = "enforce"` in its `[env]` table, and swerefactor's
    runner applies that table *over* the process environment -- `env = dict(
    os.environ)` then `env.update(suite.env)`.  So an operator who ran the stage
    with `SRB_SHIM=off`, exactly as suite.toml's own comment told them to, got
    `enforce` anyway: the shim refused every compile, State A built nothing, and
    the self-test that was supposed to prove the corpus answerable instead proved
    that the shim works.  Which was already asserted, by a gate.

    `SRB_SELFTEST=1` is therefore a second name for the same thing, deliberately
    one the suite does not pin.  It cannot weaken a graded run: the grading harness
    passes the environment it was given, and a submission has no way to put a
    variable into it.  The safe direction is also the default -- unset, or any value
    other than the four spellings below, enforces.
    """
    if os.environ.get("SRB_SELFTEST", "") in ("1", "yes", "true", "on"):
        return False
    return os.environ.get("SRB_SHIM", "enforce") == "enforce"


def c_self_test(prefixes: "list[Path]", jar_relpath: str) -> str:
    """Is this the operator's reference self-test rather than a graded submission?

    Returns the reason to record when it is, and "" when it is not, so a caller
    can write `if reason := c_self_test(...)` and have the explanation in hand.

    Three conditions, all three of them the operator's and none of them a
    submission's:

      1. The shim is not enforced -- see shim_enforced().  Under enforcement, which
         is every graded run, ccshim refuses every compile of a repository C source,
         so no submission can produce a C zlib to be mistaken for this.  This alone
         makes the branch unreachable while grading.
      2. The install tree holds a C zlib: `include/zlib.h` and a `libz` object.
         A Java submission does not install those, and one that did would fail
         `install/no-headers` and `install/no-native-lib` for it.
      3. No jar at the published path.  A submission that shipped both would be
         graded as a submission, on the jar.

    The point of the third is that this predicate can never *lower* a real
    submission's score: the only runs it can affect are runs with nothing to grade.
    """
    if shim_enforced():
        return ""
    if not prefixes:
        return ""
    for prefix in prefixes:
        if (prefix / jar_relpath).exists():
            return ""
        if not (prefix / "include" / "zlib.h").is_file():
            return ""
        libdir = prefix / "lib"
        if not any((libdir / name).exists()
                   for name in ("libz.so", "libz.so.1", "libz.a")):
            return ""
    return ("this run installed a C zlib and no jar, with the compiler shim off: "
            "it is the reference self-test of the corpus, not a submission")


def unified_diff(expected: bytes, actual: bytes, *, limit: int = 40) -> str:
    """A short, readable diff for the report.

    Byte-level when the difference is not visible at line level, because a
    trailing-newline or CR difference is a real failure that a line diff would
    render as identical text.
    """
    import difflib

    if expected == actual:
        return ""
    exp_text = expected.decode("utf-8", "replace").splitlines(keepends=True)
    act_text = actual.decode("utf-8", "replace").splitlines(keepends=True)
    lines = list(
        difflib.unified_diff(
            exp_text, act_text, fromfile="reference", tofile="submission", n=1
        )
    )
    if not lines:
        return (
            f"byte-level difference only: reference {len(expected)} bytes "
            f"sha={sha256_bytes(expected)[:16]}, submission {len(actual)} bytes "
            f"sha={sha256_bytes(actual)[:16]}"
        )
    out = "".join(lines[:limit])
    if len(lines) > limit:
        out += f"...[{len(lines) - limit} more diff lines]\n"
    return out
