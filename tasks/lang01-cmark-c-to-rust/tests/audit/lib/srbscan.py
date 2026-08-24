"""Fixtures and helpers for the read-only scan.

Deliberately small.  A scan module gets two trees and a text editor's worth of
facility: walk a directory, open a file, sniff the first four bytes of one to see
whether it is an object file, hash one against its counterpart in State A.  It
cannot configure, build, install or run the submission, and there is nothing here
that would let it -- the stage-1 image has no Rust toolchain and no CMake, so a
module that tried would fail on the missing program rather than quietly grading a
build.

The distinction that matters most in this file is `walk_source`'s: a `.o` inside
an out-of-source build directory is a normal build output, and the same file at
the top of the tree is a prebuilt binary someone checked in.  Build directories
are identified by the markers their generators leave rather than by a guessed
name, so a submission that calls its build directory something unusual is treated
the same way -- and a submission that names a *source* directory `target` is not
given a free pass.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

import pytest

#: Files that identify a cmark checkout, used to resolve the mount. All three are
#: State A content the migration preserves -- `COPYING` and `man/` are named in
#: source-contract.json's `preserved_paths`, and `src/` is where the port goes --
#: so a submission cannot move the root out from under the scan by deleting them.
ROOT_MARKERS = ("src", "COPYING", "man")


def _resolve(path: Path) -> Path:
    """``path``, or its single child, whichever is the repository root.

    `original.tar.gz` unpacks with a `repo/` prefix in three of this benchmark's
    four tasks and without one in the fourth, so whether `/opt/original` *is* the
    tree or *contains* it depends on how the operator unpacked it. Getting that
    wrong is not a visible error: a scan pointed one level off walks a directory
    holding one entry, finds no `src/`, derives an empty list of translation units,
    and reports a clean tree. Resolving it here costs a stat and removes the whole
    failure mode.

    Only one level, and only when the level below looks like the repository. A tree
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

#: Stands in for a `parametrize` list derived from State A when State A turned out
#: not to be readable.  A `@parametrize("x", DERIVED or [UNREADABLE])` keeps the
#: check collectable -- an empty list would silently collect nothing, which reads
#: as a clean tree -- but the sentinel then arrives as the parameter, and what the
#: check does with it is the whole of whether the finding is honest.
UNREADABLE = "<state-a-unreadable>"


def require_state_a(param: str) -> None:
    """Fail as a harness fault when a derived parameter is the sentinel.

    Called first in every check parametrized over a list read out of State A.  A
    check that skips this step does not go quiet -- it goes ahead and compares the
    submission against the sentinel, and the message comes out as a claim about the
    submission.  The copyright check is the sharp case: with an unreadable reference
    tree it would report

        COPYING no longer carries '<state-a-unreadable>'

    which is an accusation of stripping a copyright holder -- the most serious thing
    this module can say -- produced by a mount that failed before the submission was
    ever read.  Advisory or not, it goes into the reviewer's prompt as text, and the
    reviewer has no way to tell that string from a real missing attribution.
    """
    if param == UNREADABLE:
        pytest.fail(
            f"SCAN IS BLIND, NOT A SUBMISSION DEFECT: this check is parametrized "
            f"over a list read from State A, and State A was not readable at "
            f"{ORIGINAL}, so the list came back empty. Every check in this scan "
            f"that compares the submission against State A is unreliable for this "
            f"run. Nothing here is a claim about the submission.",
            pytrace=False)

#: Files a generator leaves at the top of a directory it owns.
BUILD_DIR_MARKERS = ("CMakeCache.txt", "CACHEDIR.TAG", ".rustc_info.json",
                     "build.ninja", ".ninja_deps")

#: Names that identify a generated directory even when it holds no marker.
GENERATED_DIR_NAMES = {"target", "__pycache__", ".git"}

#: C-family source extensions.  `.re` is re2c's input, which is what State A's
#: scanner is written in; `.inc` is how its generated tables are included.
C_SOURCE_SUFFIXES = (".c", ".cc", ".cpp", ".cxx", ".c++", ".m", ".mm", ".s",
                     ".sx", ".re", ".inc", ".hpp", ".hh", ".hxx")

#: Compiled artefacts that have no business in a source submission.
BINARY_SUFFIXES = (".o", ".obj", ".a", ".lib", ".so", ".dylib", ".dll", ".rlib",
                   ".rmeta", ".lo", ".la")

#: The one header that survives: the installed ABI contract.
HEADER_ALLOWLIST = {"src/cmark.h"}

#: Generated headers.  A checked-in copy is acceptable -- neither is C *code*.
GENERATED_HEADER_NAMES = {"cmark_export.h", "cmark_version.h"}

#: Suffixes a text scan will open at all.
TEXT_SUFFIXES = (".rs", ".toml", ".cmake", ".txt", ".in", ".h", ".md", ".sh",
                 ".py", ".yml", ".yaml", ".json", ".cfg", ".lock", "")


def _is_generated_dir(path: Path) -> bool:
    if path.name in GENERATED_DIR_NAMES:
        return True
    return any((path / marker).exists() for marker in BUILD_DIR_MARKERS)


def walk_source(root: Path) -> list[Path]:
    """Every submitted file, excluding directories the build produced."""
    out: list[Path] = []
    if not root.is_dir():
        return out
    for current, dirs, files in os.walk(root):
        here = Path(current)
        dirs[:] = sorted(d for d in dirs if not _is_generated_dir(here / d))
        for name in sorted(files):
            out.append(here / name)
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


def count_rust_logic_lines(paths: list[Path]) -> int:
    """Lines of Rust that are neither blank nor comment-only."""
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
def rust_files(files: list[Path]) -> list[Path]:
    return [p for p in files if p.suffix == ".rs"]


@pytest.fixture(scope="session")
def text_files(files: list[Path]) -> list[Path]:
    return [p for p in files if p.suffix.lower() in TEXT_SUFFIXES]
