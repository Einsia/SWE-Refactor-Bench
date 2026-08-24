#!/usr/bin/env python3
"""Plumbing shared by every verifier module.

Nothing in here knows anything about SQL.  It is process execution, hashing,
atomic writes and diffing -- the parts that have to be boring and identical
everywhere so that a failure is always attributable to the thing being graded
rather than to the harness.

The one domain fact that does live here is base_env: the Go toolchain reads a
dozen environment variables, and a build that picked up GOFLAGS or GOPROXY from
the ambient environment would not be the build the contract describes.  Pinning
them in one place means every module -- freeze, build, structure, audit --
runs Go the same way.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

LOG_DIR = Path("/logs/verifier")

# Captured output is capped unless the caller asks for all of it: a submission
# that prints a megabyte per invocation must not be able to produce a gigabyte
# report.  Measurement is the exception -- see full_capture in run().
MAX_CAPTURE = 256 * 1024

# The interpreter running the verifier, by absolute path.  Every verifier-owned
# Python subprocess is started through this rather than through a bare `python3`,
# because the graded build puts a refusing shim at the front of PATH under that
# exact name: a PATH lookup would find the shim and the verifier would shim
# itself.  It is also what the shim's own shebang is rewritten to name.
PYTHON = sys.executable


def now() -> float:
    return time.monotonic()


@dataclass
class Result:
    """One completed subprocess."""

    argv: list[str]
    cwd: str
    returncode: int
    stdout: bytes
    stderr: bytes
    duration: float
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def text(self) -> str:
        return self.stdout.decode("utf-8", "replace")

    def err_text(self) -> str:
        return self.stderr.decode("utf-8", "replace")

    def tail(self, lines: int = 12, limit: int = 1200) -> str:
        """The end of the output, which is where a tool says why it failed.

        stderr first: `go build` writes its diagnostics there and nothing to
        stdout, so a stdout-first reader would report a compile failure as an
        empty string.
        """
        blob = (self.stderr or b"").decode("utf-8", "replace").rstrip()
        if not blob:
            blob = (self.stdout or b"").decode("utf-8", "replace").rstrip()
        excerpt = " | ".join(blob.splitlines()[-lines:])
        return excerpt[-limit:]

    def brief(self, limit: int = 4000) -> dict:
        def clip_bytes(data: bytes) -> str:
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
            "stdout": clip_bytes(self.stdout),
            "stderr": clip_bytes(self.stderr),
            "stdout_bytes": len(self.stdout),
            "stderr_bytes": len(self.stderr),
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
        self.write("==== " + title + " ====")

    def record(self, result: Result, label: str) -> None:
        entry = result.brief()
        entry["label"] = label
        self.commands.append(entry)

    def dump(self, path: Path) -> None:
        write_json(path, {"commands": self.commands})

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None


def base_env(**overrides) -> dict:
    """A scrubbed, deterministic environment.

    Locale, timezone and hash seed are pinned: a submission must not be able to
    pass or fail depending on the ambient environment, and neither may the
    reference.  PATH is explicit rather than inherited.

    The Go block is what makes a build reproducible and offline.  GOFLAGS
    -mod=mod is the contract's value; GOPROXY=off and GOSUMDB=off mean a missing
    dependency is a build failure rather than a network fetch; GOTOOLCHAIN=local
    stops a `toolchain` line in the submission's go.mod from asking for a
    different Go than the image has.  A submission cannot relax any of these,
    because the verifier passes this environment rather than inheriting one.
    """
    env = {
        "PATH": "/usr/local/go/bin:/usr/local/sbin:/usr/local/bin:"
                "/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": os.environ.get("HOME", "/root"),
        "LC_ALL": "C.UTF-8",
        "LANG": "C.UTF-8",
        "TZ": "UTC",
        "PYTHONHASHSEED": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
        "SOURCE_DATE_EPOCH": "946684800",  # 2000-01-01
        "TERM": "dumb",
        "NO_COLOR": "1",
        "CGO_ENABLED": "0",
        "GOPROXY": "off",
        "GOSUMDB": "off",
        "GONOSUMDB": "*",
        "GOFLAGS": "-mod=mod",
        "GOTOOLCHAIN": "local",
        "GOTELEMETRY": "off",
        "GOTELEMETRYDIR": "/tmp/gotelemetry",
        "GOPATH": os.environ.get("GOPATH", "/root/go"),
        "GOCACHE": os.environ.get("GOCACHE", "/root/.cache/go-build"),
        "GOMODCACHE": os.environ.get("GOMODCACHE", "/root/go/pkg/mod"),
    }
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = str(value)
    return env


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
    """Run a command with a hard timeout and captured output, never raising.

    The child gets a scrubbed environment by default: no inherited state means a
    submission cannot influence the verifier's tooling through a variable it set
    during the agent phase.

    full_capture is for measurement.  Output that is compared against a frozen
    expectation must never be truncated, or the comparison is a lie -- and a
    truncated probe stream is indistinguishable from a crashed probe, which
    would make the executor invent a victim case.
    """
    start = now()
    full_env = base_env() if env is None else env
    cap = None if full_capture else MAX_CAPTURE
    try:
        completed = subprocess.run(
            list(argv),
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
            returncode=124,
            stdout=(exc.stdout or b"")[:cap],
            stderr=(exc.stderr or b"")[:cap] + b"\n[verifier] timed out\n",
            duration=now() - start,
            timed_out=True,
        )
    except (FileNotFoundError, PermissionError, OSError) as exc:
        result = Result(
            argv=list(argv),
            cwd=str(cwd or os.getcwd()),
            returncode=127,
            stdout=b"",
            stderr=f"[verifier] {type(exc).__name__}: {exc}".encode(),
            duration=now() - start,
        )
    if log is not None:
        log.record(result, label or argv[0])
        status = "ok" if result.ok else f"rc={result.returncode}"
        if result.timed_out:
            status = "TIMEOUT"
        shown = " ".join(argv[:6])
        log.write(f"  $ {shown}{' ...' if len(argv) > 6 else ''} -> {status}")
    if check and not result.ok:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(argv)}\n"
            f"{result.stderr.decode('utf-8', 'replace')[:4000]}"
        )
    return result


def generated(label: str, ok: bool, detail: str = "") -> Result:
    """A Result for an observation made without running a command.

    Some findings -- a file existing, a probe skipped because its prerequisite
    failed -- belong in the same ledger as command output, so the report
    accounts for them uniformly instead of through a second code path.
    """
    return Result(
        argv=["<observation>", label],
        cwd="",
        returncode=0 if ok else 1,
        stdout=detail.encode() if ok else b"",
        stderr=b"" if ok else detail.encode(),
        duration=0.0,
    )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_json(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


# The mode an atomic write lands on.  tempfile.mkstemp creates 0600 and
# os.replace preserves it, so without this every file written through the two
# helpers below would be owner-only while Path.write_bytes (expectations.bin) and
# copy_tree (the staged inputs) produce 0644 -- the mode of a frozen answer would
# depend on which helper happened to write it.  Measured: catalog.json came out
# 0600, and the verifier image's read-only seal saw 0400 where the sibling assets
# were 0444.  0644 is what a plain open() would have produced; umask still applies.
ATOMIC_MODE = 0o644


def current_umask() -> int:
    """The process umask, read without leaving it changed.

    POSIX offers no way to read the umask without setting it, so this sets and
    restores.  Both callers are single-threaded (freeze at image build, verify at
    grading), which is what makes the round trip safe here.
    """
    mask = os.umask(0o022)
    os.umask(mask)
    return mask


def write_json(path: Path, payload: object) -> None:
    """Write JSON atomically, so a killed verifier never leaves half a file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=1, sort_keys=True, ensure_ascii=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, ATOMIC_MODE & ~current_umask())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_text(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, ATOMIC_MODE & ~current_umask())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def copy_tree(src: Path, dst: Path, *, skip_names: Iterable[str] = ()) -> int:
    """Copy a directory tree, copying symlinks as symlinks.

    Following symlinks would let a submission point at something outside the
    workspace and have the verifier copy it in.  Returns the file count.
    """
    skip = set(skip_names)
    count = 0
    dst = Path(dst)
    dst.mkdir(parents=True, exist_ok=True)
    for root, dirs, files in os.walk(src, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in skip)
        rel = Path(root).relative_to(src)
        (dst / rel).mkdir(parents=True, exist_ok=True)
        for name in sorted(files):
            source = Path(root) / name
            target = dst / rel / name
            if source.is_symlink():
                link = os.readlink(source)
                if target.exists() or target.is_symlink():
                    target.unlink()
                os.symlink(link, target)
            elif source.is_file():
                shutil.copy2(source, target)
            else:
                continue  # sockets, fifos and devices are not copied
            count += 1
    return count


