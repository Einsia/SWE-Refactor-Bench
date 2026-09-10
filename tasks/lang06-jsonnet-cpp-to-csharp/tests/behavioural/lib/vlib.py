#!/usr/bin/env python3
"""Shared plumbing for the lang06-jsonnet-cpp-to-csharp verifier.

Process execution, logging and the evidence ledger.  Every command the verifier
runs goes through `run()` so that the transcript in the report accounts for all
of it -- a reader can reconstruct exactly what was executed, in what
environment, and what came back.

Note that graded *cases* do not come through here.  They go through
executor.run(), which is shared with the freeze step so that a case is executed
the same way when its expectation is recorded and when a submission is measured
against it.  This module is for the verifier's own commands: `dotnet publish`,
the metadata inspector, and anything else whose output is a log rather than a
measurement.
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

# The uid every submission-authored process runs as.
#
# The answers live at /opt/assets/expectations.json: 2,602 cases with the exact
# stdout, stderr, exit status and file effects the C++ reference produced.  Grading
# reads that file; the submission must not.  Both halves of the submission run
# inside this image -- `dotnet publish` builds its source, and the published CLI is
# then executed once per case -- so "the submission" here means MSBuild and the
# binary it produces, and both are covered.
#
# Mode 444 was the guard before this: it stops a write, and a leak is a read.  An
# MSBuild target assembling the path from fragments read the key at build time and
# emitted whatever it liked, with no path literal anywhere in the source for a scan
# to find.  A uid boundary answers that regardless of how the path is spelled,
# because it does not depend on recognising the attempt.
#
# 65534 (nobody/nogroup) rather than a user added by the Dockerfile: it exists in
# every base this task might sit on, owns nothing, and is what the image already
# had.  Numeric because passwd lookups are one more thing that can be absent.
#
# Imported from executor rather than restated: executor is the module that stays
# stdlib-only and is imported directly by the authoring probes, so it is the one
# place that can hold this without dragging the rest of the suite along.  Every
# entry point that reaches vlib has already put this directory on sys.path
# (driver.py, build.py, and freeze.py each do it before their sibling imports),
# which is what makes a sibling import here safe under `python3 -I`.
from executor import UNPRIV_GID, UNPRIV_UID  # noqa: E402


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
    unprivileged: bool = False,
) -> Result:
    """Run a command with a hard timeout and fully captured output.

    The child gets a scrubbed environment by default: no inherited state means a
    submission cannot influence the verifier's own tooling through a variable it
    set during the agent phase.

    Output is truncated to MAX_CAPTURE unless full_capture is set.  Pass it when
    the child's stdout is the measurement rather than a log.

    `unprivileged=True` drops the child to UNPRIV_UID, which is how a command that
    runs submission-authored code is kept away from /opt/assets.  The caller is
    responsible for the child's writable paths -- see grant_unprivileged().
    """
    start = now()
    full_env = base_env() if env is None else env
    cap = None if full_capture else MAX_CAPTURE
    priv: dict = {}
    if unprivileged:
        # Refuse rather than silently run as root.  A boundary that quietly does
        # nothing when it cannot be applied is worse than no boundary: the run
        # still passes, so nothing ever reports that the answers were reachable.
        if os.geteuid() != 0:
            raise RuntimeError(
                f"unprivileged=True needs root to drop from, running as uid "
                f"{os.geteuid()}")
        # extra_groups=[] is not decoration.  Without it subprocess never calls
        # setgroups, so the child keeps root's supplementary groups and `id` reports
        # `groups=65534(nogroup),0(root)`: a process in group root, which is no drop
        # at all for anything root-group readable.  It also broke the build, because
        # POSIX checks the first matching class only -- see ensure_traversable().
        priv = {"user": UNPRIV_UID, "group": UNPRIV_GID, "extra_groups": []}
    try:
        completed = subprocess.run(
            argv,
            cwd=str(cwd) if cwd else None,
            env=full_env,
            input=stdin_data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            **priv,
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


def ensure_traversable(path: Path | str) -> None:
    """Give UNPRIV_UID the +x it needs on every directory above `path`.

    Owning a tree is not enough to reach it.  `tempfile.mkdtemp()` creates its
    directory 0700 and root-owned, so a child dropped to UNPRIV_UID cannot traverse
    into a repo underneath one -- and the symptom is not a permission error.  MSBuild
    resolves the project path against a cwd it cannot enter and reports

        MSBUILD : error MSB1009: Project file does not exist.

    for a file that discovery had listed one line earlier.  That is what this
    prevents, and the reason it is a separate function from the chown below: the
    ancestors are not ours to own, only to walk through.

    +x on directories only, never +r: traversal lets the child reach a path it was
    given, and nothing more.  A directory it can walk but not list cannot be
    enumerated for anything it was not handed directly.

    Both g+x and o+x, because POSIX stops at the first class that matches.  A child
    whose gid is the directory's gid is checked against the group bits *only* -- the
    other bits are never consulted, so o+x alone leaves a 0701 root:root directory
    untraversable by anything in group root.  See `run()` on extra_groups for the
    other half of that, which is what made this reachable at all.
    """
    for parent in list(Path(path).resolve().parents)[:-1]:  # stop before "/"
        try:
            mode = parent.stat().st_mode & 0o7777
            if mode & 0o011 != 0o011:
                os.chmod(parent, mode | 0o011)
        except OSError:
            # An unreachable ancestor is either already traversable or outside our
            # control; the caller's own run will report the real failure.
            pass


def grant_unprivileged(*paths: Path | str, recursive: bool = True) -> None:
    """Make paths usable by UNPRIV_UID.

    chown rather than a world-writable mode: `dotnet publish` writes obj/ and bin/
    inside the tree it is given, and the case runner writes files next to a case's
    inputs, so those trees need an owner that can write them rather than a mode that
    lets anyone.  Grading itself stays root and keeps reading everything.

    Missing paths are skipped instead of raising.  Callers grant a set of paths that
    depends on what a submission's layout turned out to be, and a path that does not
    exist needs no permission.
    """
    for p in paths:
        root = Path(p)
        if not root.exists():
            continue
        ensure_traversable(root)
        try:
            os.chown(root, UNPRIV_UID, UNPRIV_GID)
            if recursive and root.is_dir():
                for dirpath, dirnames, names in os.walk(root):
                    for n in dirnames + names:
                        full = os.path.join(dirpath, n)
                        try:
                            os.chown(full, UNPRIV_UID, UNPRIV_GID,
                                     follow_symlinks=False)
                        except OSError:
                            # A single unchownable entry is not fatal: it is either
                            # already usable or not needed.  Failing the whole grade
                            # over one of them would turn a permissions detail into
                            # a zero.
                            pass
        except OSError:
            pass


UNPRIV_HOME = Path("/tmp/unpriv-home")


def unprivileged_env(**overrides) -> dict:
    """base_env, with the paths a dropped child needs to be able to write.

    base_env hands out HOME=/root, which UNPRIV_UID cannot write.  The SDK's first
    action is to write into its home, so a publish that inherited /root fails on a
    permission error that reads like a broken submission.  This points both homes at
    a directory owned by that uid and creates it.
    """
    UNPRIV_HOME.mkdir(parents=True, exist_ok=True)
    grant_unprivileged(UNPRIV_HOME)
    env = base_env(HOME=str(UNPRIV_HOME), DOTNET_CLI_HOME=str(UNPRIV_HOME))
    env.update({k: v for k, v in overrides.items() if v is not None})
    return env


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
        # The submission's build must not reach a network, and must not decide
        # anything from the ambient locale: InvariantGlobalization is in the
        # contract, and a build that passed here because the container happened
        # to have ICU would fail on a machine that did not.
        "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
        "DOTNET_NOLOGO": "1",
        "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1",
        "NUGET_PACKAGES": "/opt/nuget-offline",
        # dotnet writes to $HOME on first run and fails the build if it cannot.
        "DOTNET_CLI_HOME": "/tmp/dotnet-home",
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
