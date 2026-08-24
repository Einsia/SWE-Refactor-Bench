"""One execution path, two targets.

The reference side drives native ``sqlite3`` built from the pre-migration tree;
the submission side drives the delivered ``.wasm`` under ``wasmtime``.  Both go
through this module, from the same ``Case`` objects, because a second code path is
a second thing that can disagree with the first -- and a disagreement there would
look exactly like a failing port.  Both run in the same container, minutes apart,
so neither answer is a recording of anything and there is no third artefact whose
freshness someone has to guarantee.

Three properties this file is responsible for:

**A fresh sandbox per case.**  Every case gets its own directory, created empty
and removed afterwards.  Cases therefore cannot see each other's databases, and
running a subset gives the same answers as running everything.

**A fixed denominator.**  The score divides by ``catalog.EXPECTED``, never by the
number of cases that happened to run.  If a case cannot even be executed it
scores zero; breaking one can never raise a score.

**Symmetric normalisation.**  The sandbox path is rewritten to ``/data`` on both
sides, so the absolute path the native reference necessarily sees does not leak
into an expectation that the wasm target -- which only ever sees ``/data`` --
would then fail.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from typing import Any

import normalise
from case import Case

#: Guest paths the invocation contract preopens.  These are part of the task, not
#: an implementation detail: instruction.md states them, and the port has to work
#: with exactly this much of a filesystem.
GUEST_DATA = "/data"
GUEST_TMP = "/tmp"

#: A run inherits nothing from the build host.  LANG/LC_ALL/TZ pin collation and
#: date rendering; PATH is minimal so nothing is picked up implicitly.
BASE_ENV = {
    "LANG": "C",
    "LC_ALL": "C",
    "TZ": "UTC",
    "PATH": "/usr/bin:/bin",
    "HOME": "/tmp",
    "TMPDIR": GUEST_TMP,
    "SQLITE_TMPDIR": GUEST_TMP,
}


class RunnerError(RuntimeError):
    pass


def _substitute(text: str, mapping: dict[str, str]) -> str:
    """Replace every key with its value in a single pass.

    Single-pass matters here rather than being a nicety: the host sandbox lives
    *under* the host's temp directory, so its path contains the literal
    ``/tmp``.  Two sequential ``str.replace`` calls would rewrite text that the
    first call had just inserted, turning ``/data/db.sqlite`` into a path with
    the temp root spliced through the middle of it.  Longest key first so that a
    key which is a prefix of another cannot win.
    """
    if not mapping:
        return text
    pattern = re.compile(
        "|".join(re.escape(k) for k in sorted(mapping, key=len, reverse=True))
    )
    return pattern.sub(lambda m: mapping[m.group(0)], text)


def decode(raw: bytes) -> str:
    """Bytes to text without losing any of them.

    ``.binary on`` and ``select x'00ff41'`` make stdout arbitrary bytes, so a
    strict decode raises and a replacing decode is worse than raising: it maps
    distinct byte strings onto the same text, which would silently equalise two
    streams that differ.  ``surrogateescape`` is a bijection over the bytes that
    are not valid UTF-8, so the comparison stays exact and the readable case
    stays readable.
    """
    return raw.decode("utf-8", errors="surrogateescape")


def enc_text(text: str) -> Any:
    """Serialise text that may hold surrogate-escaped bytes.

    JSON cannot carry a lone surrogate, so the rare stream that needs them is
    stored as base64 under an explicit tag.  The common case stays a plain
    string, because a ledger nobody can read is a ledger nobody will check.
    """
    raw = text.encode("utf-8", errors="surrogateescape")
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return {"b64": base64.b64encode(raw).decode("ascii")}
    return text


def dec_text(value: Any) -> str:
    if isinstance(value, dict):
        return decode(base64.b64decode(value["b64"]))
    return value


@dataclass(frozen=True)
class Observation:
    """What a run produced, after normalisation.  Comparable and serialisable."""

    stdout: str
    stderr: str
    exit_code: int
    files: dict[str, str]
    native_read: str | None = None

    def to_json(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "stdout": enc_text(self.stdout),
            "stderr": enc_text(self.stderr),
            "exit": self.exit_code,
            "files": self.files,
        }
        if self.native_read is not None:
            d["native_read"] = enc_text(self.native_read)
        return d

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Observation:
        raw = d.get("native_read")
        return cls(
            stdout=dec_text(d["stdout"]),
            stderr=dec_text(d["stderr"]),
            exit_code=d["exit"],
            files=d.get("files", {}),
            native_read=None if raw is None else dec_text(raw),
        )

    def select(self, checks: tuple[str, ...]) -> dict[str, Any]:
        """Only the parts of the observation this case actually asserts."""
        out: dict[str, Any] = {}
        if "stdout" in checks:
            out["stdout"] = self.stdout
        if "stderr" in checks:
            out["stderr"] = self.stderr
        if "exit" in checks:
            out["exit"] = self.exit_code
        if "files" in checks:
            out["files"] = self.files
        if "native_read" in checks:
            out["native_read"] = self.native_read
        return out


# --------------------------------------------------------------------------
# Targets
# --------------------------------------------------------------------------
class Target:
    """How to invoke one implementation of the sqlite3 CLI."""

    name = "abstract"

    def argv(self, case: Case, sandbox: str, tmpdir: str) -> list[str]:
        raise NotImplementedError

    def rewrite(self, text: str, sandbox: str, tmpdir: str) -> str:
        """Map guest paths onto whatever this target can actually reach."""
        return text


class NativeTarget(Target):
    """The reference: native sqlite3 3.31.1, reachable at a real path.

    Native has no preopen indirection, so the guest paths in the case are
    rewritten to the sandbox's real paths on the way in, and rewritten back on
    the way out.  The round trip is what keeps the reference's answer free of the
    container's directory layout, which the wasm side never sees.
    """

    name = "native"

    def __init__(self, binary: str) -> None:
        self.binary = binary

    def argv(self, case: Case, sandbox: str, tmpdir: str) -> list[str]:
        return [self.binary, *(self.rewrite(a, sandbox, tmpdir) for a in case.argv)]

    def rewrite(self, text: str, sandbox: str, tmpdir: str) -> str:
        return _substitute(text, {GUEST_DATA: sandbox, GUEST_TMP: tmpdir})


class WasmTarget(Target):
    """The submission: a wasm32-wasi module under wasmtime.

    The preopens are the whole filesystem contract.  Nothing outside them is
    reachable, which is why the sandbox-escape cases in cases_platform can assert a
    clean failure rather than hoping for one.
    """

    name = "wasm"

    def __init__(self, wasmtime: str, module: str, extra: tuple[str, ...] = ()) -> None:
        self.wasmtime = wasmtime
        self.module = module
        self.extra = extra

    def argv(self, case: Case, sandbox: str, tmpdir: str) -> list[str]:
        cmd = [
            self.wasmtime, "run",
            "--dir", f"{sandbox}::{GUEST_DATA}",
            "--dir", f"{tmpdir}::{GUEST_TMP}",
            # The sandbox is preopened a second time as "." so a *relative* path
            # has something to resolve against.  Without it a bare "ar.db" fails
            # where the reference -- which runs with cwd set to the sandbox --
            # succeeds, and commands built on relative names (.archive, fsdir,
            # .import) would be untestable.  This mirrors the reference's cwd
            # rather than granting anything extra: it is the same directory.
            "--dir", f"{sandbox}::.",
        ]
        for mount in case.mounts:
            host = os.path.join(sandbox, mount.lstrip("/"))
            os.makedirs(host, exist_ok=True)
            cmd += ["--dir", f"{host}::{mount}"]
        for key, value in sorted({**BASE_ENV, **dict(case.env)}.items()):
            cmd += ["--env", f"{key}={value}"]
        cmd += [*self.extra, self.module, *case.argv]
        return cmd


# --------------------------------------------------------------------------
# Sandbox handling
# --------------------------------------------------------------------------
def _force_rmtree(path: str) -> None:
    """Remove a tree even if a case left something read-only behind."""

    def onerror(func, target, exc_info):  # noqa: ANN001 - shutil callback
        try:
            os.chmod(target, 0o700)
            func(target)
        except OSError:
            pass

    shutil.rmtree(path, onerror=onerror)


def _lay_down(case: Case, sandbox: str, tmpdir: str, target: Target) -> None:
    """Write a case's input files into the sandbox.

    Text files go through the target's path rewrite for the same reason stdin
    does: a ``.read`` script or an ``-init`` file can name ``/data/other.sql``,
    and under the native target ``/data`` does not exist.  Skipping the rewrite
    made the nested-read case fail on the reference and pass on the port, which
    is exactly backwards.  Binary files are left alone -- a fixture given as raw
    bytes means those bytes.
    """
    for name, text in case.files.items():
        path = os.path.join(sandbox, name.lstrip("/"))
        os.makedirs(os.path.dirname(path) or sandbox, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(target.rewrite(text, sandbox, tmpdir))
    for name, hexdata in case.binfiles.items():
        path = os.path.join(sandbox, name.lstrip("/"))
        os.makedirs(os.path.dirname(path) or sandbox, exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(bytes.fromhex(hexdata))


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _collect_files(case: Case, sandbox: str) -> dict[str, str]:
    """Digest and probe the files this case names.

    A named file that is missing records ``"<missing>"`` rather than raising: its
    absence is a legitimate observation, and the reference either agrees or it
    does not.
    """
    out: dict[str, str] = {}
    for name in case.digest_files:
        path = os.path.join(sandbox, name.lstrip("/"))
        out[name] = _sha256(path) if os.path.isfile(path) else "<missing>"
    for name, spec in case.probes.items():
        path = os.path.join(sandbox, name.lstrip("/"))
        kind, _, arg = spec.partition(":")
        if kind == "absent":
            out[name] = "absent" if not os.path.exists(path) else "PRESENT"
        elif not os.path.isfile(path):
            out[name] = "<missing>"
        elif kind == "exists":
            out[name] = "exists"
        elif kind == "size":
            out[name] = f"size={os.path.getsize(path)}"
        elif kind == "sha256":
            out[name] = _sha256(path)
        elif kind == "zero_prefix":
            n = int(arg)
            with open(path, "rb") as fh:
                head = fh.read(n)
            out[name] = (
                f"zero_prefix:{n}=ok"
                if len(head) == n and not any(head)
                else f"zero_prefix:{n}=violated"
            )
        else:  # pragma: no cover - guarded by case
            raise RunnerError(f"{case.key}: unhandled probe {spec!r}")
    return out


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------
#: Set once per worker process by ``configure``.  Passing targets through
#: ProcessPoolExecutor arguments would pickle them per case; this keeps the hot
#: path free of that.
_TARGET: Target | None = None
_NATIVE_ORACLE: str | None = None


def configure(target: Target, native_oracle: str | None) -> None:
    global _TARGET, _NATIVE_ORACLE
    _TARGET = target
    _NATIVE_ORACLE = native_oracle


def _native_read(case: Case, sandbox: str, tmpdir: str) -> str:
    """Hand a database the run produced to *native* sqlite3 and ask it a question.

    This is the check a stubbed VFS cannot survive.  Byte-identity says the port
    wrote the right bytes; this says the reference implementation, running on a
    different platform entirely, accepts those bytes as a database and gets the
    right answers out of them.
    """
    if not _NATIVE_ORACLE:
        raise RunnerError(f"{case.key}: native_read needs an oracle binary")
    db = os.path.join(sandbox, "db.sqlite")
    if not os.path.isfile(db):
        return "<no database>"
    proc = subprocess.run(
        [_NATIVE_ORACLE, db],
        input=case.crosscheck.encode("utf-8") + b"\n",
        capture_output=True,
        env=dict(BASE_ENV, TMPDIR=tmpdir, SQLITE_TMPDIR=tmpdir),
        timeout=case.timeout,
        cwd=sandbox,
    )
    out, err = decode(proc.stdout), decode(proc.stderr)
    text = out + ("\n[stderr] " + err if err.strip() else "")
    return _substitute(text, {sandbox: GUEST_DATA, tmpdir: GUEST_TMP})


def run_case(case: Case) -> Observation:
    """Execute one case in a fresh sandbox and return the normalised observation."""
    if _TARGET is None:  # pragma: no cover - configure() is always called first
        raise RunnerError("execute.configure() was not called")
    root = tempfile.mkdtemp(prefix=f"pf01-{case.case_id}-")
    sandbox = os.path.join(root, "data")
    tmpdir = os.path.join(root, "tmp")
    os.makedirs(sandbox)
    os.makedirs(tmpdir)
    try:
        _lay_down(case, sandbox, tmpdir, _TARGET)
        argv = _TARGET.argv(case, sandbox, tmpdir)
        stdin = _TARGET.rewrite(case.stdin, sandbox, tmpdir)
        env = dict(BASE_ENV, **dict(case.env))
        if _TARGET.name == "native":
            # Native reaches the real directories, so its temp search order has
            # to point at the sandbox's tmp rather than the host's.
            env["TMPDIR"] = tmpdir
            env["SQLITE_TMPDIR"] = tmpdir
        try:
            proc = subprocess.run(
                argv,
                # Bytes, not text: ``.binary on`` and blob literals put arbitrary
                # bytes on stdout, and a decode that replaces what it cannot read
                # would map two different streams onto the same text.
                input=stdin.encode("utf-8", errors="surrogateescape"),
                capture_output=True,
                env=env,
                timeout=case.timeout,
                cwd=sandbox,
            )
            stdout = decode(proc.stdout)
            stderr = decode(proc.stderr)
            code = proc.returncode
        except subprocess.TimeoutExpired:
            stdout, stderr, code = "", f"<timeout after {case.timeout}s>", -9
        except OSError as exc:
            stdout, stderr, code = "", f"<could not execute: {exc}>", -8

        def clean(text: str) -> str:
            # Absolute paths first, so a sandbox path inside an error message
            # becomes the guest path the expectation is written in terms of.
            # `root` is mapped too: it is a fresh mkdtemp name every run, so
            # letting it through would make any case that echoes it look
            # non-deterministic when only the directory name had changed.
            text = _substitute(text, {
                sandbox: GUEST_DATA,
                tmpdir: GUEST_TMP,
                root: "/sandbox",
            })
            # wasmtime warns about a missing ~/.sqliterc through the guest; the
            # reference has no such line, and it says nothing about the port.
            text = "\n".join(
                ln for ln in text.splitlines() if "sqliterc" not in ln
            ) + ("\n" if text.endswith("\n") else "")
            return normalise.apply(text, case.normalisers)

        files = _collect_files(case, sandbox) if "files" in case.checks else {}
        cross = (
            _native_read(case, sandbox, tmpdir)
            if "native_read" in case.checks
            else None
        )
        return Observation(
            stdout=clean(stdout),
            stderr=clean(stderr),
            exit_code=code,
            files=files,
            native_read=normalise.apply(cross, case.normalisers) if cross else cross,
        )
    finally:
        _force_rmtree(root)


def _worker_init(target: Target, oracle: str | None) -> None:
    configure(target, oracle)


def run_all(
    cases: list[Case],
    target: Target,
    native_oracle: str | None = None,
    workers: int | None = None,
) -> dict[str, Observation]:
    """Run every case, in parallel, and return observations keyed by case key."""
    workers = workers or min(16, (os.cpu_count() or 4))
    results: dict[str, Observation] = {}
    with ProcessPoolExecutor(
        max_workers=workers,
        initializer=_worker_init,
        initargs=(target, native_oracle),
    ) as pool:
        for case, obs in zip(cases, pool.map(run_case, cases, chunksize=4)):
            results[case.key] = obs
    return results