@dataclass
class CaseOutcome:
    """One graded case's verdict, with enough context to be actionable."""

    case_id: str
    family: str
    kind: str
    passed: bool
    weight: float
    detail: str = ""
    diff: str = ""
    duration: float = 0.0
    #: The case was not asked, because the tree cannot be asked it.  A reporting
    #: distinction and no longer a scoring one: `pooled` in result.py keeps a
    #: skipped case in the denominator and scores it 0, exactly as a failure, so
    #: this says *why* the answer is missing without changing what it costs.  Set
    #: only where the reason is a property of the submission's language rather than
    #: of its behaviour -- an ELF header cannot be read off a Python package -- and
    #: that is a property the submission chose, since writing the Go tree this reads
    #: was the task.
    skipped: bool = False
    #: Why it was skipped.  Carried separately from `detail` so a report can say
    #: "not applicable, because" without the sentence reading like a diagnosis.
    skip_reason: str = ""

    def to_dict(self) -> dict:
        payload = {
            "id": self.case_id,
            "family": self.family,
            "kind": self.kind,
            "passed": self.passed,
            "weight": self.weight,
        }
        if self.skipped:
            payload["skipped"] = True
            payload["skip_reason"] = self.skip_reason[:2000]
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

    def to_dict(self) -> dict:
        return {
            "id": self.gate_id,
            "check": self.check,
            "mandatory": self.mandatory,
            "passed": self.passed,
            "detail": self.detail[:4000],
            "evidence": [item[:600] for item in self.evidence[:40]],
        }


def unified_diff(expected: bytes, actual: bytes, *, limit: int = 40) -> str:
    """A short, readable diff, falling back to bytes when lines agree.

    A trailing-newline or CR difference is a real failure that a line diff
    renders as identical text, so that case reports digests instead.
    """
    if expected == actual:
        return ""
    exp = expected.decode("utf-8", "replace").splitlines(keepends=True)
    act = actual.decode("utf-8", "replace").splitlines(keepends=True)
    lines = list(
        difflib.unified_diff(exp, act, fromfile="reference", tofile="submission", n=1)
    )
    if not lines:
        return (
            f"byte-level difference only: reference {len(expected)} bytes "
            f"sha={sha256_bytes(expected)[:16]}, submission {len(actual)} bytes "
            f"sha={sha256_bytes(actual)[:16]}\n"
            f"reference[:120]={expected[:120]!r}\n"
            f"submission[:120]={actual[:120]!r}"
        )
    out = "".join(lines[:limit])
    if len(lines) > limit:
        out += f"...[{len(lines) - limit} more diff lines]\n"
    return out


def clip(text: str, limit: int = 4096) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [{len(text) - limit} more characters]"
