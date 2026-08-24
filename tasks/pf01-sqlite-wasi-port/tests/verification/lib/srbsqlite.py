"""What a stage-3 candidate is given to attack a built SQLite.

A candidate is a pytest file.  It may import this module, `pytest` and the standard
library, and nothing else.  Everything here addresses the *built product* -- the CLI
the tree's own build produced -- so there is deliberately no way from here to reach a
source file, a build directory, an intermediate object, or the tree under test.

    import srbsqlite as sq

    def test_short_read_tail_is_zero_filled():
        db = sq.sql("create table t(a); insert into t values (x'ff');")
        assert db.ok, db

Why this module exists at all, rather than each candidate running the binary
itself: the two things being compared are not the same kind of artefact.  One side
is an x86-64 ELF executable invoked as `sqlite3 <args>`; the other is a wasm32
module invoked as `wasmtime run --dir ... sqlite3.wasm <args>`.  A candidate that
built its own command line would be writing that difference into every test, and
the first thing it would discover is the difference itself -- which is not a defect
and is explicitly out of scope.  So the invocation is written once, here, in the
shape stage 2 uses and instruction.md documents.

Three properties this file is responsible for, all three of them load-bearing:

**A fresh sandbox per call.**  Every `run()` gets its own directory, laid out with
whatever files the call asks for, and the guest sees it as `/data` and `/tmp`.  Two
calls in one test cannot see each other's databases unless the test asks them to via
`Session`.

**Symmetric path handling.**  The guest paths `/data` and `/tmp` are what a case is
written in terms of.  The wasm side reaches exactly those, because the preopens say
so; the native side is handed the sandbox's real paths on the way in and has them
rewritten back out of stdout, stderr and error messages on the way home.  Skip that
and every candidate that provokes an error message containing a filename "finds" a
divergence that is the harness's, not the port's.

**A default canonicalisation of the two known platform divergences.**  `argv[0]`
and long-double digits past the 17th are different between these two builds by
construction -- whoever launches a process chooses argv[0], and x86-64's long
double carries a 64-bit significand against wasm32's 113-bit -- and both are in
the probe's deny list.
They are normalised here by default so that an adversary does not spend a round
rediscovering them.  Pass `canon=False` if you want to see the raw bytes; asserting
on what it hides is still out of scope.

What is NOT hidden: which build you are talking to.  It cannot be.  One is native
3.31.1 and one is a wasm port, both this prompt and the task's own instructions say
so, and you can read both source trees.  The rule is not that you cannot tell -- it
is that a candidate whose assertion *depends* on telling establishes nothing about
the migration and is thrown out on review.  Ask the product a question about SQLite.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "GUEST_DATA", "GUEST_TMP", "SCRATCH", "RUN_LABEL", "NATIVE_3311",
    "Output", "Sandbox", "run", "sql", "cli", "Session", "sandbox",
    "canonicalise", "read_with_native_3311", "hexdump", "sha256",
]

# --------------------------------------------------------------------------- #
# The contract with run-candidate.sh
# --------------------------------------------------------------------------- #
#: Guest paths the scored invocation preopens.  Part of the task, not a detail of
#: this file: instruction.md states them and the port has to work with exactly this
#: much of a filesystem.
GUEST_DATA = "/data"
GUEST_TMP = "/tmp"


def _spec() -> dict:
    path = os.environ.get("SRB_SQLITE_SPEC")
    if not path:
        raise RuntimeError(
            "SRB_SQLITE_SPEC is not set.  This module only works inside a stage-3 "
            "candidate run; run-candidate.sh writes the spec and exports it."
        )
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"the runner spec at {path} is unreadable: {exc}") from None


_SPEC = _spec()

#: Scratch for this (candidate, tree) pair.  Emptied between them, so a file one
#: half of a comparison left behind cannot change the answer the other half gets.
SCRATCH = Path(os.environ.get("SRB_SCRATCH", tempfile.gettempdir()))

#: A random label for this invocation, so two failure messages from one comparison
#: stay distinguishable in a transcript.  Random rather than derived from the tree:
#: it says "a different run", not "the other side".
RUN_LABEL = os.environ.get("SRB_RUN_LABEL", "?")

#: A fixed native SQLite 3.31.1, built into the image from the published payload,
#: available in *both* halves of every comparison.  This is the cross-check that a
#: stubbed storage layer cannot survive: hand it a database the build under test
#: produced and ask it a question.  It is not the thing under test and it is not the
#: reference for stdout -- it is a second, independent reader of the file format.
NATIVE_3311 = _SPEC.get("native_3311") or ""

#: Seconds a single CLI invocation gets before it is declared hung.  A hang is a
#: legitimate finding, so a timeout returns rather than raising.
DEFAULT_TIMEOUT = float(_SPEC.get("default_timeout") or 60.0)

#: The environment every run gets, pinned exactly as stage 2 and `wasi-run` pin it.
#: LANG and LC_ALL decide collation, TZ decides how dates render; a run that
#: inherited them would agree with one container and not the next.
GUEST_ENV = {
    "LANG": "C",
    "LC_ALL": "C",
    "TZ": "UTC",
    "PATH": "/usr/bin:/bin",
    "HOME": GUEST_TMP,
    "TMPDIR": GUEST_TMP,
    "SQLITE_TMPDIR": GUEST_TMP,
}


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #
@dataclass
class Output:
    """One invocation's result.  Bytes, not str -- SQLite emits blobs.

    Compare `Output`s field by field rather than whole: `a.stdout == b.stdout` says
    what diverged, `a == b` does not.
    """

    argv: list = field(default_factory=list)
    status: int = -1
    stdout: bytes = b""
    stderr: bytes = b""
    duration: float = 0.0
    timed_out: bool = False
    sandbox: Path | None = None

    @property
    def ok(self) -> bool:
        """Exit status 0 and it finished."""
        return self.status == 0 and not self.timed_out

    @property
    def out(self) -> str:
        """stdout as text.  Undecodable bytes survive as surrogates rather than
        raising, so a case that produces a stray byte is still comparable."""
        return decode(self.stdout)

    @property
    def err(self) -> str:
        return decode(self.stderr)

    def __str__(self) -> str:  # what pytest shows when an assert fails
        head = (
            f"[run {RUN_LABEL}] status={self.status}"
            f"{' TIMED-OUT' if self.timed_out else ''} in {self.duration:.2f}s"
        )
        body = [head]
        if self.stdout:
            body.append(f"  stdout: {_clip(self.out)}")
        if self.stderr:
            body.append(f"  stderr: {_clip(self.err)}")
        return "\n".join(body)


def decode(raw: bytes) -> str:
    return raw.decode("utf-8", "surrogateescape") if isinstance(raw, bytes) else raw


def _clip(text: str, limit: int = 2000) -> str:
    text = text.replace("\n", "\n          ")
    return text if len(text) <= limit else text[:limit] + f"... (+{len(text) - limit}B)"


# --------------------------------------------------------------------------- #
# Canonicalisation of the two known platform divergences
# --------------------------------------------------------------------------- #
# Both of these are denied by probe.toml's scope, and stage 2 normalises the same two
# in normalise.py.  They are folded away here so a round does not spend itself
# rediscovering a divergence the target forces.

#: `Usage: <argv[0]> [OPTIONS] ...` and `<argv[0]>: Error: ...`.  The CLI prints the
#: name it was invoked with, which on one side is a path to an ELF file and on the
#: other a path to a .wasm module.  The *text after* the program name is the port's
#: business and is left alone.
_USAGE = re.compile(r"(?m)^Usage:\s+\S+(?=\s+\[OPTIONS\])")
_ERRPFX = re.compile(r"(?m)^\S+(?=: Error: )")

#: A decimal real with a long digit string.  x86-64 renders SQLite's `%!.*f` through
#: an 80-bit long double (64-bit significand, ~19 decimal digits); wasm32 has no
#: 80-bit type, so long double is IEEE binary128 there (113-bit significand).  Both
#: hold the identical binary64 value; they disagree only in the digits past the point
#: where a double stops carrying information.
_REAL = re.compile(rb"-?\d+\.\d+(?:[eE][-+]?\d+)?")


def _significant(token: bytes) -> int:
    digits = token.split(b"e")[0].split(b"E")[0].lstrip(b"-+").replace(b".", b"")
    return len(digits.lstrip(b"0")) or 1


def _shorten(match) -> bytes:
    token = match.group(0)
    if _significant(token) <= 17:
        return token
    try:
        value = float(token)
    except ValueError:
        return token
    if not math.isfinite(value):
        return token
    # repr() of a float is the shortest string that round-trips to the same double,
    # computed here on the host -- so both sides collapse to one spelling of the one
    # value they both actually hold.
    return repr(value).encode()


def canonicalise(raw: bytes) -> bytes:
    """Fold out argv[0] and long-double digits past the 17th."""
    if not raw:
        return raw
    text = decode(raw)
    text = _USAGE.sub("Usage: sqlite3", text)
    text = _ERRPFX.sub("sqlite3", text)
    return _REAL.sub(_shorten, text.encode("utf-8", "surrogateescape"))


#: The CLI reads ~/.sqliterc at startup and says so on stderr when it cannot.  HOME
#: is a real directory on one side and a preopen on the other, so the line differs
#: for a reason that has nothing to do with SQLite.
_SQLITERC = re.compile(r"(?m)^.*\.sqliterc.*\n?")


def _strip_sqliterc(text: str) -> str:
    return _SQLITERC.sub("", text)


# --------------------------------------------------------------------------- #
# Sandboxes
# --------------------------------------------------------------------------- #
_counter = 0


class Sandbox:
    """A directory pair the guest sees as `/data` and `/tmp`.

    `sb.data` and `sb.tmp` are real host paths; use them to plant a file before a run
    or read one after it.  Inside a CLI argument, and inside SQL, write the *guest*
    path -- `/data/x.db` -- and it will mean the same file on both sides.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.data = root / "sandbox"
        self.tmp = root / "tmp"
        self.data.mkdir(parents=True, exist_ok=True)
        self.tmp.mkdir(parents=True, exist_ok=True)

    # -- guest <-> host ---------------------------------------------------- #
    def host(self, guest: str) -> Path:
        """The host path behind a guest path.  `x.db` with no leading slash is
        taken as `/data/x.db`, because that is where a bare filename lands."""
        if not guest.startswith("/"):
            return self.data / guest
        if guest == GUEST_DATA or guest.startswith(GUEST_DATA + "/"):
            return self.data / guest[len(GUEST_DATA) :].lstrip("/")
        if guest == GUEST_TMP or guest.startswith(GUEST_TMP + "/"):
            return self.tmp / guest[len(GUEST_TMP) :].lstrip("/")
        raise ValueError(
            f"{guest!r} is outside the sandbox.  A run can only reach {GUEST_DATA} "
            f"and {GUEST_TMP}; that is the whole filesystem the port is given."
        )

    def write(self, guest: str, content) -> Path:
        """Plant a file.  `content` may be bytes or str."""
        path = self.host(guest)
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            content = content.encode()
        path.write_bytes(content)
        return path

    def read(self, guest: str) -> bytes:
        return self.host(guest).read_bytes()

    def exists(self, guest: str) -> bool:
        return self.host(guest).exists()

    def listing(self) -> list:
        """Every guest path in the sandbox, sorted.  Useful for "did it clean up its
        temp files", which is in scope; the *names* of those files are not."""
        found = []
        for base, guest in ((self.data, GUEST_DATA), (self.tmp, GUEST_TMP)):
            for path in sorted(base.rglob("*")):
                rel = path.relative_to(base).as_posix()
                found.append(f"{guest}/{rel}" + ("/" if path.is_dir() else ""))
        return sorted(found)

    # -- invocation --------------------------------------------------------- #
    def run(
        self,
        args=(),
        *,
        stdin: str | bytes = "",
        timeout: float | None = None,
        env: dict | None = None,
        canon: bool = True,
    ) -> Output:
        """Run the CLI under test in this sandbox and wait for it.

        `args` are the CLI's arguments; write guest paths in them.  Repeated calls
        on one `Sandbox` see one directory, which is how a journal, a WAL or a
        recovery case gets written -- one process leaves a file, the next opens it.
        """
        return self.spawn(args, stdin=stdin, env=env).wait(timeout, canon=canon)

    def sql(self, script: str, *, db: str = "/data/db.sqlite", args=(), **kw) -> Output:
        """`sqlite3 <db> ` with `script` on stdin.  The common case."""
        if not script.endswith("\n"):
            script += "\n"
        return self.run([db, *args], stdin=script, **kw)

    def spawn(self, args=(), *, stdin: str | bytes = "", env: dict | None = None):
        """Start the CLI without waiting.

        For the cases that need two processes on one database at once -- a second
        writer arriving mid-transaction, a reader during a checkpoint.  Two guests
        can hold the same file here because both preopen the same host directory.

            a = box.spawn(["/data/db.sqlite"], stdin="begin exclusive; select 1;")
            b = box.run(["/data/db.sqlite"], stdin="insert into t values(9);")
            a.wait()
        """
        return _Running(self, list(args), stdin, env)

    def native(self, guest_db: str = "/data/db.sqlite",
               script: str = "pragma audit_check;\n", *,
               timeout: float | None = None) -> Output:
        """Ask the fixed native 3.31.1 a question about a file in this sandbox.

        Available on both sides of the comparison, and the same binary on both, so a
        divergence here is a divergence in the *file* the build under test wrote --
        not in how it renders output.  This is the check a stubbed storage layer
        cannot survive: byte-identity says the port wrote the right bytes, this says
        an independent implementation accepts them as a database.
        """
        return read_with_native_3311(self, guest_db, script, timeout=timeout)

    def __repr__(self) -> str:
        return f"<Sandbox {self.root.name}>"


