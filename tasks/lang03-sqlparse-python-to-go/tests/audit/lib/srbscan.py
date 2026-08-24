"""Fixtures and helpers for the read-only scan.

Deliberately small.  A scan module gets two trees and a text editor's worth of
facility: walk a directory, open a file, sniff the first four bytes of one to see
whether it is an object file, hash one against its counterpart in State A.  It
cannot configure, build, install or run the submission, and there is nothing here
that would let it -- the stage-1 image has no Go toolchain and no Python beyond the
one running pytest, so a module that tried would fail on the missing program rather
than quietly grading a build.

Two decisions in this file carry most of its weight, and both differ from how the
C-to-Rust task does it.

`walk_source` excludes version-control directories and nothing else.  lang01 has to
distinguish a `.o` that CMake produced in a build directory from the same file
checked in at the top of the tree, so it identifies build directories by the markers
their generators leave.  Go has no in-source build directory to identify: `go build`
writes to a cache outside the tree and `go install` to a prefix, so anything under
`/opt/workspace` at scan time was put there by the submission.  Inventing a marker
list here would only create a way to hide a file -- name a directory the right thing
and the scan stops looking in it.

`__pycache__` is deliberately *not* excluded, which is the opposite of lang01's
choice for the same directory name.  There it is noise from someone's editor.  Here
it is on the contract's forbidden list and its contents are `.pyc` files, which are
exactly the evidence this task's central gate is about.  Excluding it would make
`no .pyc anywhere` pass while `no __pycache__` failed, on one tree, for one reason.

`PYTHON_SUFFIXES` is separate from `INTERPRETER_NAMES`.  The first is what a file
is; the second is what a script reaches for.  A submission with no `.py` anywhere
can still have a Makefile that runs `python3 -c`, and those are different findings
with different explanations, so they are different checks.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

import pytest

#: Files that identify an sqlparse checkout, used to resolve the mount.
#:
#: Every one is a path source-contract.json's `preserved_paths` requires to survive
#: the migration, which is the property that matters.  `sqlparse/` would identify
#: State A perfectly and is exactly what the submission is required to delete, so a
#: marker list including it would resolve the original correctly and the submission
#: one level off.  `docs/` is absent for a subtler version of the same problem: the
#: man page is allowed to move to `share/man/man1/sqlformat.1`, so a submission may
#: legitimately have no `docs/` at all.
ROOT_MARKERS = ("LICENSE", "AUTHORS", "CHANGELOG", "CONTRIBUTING.md", "SECURITY.md")


def _resolve(path: Path) -> Path:
    """``path``, or its single child, whichever is the repository root.

    `original.tar.gz` unpacks with a `repo/` prefix in three of this benchmark's
    tasks and without one in lang03, so whether `/opt/original` *is* the tree or
    *contains* it depends on how the operator unpacked it.  Getting that wrong is
    not a visible error: a scan pointed one level off walks a directory holding one
    entry, finds no `sqlparse/`, derives an empty list of modules, and reports that
    every one of them was deleted.  Resolving it here costs a stat and removes the
    whole failure mode.

    Only one level, and only when the level below looks like the repository.  A tree
    matching nothing is returned unchanged, so the check that says "this mount is
    wrong" is the one that reports it.
    """
    if not path.is_dir():
        return path
    if any((path / marker).exists() for marker in ROOT_MARKERS):
        return path
    children = [c for c in path.iterdir() if c.is_dir()]
    if len(children) == 1 and any((children[0] / m).exists() for m in ROOT_MARKERS):
        return children[0]
    return path


REPO = _resolve(Path(os.environ.get("SRB_REPO", "/opt/workspace")))
ORIGINAL = _resolve(Path(os.environ.get("SRB_ORIGINAL", "/opt/original")))

#: Directories the walk does not enter.  Version control only: a `.git` holding a
#: packed history of State A would otherwise give every per-file check thousands of
#: blobs to report, and the check that cares about a VCS directory looks for the
#: directory itself.  See the module docstring for why the list stops here.
SKIP_DIR_NAMES = {".git", ".hg", ".svn", ".bzr", "_darcs", "CVS"}

#: The five suffixes the contract forbids outright, anywhere, with no exceptions.
PYTHON_SUFFIXES = (".py", ".pyi", ".pyc", ".pyo", ".pyd")

#: Other spellings of Python source.  Not contract violations by suffix -- a `.pyx`
#: is not on the forbidden list -- but a file with one of these in a Go repository
#: is Python that has been given a name the suffix check does not read.
PYTHON_ADJACENT_SUFFIXES = (".pyx", ".pxd", ".pyw", ".ipynb", ".pth", ".egg-info")

#: Compiled artefacts that have no business in a source submission.  `.so` covers
#: both a C shared object and a CPython extension module; `.whl` and `.egg` are
#: built Python distributions, which is how a vendored reference would arrive.
BINARY_SUFFIXES = (".o", ".obj", ".a", ".lib", ".so", ".dylib", ".dll", ".exe",
                   ".whl", ".egg", ".pyc", ".pyo", ".pyd", ".lo", ".la", ".test")

#: Archive suffixes.  An sdist of the reference is a `.tar.gz`, so these are read
#: as "something is packed here", and the module reports the name for a reader.
ARCHIVE_SUFFIXES = (".tar", ".tgz", ".gz", ".bz2", ".xz", ".zip", ".zst", ".7z")

#: Interpreter names a build file or script would reach for.  `uv` and `pipx` are
#: here because either one installs and runs Python without naming it.
INTERPRETER_NAMES = ("python", "python3", "python3.10", "python3.11", "python3.12",
                     "python3.13", "cpython", "pypy", "pypy3", "pip", "pip3",
                     "pipx", "uv", "virtualenv", "conda", "poetry", "tox")

#: Suffixes a text scan will open at all.  `.rst` and `.1` are in because README.rst
#: and the man page are preserved paths a check reads; the empty string is in because
#: LICENSE, AUTHORS, CHANGELOG, TODO and Makefile have no suffix.
TEXT_SUFFIXES = (".go", ".mod", ".sum", ".work", ".toml", ".txt", ".md", ".rst",
                 ".sh", ".bash", ".mk", ".yml", ".yaml", ".json", ".cfg", ".ini",
                 ".in", ".1", ".sql", ".c", ".h", ".env", "")

#: The floor source-contract.json sets on Go logic lines, and the size of the Python
#: it replaces.  Both are read from the contract at check time rather than trusted
#: from here; these are the values the check's message quotes when it explains the
#: comparison, and a drift between the two is itself worth a failure.
MIN_GO_LOGIC_LINES = 3500
STATE_A_PYTHON_LINES = 4024


def walk_source(root: Path) -> list[Path]:
    """Every submitted file, excluding version-control directories."""
    out: list[Path] = []
    if not root.is_dir():
        return out
    for current, dirs, files in os.walk(root):
        here = Path(current)
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIR_NAMES)
        for name in sorted(files):
            out.append(here / name)
    return out


def walk_dirs(root: Path) -> list[Path]:
    """Every directory in the tree, version-control directories included.

    The walk that finds a `.git` has to be allowed to see it, which is the one
    place the exclusion in `walk_source` would hide the answer.  Reported as
    directories rather than as their contents, which is what the check is about.
    """
    out: list[Path] = []
    if not root.is_dir():
        return out
    for current, dirs, _ in os.walk(root):
        here = Path(current)
        dirs[:] = sorted(dirs)
        out.extend(here / d for d in dirs)
    return out


def rel(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def read_text(path: Path, limit: int = 4_000_000) -> str:
    try:
        with path.open("rb") as handle:
            return handle.read(limit).decode("utf-8", "replace")
    except OSError:
        return ""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
    except OSError:
        return ""
    return digest.hexdigest()


def magic(path: Path, size: int = 8) -> bytes:
    try:
        with path.open("rb") as handle:
            return handle.read(size)
    except OSError:
        return b""


def is_elf(path: Path) -> bool:
    return magic(path, 4) == b"\x7fELF"


def is_archive(path: Path) -> bool:
    return magic(path, 8).startswith(b"!<arch>")


def is_pyc(path: Path) -> bool:
    """A CPython bytecode file, by its magic rather than by its name.

    Bytes 2-4 of a `.pyc` are always `\\r\\n` -- the pair is there so that a file
    sent over a channel which translates line endings is corrupted rather than
    silently loaded -- and bytes 0-2 are the version's magic number, little endian.
    The `\\r\\n` alone is a weak test, since plenty of files have those two bytes at
    that offset; pairing it with the magic's range is what makes this worth running.

    The range covers CPython 3.x, whose magics run from 3390 (3.7) up.  A 2.x
    bytecode file is not recognised, and does not need to be: this function exists
    for a `.pyc` that was *renamed*, and every `.pyc` still called one is caught by
    suffix.
    """
    head = magic(path, 4)
    if len(head) != 4 or head[2:4] != b"\r\n":
        return False
    return 3000 <= int.from_bytes(head[:2], "little") <= 4100


def is_zip(path: Path) -> bool:
    """A zip container, which is what a wheel, an egg and a `.pyz` all are."""
    return magic(path, 4)[:2] == b"PK"


def locate(path: Path, needle: str) -> int | None:
    """The 1-based line ``needle`` first appears on, for a citation."""
    for lineno, line in enumerate(read_text(path).splitlines(), 1):
        if needle in line:
            return lineno
    return None


def line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def first_match(text: str, pattern: str, flags: int = 0):
    return re.search(pattern, text, flags)


def count_go_logic_lines(paths: list[Path]) -> int:
    """Lines of Go that are neither blank nor comment-only.

    Counts `_test.go` files only if the caller passed them, which the check does
    not: the floor in source-contract.json is about the implementation, and a port
    could otherwise reach it on table-driven test data alone.
    """
    total = 0
    for path in paths:
        in_block = False
        for line in read_text(path).splitlines():
            stripped = line.strip()
            if in_block:
                if "*/" in stripped:
                    in_block = False
                continue
            if not stripped:
                continue
            if stripped.startswith("/*"):
                if "*/" not in stripped:
                    in_block = True
                continue
            if stripped.startswith("//"):
                continue
            total += 1
    return total


def go_imports(path: Path) -> list[tuple[int, str]]:
    """Every import path in one Go file, with the line it is on.

    Both spellings: a factored `import ( ... )` block and a single `import "x"`.
    Named, dotted and blank imports all reach the same place, since `_ "os/exec"`
    runs the package's init and is not a lesser import than the plain form.

    Regex rather than a Go parser because there is no Go in this image.  The failure
    mode is a string in a comment that looks like an import line; a check reporting
    one of these hands the reviewer the line, and the reviewer has the file open.
    """
    out: list[tuple[int, str]] = []
    text = read_text(path)
    for match in re.finditer(r"^\s*import\s*\(([^)]*)\)", text, re.MULTILINE):
        base = line_of(text, match.start())
        for offset, raw in enumerate(match.group(1).splitlines()):
            entry = re.search(r'"([^"]+)"', raw)
            if entry:
                out.append((base + offset, entry.group(1)))
    for match in re.finditer(r'^\s*import\s+(?:[\w.]+\s+)?"([^"]+)"',
                             text, re.MULTILINE):
        out.append((line_of(text, match.start()), match.group(1)))
    return out


def state_a_python_modules() -> list[str]:
    """State A's implementation modules, as paths relative to the root.

    Derived from the mounted original rather than listed, for the reason lang01's
    equivalent gives: a list here would be a second description of the upstream
    release, free to drift from the first.  Sorted, and empty when the mount is
    empty -- callers substitute their own fallback so that collection succeeds
    against the empty mount point the build-time check uses.
    """
    package = ORIGINAL / "sqlparse"
    if not package.is_dir():
        return []
    return sorted(
        rel(ORIGINAL, p) for p in package.rglob("*.py") if p.is_file()
    )


@pytest.fixture(scope="session")
def repo() -> Path:
    return REPO


@pytest.fixture(scope="session")
def original() -> Path:
    return ORIGINAL


@pytest.fixture(scope="session")
def files() -> list[Path]:
    return walk_source(REPO)


@pytest.fixture(scope="session")
def dirs() -> list[Path]:
    return walk_dirs(REPO)


@pytest.fixture(scope="session")
def go_files(files: list[Path]) -> list[Path]:
    return [p for p in files if p.suffix == ".go"]


@pytest.fixture(scope="session")
def go_impl_files(go_files: list[Path]) -> list[Path]:
    return [p for p in go_files if not p.name.endswith("_test.go")]


@pytest.fixture(scope="session")
def text_files(files: list[Path]) -> list[Path]:
    return [p for p in files if p.suffix.lower() in TEXT_SUFFIXES]


def _load_contract() -> dict:
    """source-contract.json, as the scan's own copy ships it.

    The scan reads the contract rather than restating it.  lang01's scan transcribes
    its equivalents into constants at the top of each module, with a comment naming
    where each came from, and that is one description of the task per module free to
    drift from the real one -- which is the shape of two bugs already recorded
    against this benchmark.  The contract is not secret: the solver's own image
    installs it at /opt/swerefactor/source-contract.json, mode 0444, so nothing is
    leaked by the scan reading the same bytes the author was given.

    A module level constant rather than a fixture because the lists in it drive
    `parametrize`, which runs at collection time when no fixture exists yet.

    Returns {} when the file is unreadable, and every caller substitutes a fallback
    so collection still succeeds.  A silently empty contract would make several
    checks vacuous, so `test_contract_file_is_readable` fails on it directly.
    """
    import json

    path = Path(__file__).resolve().parent.parent / "data" / "source-contract.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


CONTRACT = _load_contract()


@pytest.fixture(scope="session")
def contract() -> dict:
    return CONTRACT
