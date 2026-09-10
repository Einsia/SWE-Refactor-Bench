"""Helpers for the read-only scan.

Deliberately small.  A scan module gets two trees and a text editor's worth of
facility: walk a directory, open a file, sniff the first four bytes to see whether
it is a compiled image, hash one file against its counterpart in State A.  It
cannot restore, build, publish or run the submission, and there is nothing here
that would let it -- the stage-1 image has no .NET SDK and no C++ toolchain, so a
module that tried would fail on the missing program rather than quietly grading a
build.

Nothing here decides anything either.  `swerefactor.scan` records every check with
`required = false`; the gate is the prose questions in evaluation.toml, answered by
a reviewer with both trees open.  What these produce is the half a string can
settle -- *which* of State A's C++ translation units is still on disk, whether
`stdlib/std.jsonnet` still hashes to what upstream shipped -- so the reviewer
spends its turns on the half a string cannot: whether the C# is an interpreter or a
shell around something else, and whether `std.jsonnet` is being *executed* or was
transliterated into C# and left in the tree as scenery.

The distinction that matters most is `walk_source`'s.  A `.dll` inside `bin/Debug`
is a normal build output; the same file at the top of the tree is a compiled
implementation someone checked in.  Build directories are identified by the markers
the toolchain leaves rather than by a guessed name, so a submission whose build
directory has an unusual name is treated the same way -- and a submission that
names a *source* directory `obj` gets no free pass.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

import pytest

#: Files that identify a jsonnet checkout, used to resolve the mount.  All three
#: are State A content the migration preserves -- `stdlib/` holds std.jsonnet,
#: `test_suite/` is the conformance record, `LICENSE` is the licence -- so a
#: submission cannot move the root out from under the scan by deleting them.
ROOT_MARKERS = ("stdlib", "test_suite", "LICENSE")


def _resolve(path: Path) -> Path:
    """``path``, or its single child, whichever is the repository root.

    Whether `/opt/original` *is* the tree or *contains* it depends on how the
    tarball was unpacked, and this one unpacks with a `jsonnet-0.20.0/` prefix.
    Getting that wrong is not a visible error: a scan pointed one level off walks a
    directory holding one entry, finds no `core/`, derives an empty list of
    translation units, and reports a clean tree.  Resolving it here costs a stat and
    removes the whole failure mode.

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


class ContractMissing(RuntimeError):
    """The shipped contract could not be read.

    Raised rather than defaulted.  Every path list in this stage comes out of
    `source-contract.json`, so a missing file would collapse each of them to nothing
    and produce a scan that reports a clean tree -- the one failure mode where
    silence is actively wrong.
    """


#: The contract, read from the stage image rather than restated here.
#:
#: `source-contract.json` is shipped into this stage's build context as
#: `data/source-contract.json` and is the same bytes as `environment/`'s copy --
#: `swerefactor validate` compares them and fails the task if they diverge, which is
#: the reason to read it instead of typing its contents into three scan modules.  A
#: second copy of a path list is a second description of the migration, free to
#: drift from the first, and a scan asserting against a stale list either misdirects
#: the reviewer or passes vacuously.
#:
#: The submitter has this file too, at `/opt/swerefactor/source-contract.json` (the
#: path the environment image installs it to; verified against the built image, not
#: read off the Dockerfile).  Nothing
#: in it is an answer: it states which paths must go and which must survive, not what
#: any of the CLIs print.
CONTRACT_PATH = Path(os.environ.get(
    "SRB_CONTRACT",
    str(Path(__file__).resolve().parent.parent / "data" / "source-contract.json")))


def _load_contract() -> dict:
    import json
    try:
        text = CONTRACT_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise ContractMissing(
            f"no source-contract.json at {CONTRACT_PATH}: {exc}. Every path list "
            f"in this stage is derived from it.") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ContractMissing(f"{CONTRACT_PATH} is not valid JSON: {exc}") from exc
    if data.get("task") != "lang06-jsonnet-cpp-to-csharp":
        raise ContractMissing(
            f"{CONTRACT_PATH} declares task {data.get('task')!r}; this scan belongs "
            f"to lang06-jsonnet-cpp-to-csharp")
    return data


CONTRACT = _load_contract()


def contract_section(*keys: str):
    """``CONTRACT`` walked by ``keys``, raising when a key is absent.

    The raise is the point.  `CONTRACT.get("forbidden_paths", {}).get("directories",
    [])` on a renamed key yields an empty list and seven checks that pass without
    looking at anything; this yields a scan that fails to import, which
    `collect-check.sh` turns into a build-time error rather than a clean report.
    """
    node = CONTRACT
    for index, key in enumerate(keys):
        if not isinstance(node, dict) or key not in node:
            raise ContractMissing(
                "source-contract.json has no %s (looking for %s)"
                % (".".join(keys[:index + 1]), ".".join(keys)))
        node = node[key]
    return node

#: What MSBuild and cmake leave at the top of a directory they own.
BUILD_DIR_MARKERS = ("project.assets.json", "CMakeCache.txt", "build.ninja",
                     ".ninja_deps", "project.nuget.cache", "CACHEDIR.TAG")

#: Names that identify a generated directory even when it holds no marker.  `bin`
#: and `obj` are MSBuild's; a source directory called either is vanishingly rare
#: and would be worth a finding of its own.
GENERATED_DIR_NAMES = {"bin", "obj", "__pycache__", ".git", ".vs", "node_modules"}

#: C++-family source extensions.  State A's implementation is C++11; `.inc` is how
#: its generated tables are included and `.ipp` is a vendored header convention.
CPP_SOURCE_SUFFIXES = (".c", ".cc", ".cpp", ".cxx", ".c++", ".h", ".hh", ".hpp",
                       ".hxx", ".inc", ".ipp", ".m", ".mm", ".s", ".sx")

#: Compiled artefacts that have no business in a source submission.  `.dll` covers
#: both a managed assembly and a native library; the check that reports one says
#: which by reading its magic.
BINARY_SUFFIXES = (".o", ".obj", ".a", ".lib", ".so", ".dylib", ".dll", ".exe",
                   ".pdb", ".lo", ".la", ".pyd", ".nupkg")

#: Suffixes a text scan will open at all.
TEXT_SUFFIXES = (".cs", ".csproj", ".sln", ".props", ".targets", ".config",
                 ".json", ".toml", ".txt", ".in", ".md", ".sh", ".py", ".yml",
                 ".yaml", ".cfg", ".jsonnet", ".libsonnet", ".editorconfig", "")


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


def is_pe(path: Path) -> bool:
    """A PE image, which is what both a managed assembly and a Windows DLL are.

    Distinguishing managed from native needs the CLI header, which is stage 2's
    job -- it has the metadata reader and a publish directory to point it at.  Here
    the useful fact is only that a compiled image is checked in.
    """
    return magic(path, 2) == b"MZ"


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


def count_logic_lines(paths: list[Path]) -> int:
    """Lines of C# that are neither blank nor comment-only.

    A floor on this is the crudest possible statement of "there is an
    implementation in here", and it is stated as a floor rather than a target
    precisely because it cannot tell good code from bad.  What it can tell is 800
    lines from 15,000, and a submission at 800 has not reimplemented a language.
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
def cs_files(files: list[Path]) -> list[Path]:
    return [p for p in files if p.suffix == ".cs"]


@pytest.fixture(scope="session")
def text_files(files: list[Path]) -> list[Path]:
    return [p for p in files if p.suffix.lower() in TEXT_SUFFIXES]