def sandbox(files: dict | None = None) -> Sandbox:
    """A fresh sandbox, optionally pre-populated: `sandbox({"/data/x.db": blob})`."""
    global _counter
    _counter += 1
    root = SCRATCH / f"run-{_counter:04d}"
    if root.exists():  # a candidate that re-ran its own helper
        shutil.rmtree(root, ignore_errors=True)
    box = Sandbox(root)
    for guest, content in (files or {}).items():
        box.write(guest, content)
    return box


#: `Session` reads better than `sandbox` when the point is a sequence of commands
#: against one database rather than the directory they share.
Session = sandbox


# --------------------------------------------------------------------------- #
# Invocation
# --------------------------------------------------------------------------- #
_KIND = _SPEC.get("kind") or ""
_PRODUCT = _SPEC.get("product") or ""
_WASMTIME = _SPEC.get("wasmtime") or ""

if _KIND not in ("native", "wasm"):
    raise RuntimeError(f"the runner spec names an unknown kind {_KIND!r}")


def _substitute(text: str, mapping: dict) -> str:
    """Replace every key with its value in a single pass.

    Single-pass rather than a chain of `str.replace`, and the reason is specific:
    the sandbox lives under a directory whose own path contains the literal `/tmp`,
    so a second pass would rewrite text the first pass had just inserted and splice
    the scratch root through the middle of `/data/db.sqlite`.  Longest key first, so
    a key that is a prefix of another cannot win.
    """
    if not mapping:
        return text
    pattern = re.compile("|".join(re.escape(k) for k in sorted(mapping, key=len, reverse=True)))
    return pattern.sub(lambda m: mapping[m.group(0)], text)


