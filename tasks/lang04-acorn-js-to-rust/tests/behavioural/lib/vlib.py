#!/usr/bin/env python3
"""Shared plumbing for the lang04-acorn-js-to-rust verifier.

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
import subprocess
import sys
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

    @classmethod
    def from_brief(cls, payload: dict) -> "Result":
        """Rebuild a Result from what `brief()` wrote.

        The behavioural suite runs one module per process, so the build's results
        have to survive as JSON between them.  What comes back is lossy in one
        way that matters and is therefore stated here: `brief()` clips long
        output, so a rehydrated Result's stdout may be shorter than the original
        and must not be used for byte comparison.  Every consumer of a rehydrated
        Result reads `ok`, `returncode` or `tail()`, all of which survive.
        """
        return cls(
            argv=list(payload.get("argv") or []),
            cwd=str(payload.get("cwd") or ""),
            returncode=int(payload.get("returncode", 1)),
            stdout=str(payload.get("stdout") or "").encode("utf-8", "replace"),
            stderr=str(payload.get("stderr") or "").encode("utf-8", "replace"),
            duration=float(payload.get("duration_sec") or 0.0),
            timed_out=bool(payload.get("timed_out")),
        )


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
        "CARGO_NET_OFFLINE": "true",
        "CARGO_HOME": "/usr/local/cargo",
        "RUSTUP_HOME": "/usr/local/rustup",
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
    #: Why this case was not asked, when it was not.  Two things set it, and both
    #: mean "the question does not exist", not "the answer was wrong":
    #:
    #:   - structure.JS_STRUCT_SKIP_GROUPS: the evidence the case reads does not
    #:     exist in the tree being graded.
    #:   - freeze.check_unportable, via `probe.Runner.run_corpus`: the frozen
    #:     answer is a crash inside the reference, and the message the response
    #:     schema requires is the engine's wording for the shape of the code that
    #:     crashed rather than anything acorn defines.
    #:
    #: `passed` stays False, because a case that was never asked did not pass.
    #: What this adds is that it was not asked, which is a different statement
    #: from asked-and-wrong: the driver turns it into a `skip` verdict, and a
    #: skip leaves the module's denominator instead of costing it weight.  A
    #: reason rather than a flag, because it reaches the report verbatim and a
    #: reader should not have to come here to find out what was missing.
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
            payload["not_applicable"] = self.not_applicable[:400]
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


def first_difference(expected: bytes, actual: bytes) -> tuple[int, str]:
    """Offset of the first differing byte, with the bytes either side of it.

    A whole-response diff is useless when the response is a 13 MB AST: the
    interesting fact is where the two first disagree and what each said there.
    Returns (-1, "") when they are equal.
    """
    if expected == actual:
        return -1, ""
    n = min(len(expected), len(actual))
    i = 0
    # Compare a block at a time -- byte-at-a-time over 13 MB in Python is slow
    # enough to matter across thousands of cases.
    BLOCK = 4096
    while i < n:
        end = min(i + BLOCK, n)
        if expected[i:end] != actual[i:end]:
            while i < end and expected[i] == actual[i]:
                i += 1
            break
        i = end
    lo = max(0, i - 40)
    exp_ctx = expected[lo : i + 40].decode("utf-8", "replace")
    act_ctx = actual[lo : i + 40].decode("utf-8", "replace")
    if i >= n:
        return i, (
            f"at byte {i}: reference is {len(expected)} bytes, submission is "
            f"{len(actual)} bytes; the shorter is a prefix of the longer"
        )
    return i, (
        f"at byte {i}:\n"
        f"  reference  ...{exp_ctx}...\n"
        f"  submission ...{act_ctx}..."
    )


def read_ndjson_line(stream, *, limit: int = 512 * 1024 * 1024) -> bytes | None:
    """Read one newline-terminated record from a binary stream.

    `stream.readline()` is fine for this, but a probe that never emits a newline
    would make it block until the pipe closes and then return a partial line
    indistinguishable from a complete one.  Reading with an explicit cap and
    reporting the truncation separately keeps "no response" distinct from "a
    response that happened to be huge" -- which matters because a fixture
    response legitimately is tens of megabytes.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = stream.readline(1 << 20)
        if not chunk:
            if not chunks:
                return None
            # EOF without a newline: an incomplete final record.
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        if chunk.endswith(b"\n"):
            return b"".join(chunks)[:-1]
        if total > limit:
            raise RuntimeError(f"response exceeded {limit} bytes with no newline")


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
