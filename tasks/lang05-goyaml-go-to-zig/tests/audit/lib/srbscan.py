"""Fixtures and helpers for the read-only scan.

Deliberately small.  A scan module gets two trees and a text editor's worth of
facility: walk a directory, open a file, sniff the first four bytes of one to see
whether it is an object file, hash one against its counterpart in State A.  It
cannot build, install or run the submission, and there is nothing here that would
let it -- the stage-1 image has no Zig toolchain and no Go, so a module that tried
would fail on the missing program rather than quietly grading a build.

The distinction that matters most in this file is `walk_source`'s: an object file
inside `zig-out/` or `.zig-cache/` is a normal build output, and the same file at
the top of the tree is a prebuilt binary someone checked in.  Build directories are
identified by the markers their generators leave as well as by name, so a
submission that calls its build directory something unusual is treated the same
way -- and a submission that names a *source* directory `zig-out` is not given a
free pass, because the marker check is what clears a directory, not the name alone.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

import pytest

#: Files that identify a go-yaml checkout, used to resolve the mount.  All are
#: State A content the migration keeps -- LICENSE, NOTICE and README.md are
#: `retained_paths` in source-contract.json, and build.zig is the build State B is
#: required to have -- so a submission cannot move the root out from under the scan
#: by deleting them.
ROOT_MARKERS = ("LICENSE", "NOTICE", "README.md", "build.zig")


def _resolve(path: Path) -> Path:
    """``path``, or its single child, whichever is the repository root.

    `original.tar.gz` unpacks with a `repo/` prefix in some of this benchmark's
    tasks and without one in others, so whether `/opt/original` *is* the tree or
    *contains* it depends on how the operator unpacked it.  Getting that wrong is
    not a visible error: a scan pointed one level off walks a directory holding one
    entry, finds no Go, derives an empty list of translation units, and reports a
    clean tree.  Resolving it here costs a stat and removes the whole failure mode.

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

#: Stands in for a population derived from State A when State A turned out not to
#: be readable.  Two checks here are derived that way and they need it in different
#: shapes: `closure` parametrizes over State A's Go filenames, and `contract` hashes
#: State A's Go sources into a reverse lookup.  Neither can simply guard in the
#: fixture -- the build-time collect-check runs with /opt/original deliberately
#: empty, so a hard failure there would fail the image build -- and neither can
#: fall back to `[]` or `{}`, because an empty population makes the two checks that
#: catch a renamed copy of State A collect, pass vacuously and read clean.  The
#: sentinel keeps collection working and makes the run say so.
UNREADABLE = "<state-a-unreadable>"


def require_state_a(param: str) -> None:
    """Fail as a harness fault when a derived population is the sentinel.

    Called first in every check whose population is read out of State A.  The point
    is that the alternative is not silence: `test_original_go_file_is_gone` and
    `test_no_original_go_file_by_content` are the two checks in this suite that
    survive `cp scannerc.go scanner.zig.bak`, and against an unreadable State A they
    have nothing to compare.  A licensed skip is neutral by contract, so a scan with
    both of them skipped is indistinguishable from a scan of a tree that retired the
    Go properly -- and the review that reads the scan answers pass or fail, where
    "no findings" is the reading it will take.  So the blindness has to be a finding
    of its own.

    `pytrace=False` because a traceback adds nothing to a harness fault: the message
    is the whole of the information and the frames are this file.  Note that it fires
    *once* per check and not once per name -- an unreadable State A leaves `ORIGINAL_GO`
    empty, so the parametrize collapses to the single sentinel case rather than to the
    twenty-four filenames a mounted tree yields.  That is the difference from lang01's
    `_require_in_state_a`, which is parametrized over thirteen hardcoded paths that do
    each fire, and whose comment records what that costs: enough guard paragraphs push
    the real findings past `scan.digest()`'s render limit.
    """
    if param == UNREADABLE:
        pytest.fail(
            f"SCAN IS BLIND, NOT A SUBMISSION DEFECT: this check is derived from a "
            f"population read out of State A, and State A was not readable at "
            f"{ORIGINAL}, so the population came back empty. Every check in this "
            f"scan that compares the submission against State A is unreliable for "
            f"this run. Nothing here is a claim about the submission.",
            pytrace=False)


#: Files a generator leaves at the top of a directory it owns.  Zig writes no
#: cache tag of its own into `zig-out`, so the name list below carries most of the
#: weight for this task and these markers catch the rest.
BUILD_DIR_MARKERS = ("CACHEDIR.TAG", "build.ninja", ".ninja_deps",
                     "CMakeCache.txt")

#: Names that identify a generated directory even when it holds no marker.
#: `zig-out` is `zig build`'s install prefix and `.zig-cache` / `zig-cache` are its
#: intermediates; task.toml excludes all three from the collected artifact, so a
#: file inside one did not travel with the submission in the first place.
GENERATED_DIR_NAMES = {"zig-out", ".zig-cache", "zig-cache", "__pycache__",
                       ".git"}

#: Go and C-family source extensions.  Both are forbidden by
#: source-contract.json's `forbidden_paths`: the Go is State A's implementation,
#: and C is a way of getting an implementation the pinned Zig toolchain did not
#: compile.
GO_SOURCE_SUFFIXES = (".go",)
C_SOURCE_SUFFIXES = (".c", ".h", ".cc", ".cpp", ".cxx", ".c++", ".hpp", ".hh",
                     ".hxx", ".m", ".mm", ".s", ".sx")

#: Compiled artefacts that have no business in a source submission.
BINARY_SUFFIXES = (".o", ".obj", ".a", ".lib", ".so", ".dylib", ".dll", ".wasm",
                   ".lo", ".la")

#: Names forbidden outright by the contract, at any depth.
FORBIDDEN_NAMES = ("go.mod", "go.sum", "go.work", "go.work.sum", "vendor",
                   "Makefile.go", ".golangci.yml")

#: Files the contract requires to survive the migration.
RETAINED_PATHS = ("LICENSE", "NOTICE", "README.md")

#: Suffixes a text scan will open at all.
TEXT_SUFFIXES = (".zig", ".zon", ".toml", ".txt", ".md", ".sh", ".py", ".yml",
                 ".yaml", ".json", ".cfg", ".lock", ".in", "")


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


def is_wasm(path: Path) -> bool:
    return magic(path, 4) == b"\x00asm"


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


def zig_logic_lines(text: str) -> int:
    """Non-blank, non-comment lines of Zig.

    A deliberate second copy of ``vlib.zig_logic_lines`` from the stage-2 suite.
    The two stages are separate Docker build contexts -- stage 1's is
    ``tests/audit`` and it cannot read ``tests/behavioural/lib`` -- so
    sharing the file is not available and the choice is between duplicating the
    function and having stage 1 count differently from the stage that set the
    floor.  Duplicated, and ``tests/check-task.py`` runs both implementations over
    the same awkward inputs and fails if they disagree: a copy nobody cross-checks
    is how the floor and the figure justifying it end up in different units.

    Zig has one comment syntax and no block comments, which makes this honest
    without a parser: ``//``, ``///`` and ``//!`` all start a line comment that
    runs to the end of the line.  A ``//`` inside a string literal would be
    miscounted if it were the first thing on the line, which cannot happen -- a
    line beginning with a string literal begins with a quote or a backslash.
    """
    total = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("//"):
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
def zig_files(files: list[Path]) -> list[Path]:
    return [p for p in files if p.suffix == ".zig"]


@pytest.fixture(scope="session")
def text_files(files: list[Path]) -> list[Path]:
    return [p for p in files if p.suffix.lower() in TEXT_SUFFIXES]