def _into(box: Sandbox, text: str) -> str:
    """Guest paths -> what this build can actually reach.

    On wasm this is the identity: the preopens make `/data` and `/tmp` real inside
    the guest.  On native there is no preopen indirection, so the same string has to
    become the sandbox's own directories -- otherwise every case that names a file
    would fail on one side for a reason that is the harness's, not the port's.
    """
    if _KIND == "wasm":
        return text
    return _substitute(text, {
        GUEST_DATA: str(box.data),
        GUEST_TMP: str(box.tmp),
    })


def _outof(box: Sandbox, text: str) -> str:
    """...and back, so an error message reads the same from both builds."""
    return _substitute(text, {
        str(box.data): GUEST_DATA,
        str(box.tmp): GUEST_TMP,
        # A fresh directory name per call, so letting it through would make any
        # case that echoes it look non-deterministic when only the name changed.
        str(box.root): "/sandbox",
        str(SCRATCH): "/scratch",
        # The product's own path holds the per-run token.  Both builds are invoked
        # through the same filename, so this collapses to one spelling.
        _PRODUCT: "sqlite3",
    })


def _argv(box: Sandbox, args: list, env: dict) -> list:
    """The command line, in the shape the task's own contract fixes.

    The wasm half is `scripts/wasi-run` written out in Python: the same three
    preopens, the same six pinned environment values, in the same order.  It is not
    a convenience -- a WASI guest reaches exactly what the host preopens for it, so
    the launch *is* part of what is being tested, and a port that works under a
    fourth preopen has not been shown to work.
    """
    argv = [_PRODUCT if _KIND == "native" else _WASMTIME]
    if _KIND == "wasm":
        argv += [
            "run",
            "--dir", f"{box.data}::{GUEST_DATA}",
            "--dir", f"{box.tmp}::{GUEST_TMP}",
            # The sandbox again as the guest cwd, so a bare relative name has
            # something to resolve against -- `.archive`, `.import` and `fsdir` all
            # produce them.  It mirrors the native side running with its cwd set
            # rather than granting extra reach: it is the same directory twice.
            "--dir", f"{box.data}::.",
        ]
        for key, value in sorted(env.items()):
            argv += ["--env", f"{key}={value}"]
        argv.append(_PRODUCT)
    return argv + [_into(box, str(a)) for a in args]


