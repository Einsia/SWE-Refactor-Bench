#!/usr/bin/env python3
"""Shared plumbing for the lang07-jsonata-js-to-typescript verifier.

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

    full_capture uncaps stdout only.  stderr stays capped either way: it is a log
    in every caller, and a submission that writes gigabytes to it should not get
    them retained in a Result that then gets written to the transcript.  Note the
    cap never limited the *read* -- subprocess.run has buffered both streams in
    full before this runs -- so it governs only what is kept.
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
            # stderr is a log even when stdout is data; see full_capture.
            stderr=completed.stderr[:MAX_CAPTURE],
            duration=now() - start,
        )
    except subprocess.TimeoutExpired as exc:
        result = Result(
            argv=list(argv),
            cwd=str(cwd or os.getcwd()),
            returncode=-9,
            stdout=(exc.stdout or b"")[:cap],
            stderr=(exc.stderr or b"")[:MAX_CAPTURE],
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
        # npm is told it is offline rather than left to discover it by timing out
        # against a registry that is not routable.  A submission that added a
        # dependency then fails with "cannot resolve" in a second instead of
        # spending the module's whole timeout on DNS.
        "npm_config_offline": "true",
        "npm_config_audit": "false",
        "npm_config_fund": "false",
        "npm_config_update_notifier": "false",
        # Node's own determinism knobs.  The probe hashes nothing and iterates no
        # sets, but a submission's port might, and a graded run that depends on
        # hash order is one that changes verdict between two identical images.
        "NODE_OPTIONS": "",
        "TERM": "dumb",
    }
    # Deliberately absent: NODE_PATH.  Setting it would let a submission's
    # `node_modules` satisfy a require the port itself should satisfy, and
    # leaving it unset is not enough on its own -- `base_env` replaces the
    # environment rather than extending it, which is what actually keeps a
    # variable the agent phase exported out of the graded run.
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

    def to_dict(self) -> dict:
        payload = {
            "id": self.case_id,
            "family": self.family,
            "kind": self.kind,
            "passed": self.passed,
            "weight": self.weight,
        }
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
