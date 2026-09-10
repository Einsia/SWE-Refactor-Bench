"""What a stage-3 candidate is given to attack an installed cmark.

A candidate is a pytest file. It may import this module and the standard library,
and nothing else. Everything here addresses the *installed* library and the
*installed* executable: a migration is graded on what it ships, so there is
deliberately no way from here to reach a build directory, an intermediate
artifact, or a source file.

Two ways in:

    run_cmark(...)   the command line, for anything cmark(1) can express
    CProgram(...)    a C program compiled and linked against libcmark

The second exists because the interesting half of this API is not reachable from
a command line. The custom allocator, the streaming parser, the node tree and the
iterator are C entry points, and a candidate that wants to know whether
cmark_mem is honoured has to write C. Compiling it by hand would mean every
adversary writing the same forty lines of cc invocation, badly, and a candidate
that fails because its own cc line was wrong is a wasted round.

The compiler here is for the CANDIDATE's C, never for the tree under test. Both
trees arrive already built and installed; this module cannot rebuild either, and
a candidate cannot use it to supply C to a submission that is missing some.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "PREFIX", "CMARK", "LIBDIR", "INCLUDEDIR", "TARGET_NAME", "SCRATCH",
    "Output", "run_cmark", "render", "CProgram", "pkg_config", "so_files",
    "library_path", "nm_defined",
]


def _env_path(name: str) -> Path:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"{name} is not set. This module only works inside a stage-3 "
            f"candidate run; run-candidate.sh sets it."
        )
    return Path(value)


#: The install prefix of the tree under test.
PREFIX = _env_path("SRB_PREFIX")
#: The installed cmark(1).
CMARK = _env_path("SRB_CMARK")
LIBDIR = _env_path("SRB_LIBDIR")
INCLUDEDIR = _env_path("SRB_INCLUDEDIR")
#: An opaque per-run label for the tree under test, for diagnostics.  A hash
#: keeps two failure messages from one comparison distinguishable without saying
#: which side produced which.
TARGET_NAME = os.environ.get("SRB_TARGET_TOKEN", "?")
#: Scratch for compiled helpers. Emptied between (candidate, tree) pairs.
SCRATCH = Path(os.environ.get("SRB_SCRATCH", "/tmp/srb-candidate"))

#: Every subprocess gets this. LD_LIBRARY_PATH points at the install so a helper
#: links against the tree under test; the locale is fixed because a renderer that
#: consults it would otherwise answer differently on the two trees for a reason
#: that is not the migration.
def _base_env(**extra: str) -> dict[str, str]:
    env = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "LD_LIBRARY_PATH": str(LIBDIR),
        "PKG_CONFIG_PATH": str(LIBDIR / "pkgconfig"),
        "LC_ALL": "C.UTF-8",
        "LANG": "C.UTF-8",
        "TZ": "UTC",
        "HOME": str(SCRATCH),
    }
    env.update(extra)
    return env


@dataclass
class Output:
    """The result of running something. Streams are bytes, deliberately.

    cmark's output is not always valid UTF-8 -- it is whatever the input made it,
    and several of the interesting cases are about exactly that. Decoding here
    would hide the difference a candidate is looking for.
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

    def __str__(self) -> str:
        head = f"{' '.join(self.argv)} -> {self.returncode}"
        if self.timed_out:
            head += " (timed out)"
        tail = self.stderr.decode("utf-8", "replace").strip()
        return f"{head}\n{tail[:2000]}" if tail else head


def _run(argv: list[str], *, stdin: bytes = b"", timeout: float = 30.0,
         cwd: Path | None = None, env: dict[str, str] | None = None) -> Output:
    try:
        proc = subprocess.run(
            argv, input=stdin, capture_output=True, timeout=timeout,
            cwd=str(cwd) if cwd else None, env=env or _base_env(),
        )
    except subprocess.TimeoutExpired as exc:
        return Output(argv, 124, exc.stdout or b"", exc.stderr or b"",
                      timed_out=True)
    return Output(argv, proc.returncode, proc.stdout, proc.stderr)


# --------------------------------------------------------------------------- #
# The command line
# --------------------------------------------------------------------------- #

def run_cmark(*args: str, stdin: bytes | str = b"",
              timeout: float = 30.0) -> Output:
    """Run the installed cmark(1) with these arguments.

        out = run_cmark("--to", "xml", stdin="*hi*\\n")
        assert out.stdout == b"..."

    A timeout returns an Output with timed_out set and returncode 124 rather than
    raising: "it hung" is a finding, and a candidate should be able to assert it.
    """
    if isinstance(stdin, str):
        stdin = stdin.encode("utf-8")
    return _run([str(CMARK), *args], stdin=stdin, timeout=timeout)


def render(document: bytes | str, fmt: str = "html", *extra: str,
           timeout: float = 30.0) -> bytes:
    """Render a document and return the bytes, raising if cmark failed.

    The shorthand for the common case. Use run_cmark when the exit status, the
    stderr or a non-zero return is the thing being asserted.
    """
    out = run_cmark("--to", fmt, *extra, stdin=document, timeout=timeout)
    if not out.ok:
        raise AssertionError(f"cmark --to {fmt} failed on {TARGET_NAME}: {out}")
    return out.stdout