class _Running:
    """A started CLI process.  `wait()` collects it into an `Output`."""

    def __init__(self, box: Sandbox, args: list, stdin, env: dict | None) -> None:
        self.box = box
        wanted = dict(GUEST_ENV, **(env or {}))
        self.argv = _argv(box, args, wanted)
        if _KIND == "native":
            # Native reaches real directories, so its temp search order has to point
            # at this sandbox's tmp rather than the container's.
            wanted = dict(wanted, TMPDIR=str(box.tmp), SQLITE_TMPDIR=str(box.tmp),
                          HOME=str(box.tmp))
        if isinstance(stdin, str):
            stdin = _into(box, stdin).encode("utf-8", "surrogateescape")
        self.stdin = stdin
        self.started = _now()
        try:
            self.proc = subprocess.Popen(
                self.argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(box.data),
                # Nothing is inherited: the container's environment holds paths into
                # the tree under test, and a run that picked up LANG or TZ from it
                # would agree with one container and not the next.
                env=wanted,
                # Its own process group, so a hang can be ended.  Killing the child
                # alone is not enough: wasmtime is the child and the guest's work
                # happens under it, and a surviving grandchild keeps the stdout pipe
                # open -- which turns "the port hangs" into "the round hangs".
                start_new_session=True,
            )
        except OSError as exc:
            raise RuntimeError(f"could not start the build under test: {exc}") from None

    def wait(self, timeout: float | None = None, *, canon: bool = True) -> Output:
        limit = DEFAULT_TIMEOUT if timeout is None else float(timeout)
        timed_out = False
        try:
            out, err = self.proc.communicate(self.stdin, timeout=limit)
        except subprocess.TimeoutExpired:
            # A hang is a legitimate finding, so this returns rather than raising:
            # `assert not r.timed_out` is a thing a candidate is allowed to write.
            out, err = _end(self.proc)
            timed_out = True
        result = Output(
            argv=list(self.argv),
            status=-9 if timed_out else self.proc.returncode,
            stdout=_clean(self.box, out or b"", canon),
            stderr=_clean(self.box, err or b"", canon),
            duration=_now() - self.started,
            timed_out=timed_out,
            sandbox=self.box.root,
        )
        return result

    def poll(self):
        return self.proc.poll()

    def kill(self) -> None:
        _end(self.proc)


