"""What a stage-3 candidate is given to attack a built jsonnet.

A candidate is a pytest file.  It may import this module and the standard library,
and nothing else.  Everything here addresses the two *built programs* -- the tree
under test has already been built and its `jsonnet` and `jsonnetfmt` installed into
a prefix -- so there is deliberately no way from here to reach a source file, a
project file, an intermediate artifact or a build log.  A migration is graded on
what it ships.

The shape of a candidate is the shape of a stage-2 case, on purpose: an input tree,
an argv, and a comparison of exit status, stdout, stderr and the files the run
created, changed or removed.  Nothing else is observable, and nothing else is
compared.  That is what makes a found candidate useful after the round -- it can be
read into the behavioural suite as a case, because it is already one.

    import srbjsonnet as rj

    def test_object_comprehension_over_empty():
        out = rj.jsonnet("-e", "{[k]: 1 for k in []}")
        assert out.ok, out
        assert out.stdout == b"{ }\\n"

Two levels of interface:

    jsonnet(...) / jsonnetfmt(...)   run one program, get an Output
    Case(...).run()                  a tree of input files, a run, and what
                                     changed on disk afterwards

Use the first for anything expressible as stdin and argv.  Use the second when the
interesting part is on disk -- `--multi`, `--output-file`, `--string`, jsonnetfmt's
`--in-place` and `--test`, or an import that has to resolve against a directory
laid out a particular way.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "PREFIX", "JSONNET", "JSONNETFMT", "TARGET_NAME", "SCRATCH",
    "Output", "Case", "Result",
    "jsonnet", "jsonnetfmt", "run", "evaluate", "reformat", "version",
]


def _env_path(name: str) -> Path:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"{name} is not set.  This module only works inside a stage-3 "
            f"candidate run; run-candidate.sh sets it.")
    return Path(value)


#: The install prefix of the tree under test.
PREFIX = _env_path("SRB_PREFIX")
#: The two programs.  Same names on both trees, at the same relative paths, so a
#: candidate cannot tell them apart by where they are.
JSONNET = _env_path("SRB_JSONNET")
JSONNETFMT = _env_path("SRB_JSONNETFMT")
#: An opaque per-run label for the tree under test, for failure messages.  It is a
#: hash, not "original" or "submission": a candidate that reads the role would meet
#: every mechanical condition for a break -- passes on one tree, fails on the other,
#: reproduces exactly -- while establishing nothing about the migration.  A token
#: keeps two messages from one comparison distinguishable without answering "which
#: side am I on".
TARGET_NAME = os.environ.get("SRB_TARGET_TOKEN", "?")
#: Scratch for input trees.  Emptied between (candidate, tree) pairs.
SCRATCH = Path(os.environ.get("SRB_SCRATCH", "/tmp/srb-candidate"))

#: How long a single run may take before it is reported as a hang.  A hang IS a
#: finding -- the reference terminates on everything in its own suite -- but the
#: wall-clock number is not, and a candidate asserting one is out of scope.
DEFAULT_TIMEOUT = 30.0

#: Every run gets exactly this environment, plus whatever the case adds.  Fixed
#: rather than inherited: the locale, the timezone and HOME all reach a program that
#: formats numbers or resolves a path, and a difference in any of them between the
#: two halves of a comparison would be a difference this stage is not measuring.
#: JSONNET_PATH is deliberately absent -- set it per case if that is the point.
BASE_ENV = {
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "LC_ALL": "C",
    "LANG": "C",
    "TZ": "UTC",
    "TERM": "dumb",
}


@dataclass
class Output:
    """What one run produced.  Streams are bytes, deliberately.

    jsonnet's output is not always valid UTF-8 -- it is whatever the input made it,
    and the interesting cases include exactly that.  Decoding here would hide the
    difference a candidate is looking for, and `--string` output of a manifested
    string can be arbitrary bytes.
    """

    argv: list[str]
    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def text(self, errors: str = "replace") -> str:
        return self.stdout.decode("utf-8", errors)

    def err(self, errors: str = "replace") -> str:
        return self.stderr.decode("utf-8", errors)

    def __str__(self) -> str:
        head = f"{' '.join(self.argv)} -> exit {self.returncode}"
        if self.timed_out:
            head += f" (no exit within the timeout)"
        parts = [head]
        if self.stdout:
            parts.append(f"stdout: {self.stdout[:1500]!r}")
        if self.stderr:
            parts.append(f"stderr: {self.stderr[:1500]!r}")
        return "\n".join(parts)


def _run(argv: list[str], *, stdin: bytes = b"", timeout: float,
         cwd: Path, env: dict[str, str]) -> Output:
    try:
        proc = subprocess.run(
            argv, input=stdin, capture_output=True, timeout=timeout,
            cwd=str(cwd), env=env,
        )
    except subprocess.TimeoutExpired as exc:
        return Output(argv, 124, exc.stdout or b"", exc.stderr or b"",
                      timed_out=True)
    return Output(argv, proc.returncode, proc.stdout, proc.stderr)


def _fresh(prefix: str) -> Path:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f"{prefix}-", dir=str(SCRATCH)))


# --------------------------------------------------------------------------- #
# One run, no input tree
# --------------------------------------------------------------------------- #

def run(program: str, *args: str, stdin: bytes | str = b"",
        timeout: float = DEFAULT_TIMEOUT,
        env: dict[str, str] | None = None) -> Output:
    """Run `jsonnet` or `jsonnetfmt` with these arguments in an empty directory.

    The working directory is fresh and empty, which matters more than it looks: a
    relative import, a `--multi` target and jsonnetfmt's `--in-place` are all
    resolved against cwd, and a candidate that ran in a directory holding another
    candidate's leftovers would not reproduce.

    A timeout returns an Output with `timed_out` set and returncode 124 rather than
    raising, so "it never came back" is something a candidate can assert.
    """
    binary = {"jsonnet": JSONNET, "jsonnetfmt": JSONNETFMT}.get(program)
    if binary is None:
        raise ValueError(f"unknown program {program!r}; "
                         f"expected 'jsonnet' or 'jsonnetfmt'")
    if isinstance(stdin, str):
        stdin = stdin.encode("utf-8")
    full = dict(BASE_ENV)
    full["HOME"] = str(SCRATCH)
    full.update(env or {})
    work = _fresh(program)
    try:
        return _run([str(binary), *args], stdin=stdin, timeout=timeout,
                    cwd=work, env=full)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def jsonnet(*args: str, stdin: bytes | str = b"",
            timeout: float = DEFAULT_TIMEOUT,
            env: dict[str, str] | None = None) -> Output:
    """The evaluator.  `jsonnet("-e", "1+1")` -> Output."""
    return run("jsonnet", *args, stdin=stdin, timeout=timeout, env=env)


def jsonnetfmt(*args: str, stdin: bytes | str = b"",
               timeout: float = DEFAULT_TIMEOUT,
               env: dict[str, str] | None = None) -> Output:
    """The formatter.  `jsonnetfmt("-", stdin="{a:1}")` -> Output."""
    return run("jsonnetfmt", *args, stdin=stdin, timeout=timeout, env=env)


def evaluate(snippet: str, *extra: str, timeout: float = DEFAULT_TIMEOUT) -> bytes:
    """`jsonnet -e <snippet>`, raising unless it exited 0.  The shorthand.

    Use `jsonnet(...)` directly whenever the exit status, the stderr or a non-zero
    return is the thing being asserted -- which for this task is most of the time,
    because the three error channels and their exact wording are in scope.
    """
    out = jsonnet(*extra, "-e", snippet, timeout=timeout)
    if not out.ok:
        raise AssertionError(f"jsonnet -e failed on {TARGET_NAME}: {out}")
    return out.stdout


def reformat(source: str, *extra: str, timeout: float = DEFAULT_TIMEOUT) -> bytes:
    """`jsonnetfmt -` over this source, raising unless it exited 0."""
    out = jsonnetfmt(*extra, "-", stdin=source, timeout=timeout)
    if not out.ok:
        raise AssertionError(f"jsonnetfmt failed on {TARGET_NAME}: {out}")
    return out.stdout


def version(program: str = "jsonnet") -> bytes:
    """`--version` for one of the two programs."""
    return run(program, "--version").stdout


# --------------------------------------------------------------------------- #
# A case: an input tree, a run, and what happened on disk
# --------------------------------------------------------------------------- #

@dataclass
class Result:
    """An Output plus the effect the run had on the input tree.

    `created`, `changed` and `removed` are relative paths.  All three are compared
    because all three are observable and all three have been wrong in ports of this
    program: `--multi` writes a file per top-level field and skips the write when
    the content is unchanged, `--output-file` writes one, `jsonnetfmt --in-place`
    rewrites its inputs, and none of them is supposed to touch anything else.
    """

    output: Output
    created: dict[str, bytes] = field(default_factory=dict)
    changed: dict[str, bytes] = field(default_factory=dict)
    removed: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.output.ok

    @property
    def stdout(self) -> bytes:
        return self.output.stdout

    @property
    def stderr(self) -> bytes:
        return self.output.stderr

    @property
    def returncode(self) -> int:
        return self.output.returncode

    def read(self, rel: str) -> bytes:
        """The contents of a file the run created or changed.

        Raises with the full list rather than KeyError, because "it wrote nothing"
        and "it wrote somewhere else" are different findings and the message should
        say which.
        """
        for where in (self.created, self.changed):
            if rel in where:
                return where[rel]
        raise AssertionError(
            f"{rel!r} was neither created nor changed on {TARGET_NAME}.  "
            f"created={sorted(self.created)} changed={sorted(self.changed)} "
            f"removed={sorted(self.removed)}\n{self.output}")

    def __str__(self) -> str:
        disk = (f"created={sorted(self.created)} changed={sorted(self.changed)} "
                f"removed={sorted(self.removed)}")
        return f"{self.output}\n{disk}"


class Case:
    """A directory of input files, one run, and the diff of the directory after.

        c = Case({"a.jsonnet": "{x: import 'b.libsonnet'}",
                  "b.libsonnet": "1 + 1"})
        r = c.run("jsonnet", "-m", "out", "a.jsonnet")
        assert r.ok, r
        assert r.read("out/x") == b"2\\n"

    Files are written relative to a fresh directory that is the run's cwd.  Parent
    directories are created.  A value may be str (encoded UTF-8) or bytes -- bytes
    when the input is deliberately not valid UTF-8, which several of the
    interesting cases are.

    The directory is snapshotted by content hash before the run and compared after,
    which is how `created`, `changed` and `removed` are computed.  Contents, not
    mtimes: a program that rewrites a file with identical bytes has not changed it,
    and the reference's `--multi` explicitly does not rewrite an unchanged output.
    """

    def __init__(self, files: dict[str, str | bytes] | None = None, *,
                 env: dict[str, str] | None = None) -> None:
        self.files = dict(files or {})
        self.env = dict(env or {})
        self.dir: Path | None = None

    # -- laying the tree out ------------------------------------------------

    def materialize(self) -> Path:
        """Write the input files into a fresh directory and return it."""
        work = _fresh("case")
        for rel, content in self.files.items():
            path = work / rel
            if not path.resolve().is_relative_to(work.resolve()):
                raise ValueError(f"{rel!r} escapes the case directory")
            path.parent.mkdir(parents=True, exist_ok=True)
            data = content.encode("utf-8") if isinstance(content, str) else content
            path.write_bytes(data)
        self.dir = work
        return work

    @staticmethod
    def _snapshot(root: Path) -> dict[str, str]:
        out: dict[str, str] = {}
        for path in root.rglob("*"):
            if path.is_file() and not path.is_symlink():
                rel = str(path.relative_to(root))
                out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
        return out

    # -- running ------------------------------------------------------------

    def run(self, program: str, *args: str, stdin: bytes | str = b"",
            timeout: float = DEFAULT_TIMEOUT) -> Result:
        """Run one program in the case directory and diff the directory after."""
        binary = {"jsonnet": JSONNET, "jsonnetfmt": JSONNETFMT}.get(program)
        if binary is None:
            raise ValueError(f"unknown program {program!r}; "
                             f"expected 'jsonnet' or 'jsonnetfmt'")
        work = self.materialize()
        before = self._snapshot(work)
        if isinstance(stdin, str):
            stdin = stdin.encode("utf-8")
        full = dict(BASE_ENV)
        full["HOME"] = str(work)
        full.update(self.env)
        try:
            output = _run([str(binary), *args], stdin=stdin, timeout=timeout,
                          cwd=work, env=full)
            after = self._snapshot(work)
            created = {rel: (work / rel).read_bytes()
                       for rel in sorted(set(after) - set(before))}
            changed = {rel: (work / rel).read_bytes()
                       for rel in sorted(set(after) & set(before))
                       if after[rel] != before[rel]}
            removed = sorted(set(before) - set(after))
            return Result(output, created=created, changed=changed,
                          removed=removed)
        finally:
            shutil.rmtree(work, ignore_errors=True)
            self.dir = None