# --------------------------------------------------------------------------- #
# The C ABI
# --------------------------------------------------------------------------- #

_CC = shutil.which("cc") or "/usr/bin/cc"


class CProgram:
    """A C program compiled against the installed libcmark and run.

        prog = CProgram('''
            #include <cmark.h>
            #include <stdio.h>
            int main(void) {
                char *html = cmark_markdown_to_html("*hi*\\n", 5, CMARK_OPT_DEFAULT);
                fputs(html, stdout);
                return 0;
            }
        ''')
        out = prog.run()
        assert out.stdout == b"<p><em>hi</em></p>\\n"

    Compilation flags come from the installed libcmark.pc via pkg-config, which
    is how a downstream package would find them -- so a submission whose .pc file
    is wrong fails to compile a candidate here, and that is a real defect rather
    than an artefact of this helper guessing paths.

    `static=True` links against libcmark.a instead. Worth doing at least once per
    round: the two configurations are separate CMake builds and a rewrite can get
    one right and the other wrong.
    """

    def __init__(self, source: str, *, name: str = "prog",
                 static: bool = False, extra_cflags: tuple[str, ...] = (),
                 extra_ldflags: tuple[str, ...] = ()) -> None:
        self.source = textwrap.dedent(source)
        self.name = name
        self.static = static
        self.dir = SCRATCH / name
        self.dir.mkdir(parents=True, exist_ok=True)
        self.src_path = self.dir / f"{name}.c"
        self.bin_path = self.dir / name
        self.src_path.write_text(self.source, encoding="utf-8")
        self.compile_output: Output | None = None
        self._extra_cflags = tuple(extra_cflags)
        self._extra_ldflags = tuple(extra_ldflags)

    # -- compiling ---------------------------------------------------------

    def _flags(self) -> tuple[list[str], list[str]]:
        cflags = pkg_config("--cflags") or [f"-I{INCLUDEDIR}"]
        if self.static:
            ldflags = [str(LIBDIR / "libcmark.a")]
        else:
            ldflags = pkg_config("--libs") or [f"-L{LIBDIR}", "-lcmark"]
        return cflags + list(self._extra_cflags), ldflags + list(self._extra_ldflags)

    def compile(self, timeout: float = 120.0) -> Output:
        """Compile, returning the compiler's own Output.

        Not an exception, because "a C program that compiles against 0.31.1 does
        not compile against this" is itself a finding about the header or the
        package config, and a candidate should be able to assert it directly.
        """
        cflags, ldflags = self._flags()
        argv = [_CC, "-std=c99", "-O1", "-o", str(self.bin_path),
                str(self.src_path), *cflags, *ldflags]
        self.compile_output = _run(argv, timeout=timeout, cwd=self.dir)
        return self.compile_output

    def build(self, timeout: float = 120.0) -> "CProgram":
        """Compile and raise unless it worked. For the usual case."""
        out = self.compile(timeout=timeout)
        if not out.ok:
            raise AssertionError(
                f"the helper did not compile against {TARGET_NAME}: {out}")
        return self

    # -- running -----------------------------------------------------------

    def run(self, *args: str, stdin: bytes | str = b"",
            timeout: float = 30.0) -> Output:
        """Compile if needed, then run. Returns the program's Output."""
        if self.compile_output is None:
            self.build()
        if not self.bin_path.exists():
            raise AssertionError(f"{self.bin_path} was never built")
        if isinstance(stdin, str):
            stdin = stdin.encode("utf-8")
        return _run([str(self.bin_path), *args], stdin=stdin, timeout=timeout,
                    cwd=self.dir)


def pkg_config(*args: str) -> list[str]:
    """Ask the installed libcmark.pc something. [] if pkg-config failed.

        assert pkg_config("--modversion") == ["0.31.1"]
    """
    out = _run(["pkg-config", *args, "libcmark"], timeout=30.0)
    if not out.ok:
        return []
    return out.stdout.decode("utf-8", "replace").split()


# --------------------------------------------------------------------------- #
# The installed artifacts
# --------------------------------------------------------------------------- #

def library_path(static: bool = False) -> Path:
    """The installed library. Raises if it is not there at all."""
    name = "libcmark.a" if static else "libcmark.so.0.31.1"
    path = LIBDIR / name
    if not path.is_file():
        raise AssertionError(f"{name} is not installed in {LIBDIR} on "
                             f"{TARGET_NAME}; contents: {so_files()}")
    return path


def so_files() -> list[str]:
    """Everything in the install's lib directory, for a failure message."""
    if not LIBDIR.is_dir():
        return []
    return sorted(p.name for p in LIBDIR.iterdir())


def nm_defined(static: bool = False) -> set[str]:
    """The defined, exported symbols of the installed library.

    `nm --defined-only --extern-only`. Used for asking whether every symbol
    cmark.h declares is actually present -- which is in scope. Asking what ELSE
    is present is not: a Rust build exports its own machinery and that is not a
    migration defect.
    """
    path = library_path(static=static)
    out = _run(["nm", "--defined-only", "--extern-only", "--format=posix",
                str(path)], timeout=60.0)
    if not out.ok:
        return set()
    names: set[str] = set()
    for line in out.stdout.decode("utf-8", "replace").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] in "TDBRW":
            names.add(parts[0])
    return names