def _end(proc) -> tuple:
    """Stop a run that will not stop, and collect whatever it had said.

    SIGKILL to the whole group rather than to the one process, then a bounded
    collect.  If even that leaves the pipes held -- a process in uninterruptible
    sleep -- the pipes are closed and the output taken as truncated, because a
    candidate waiting forever for a hung read reports nothing at all.
    """
    for sig in (signal.SIGKILL,):
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except OSError:
                pass
    for attempt in (2.0, 2.0):
        try:
            return proc.communicate(timeout=attempt)
        except subprocess.TimeoutExpired:
            continue
        except (ValueError, OSError):
            break
    out = err = b""
    for stream, into in ((proc.stdout, "out"), (proc.stderr, "err")):
        if stream is None:
            continue
        try:
            os.set_blocking(stream.fileno(), False)
            data = stream.read() or b""
        except (OSError, ValueError):
            data = b""
        if into == "out":
            out = data
        else:
            err = data
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        try:
            if stream is not None:
                stream.close()
        except OSError:
            pass
    return out, err


def _now() -> float:
    return time.monotonic()


def _clean(box: Sandbox, raw: bytes, canon: bool) -> bytes:
    if not raw:
        return raw
    text = _outof(box, decode(raw))
    # Unconditional, both of them: these are the harness's own noise rather than
    # anything the port decided, so `canon=False` should not resurrect them.
    text = _strip_sqliterc(text)
    out = text.encode("utf-8", "surrogateescape")
    return canonicalise(out) if canon else out


# --------------------------------------------------------------------------- #
# The short forms
# --------------------------------------------------------------------------- #
def run(args=(), *, files: dict | None = None, **kw) -> Output:
    """One invocation in a fresh sandbox.  `run(["/data/db.sqlite", "select 1;"])`."""
    return sandbox(files).run(args, **kw)


def sql(script: str, *, files: dict | None = None, db: str = "/data/db.sqlite",
        args=(), **kw) -> Output:
    """One SQL script against a fresh database.  The form most cases want."""
    return sandbox(files).sql(script, db=db, args=args, **kw)


def cli(*args, **kw) -> Output:
    """`cli("-help")`, `cli("-csv", "/data/db.sqlite", "select 1;")`."""
    return run(list(args), **kw)


def read_with_native_3311(box: Sandbox, guest_db: str = "/data/db.sqlite",
                          script: str = "pragma audit_check;\n", *,
                          timeout: float | None = None) -> Output:
    """Run the fixed native 3.31.1 against a file in `box`.

    Not the thing under test and not the reference for stdout -- a second reader.
    Identical binary on both sides of the comparison, so the only thing that can
    make its answers differ is the file it was handed.
    """
    if not NATIVE_3311 or not os.path.isfile(NATIVE_3311):
        raise RuntimeError(
            "the fixed native 3.31.1 is missing from this image; "
            "report this rather than working around it"
        )
    if not script.endswith("\n"):
        script += "\n"
    host = box.host(guest_db)
    started = _now()
    argv = [NATIVE_3311, str(host)]
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=str(box.data),
        env=dict(GUEST_ENV, TMPDIR=str(box.tmp), SQLITE_TMPDIR=str(box.tmp),
                 HOME=str(box.tmp)),
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(script.encode("utf-8", "surrogateescape"),
                                    timeout=DEFAULT_TIMEOUT if timeout is None
                                    else float(timeout))
        status, hung = proc.returncode, False
    except subprocess.TimeoutExpired:
        # 3.31.1 does not hang by itself.  Handed a file the build under test wrote,
        # it can: a corrupt page map is a legitimate way to loop.  So this returns
        # the same shape as any other run rather than raising.
        out, err = _end(proc)
        status, hung = -9, True
    return Output(
        argv=argv, status=status,
        stdout=_clean(box, out, True), stderr=_clean(box, err, True),
        duration=_now() - started, timed_out=hung, sandbox=box.root,
    )


# --------------------------------------------------------------------------- #
# Reading bytes
# --------------------------------------------------------------------------- #
def sha256(data) -> str:
    if isinstance(data, (str, Path)):
        data = Path(data).read_bytes()
    return hashlib.sha256(data).hexdigest()


def hexdump(data: bytes, start: int = 0, length: int = 256) -> str:
    """A readable window on a database file, for a failure message that has to show
    what actually differed rather than assert that something did."""
    chunk = data[start:start + length]
    lines = []
    for off in range(0, len(chunk), 16):
        row = chunk[off:off + 16]
        hexed = " ".join(f"{b:02x}" for b in row)
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
        lines.append(f"{start + off:08x}  {hexed:<47}  {text}")
    return "\n".join(lines)
