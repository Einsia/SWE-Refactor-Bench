"""Fixtures and helpers for the read-only scan.

Deliberately small.  A scan module gets two trees and a text editor's worth of
facility: walk a directory, open a file, sniff the first bytes of one to see whether
it is an executable, hash one against its counterpart in State A.  It cannot
configure, build, install or run the submission, and there is nothing here that
would let it -- the stage-1 image has no Rust toolchain and no node, so a module
that tried would fail on the missing program rather than quietly grading a build.

Three decisions in this file carry most of its weight.

`walk_source`'s: a `.d` file or a stripped ELF inside `target/` is a normal cargo
output, and the same file at the top of the tree is a prebuilt binary someone
checked in.  Build directories are identified by the markers their generators leave
rather than by a guessed name, so a submission that calls its build directory
something unusual is treated the same way -- and a submission that names a *source*
directory `target` is not given a free pass.

`authored`'s: every text search is scoped to files whose bytes differ from State A's
copy at the same path.  This repository is a JavaScript parser, so the words a
naive search for danger would use -- `node`, `eval`, `Function`, `import`,
`acorn` -- are the vocabulary of the thing being ported.  They appear thousands of
times in State A and they appear in every honest port.  Searching only authored text
does not make the search precise, but it removes the class of finding that is
identical on every run, which is the class a reviewer learns to skip.

`FIXTURE_PREFIX`'s: `test/bench/fixtures/` holds six third-party JavaScript bundles
-- jQuery, ember.js and friends -- that State A uses as benchmark input.  They are
`.js` files and the contract says they go with the rest of the JavaScript, so a
submission that keeps them fails `javascript-left`.  But they are also the largest
JavaScript in the tree by two orders of magnitude, so a `no-embedded-reference`
search that treats them as State A implementation source reports every long
JavaScript-looking string in the submission as a match for jQuery.  The two
questions are separated here rather than in each module.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

import pytest

#: Files that identify an acorn checkout, used to resolve the mount.  All four are
#: State A content the migration keeps or replaces in place -- `README.md` and
#: `AUTHORS` are in the contract's `retained_paths`, and the three package
#: directories are the ones the Rust workspace's crates are named after -- so a
#: submission cannot move the root out from under the scan by deleting them.
ROOT_MARKERS = ("README.md", "AUTHORS", "acorn", "acorn-walk")


def _resolve(path: Path) -> Path:
    """``path``, or its single child, whichever is the repository root.

    `original.tar.gz` unpacks with a `repo/` prefix in three of this benchmark's
    tasks and without one in the fourth, so whether `/opt/original` *is* the tree or
    *contains* it depends on how the operator unpacked it.  Getting that wrong is
    not a visible error: a scan pointed one level off walks a directory holding one
    entry, finds no `acorn/`, derives an empty list of anchor files, and reports a
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

#: The frozen description of State A, shipped beside the scan.  Every list a module
#: needs that is *about the upstream release* comes from here or from ORIGINAL, never
#: from a literal in a test: a literal is a second description of the same release,
#: free to drift from the first.
CONTRACT_PATH = Path(os.environ.get(
    "SRB_CONTRACT", "/tests/audit/data/source-contract.json"))


def _load_contract() -> dict:
    try:
        return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


CONTRACT = _load_contract()

#: Files a generator leaves at the top of a directory it owns.  `CACHEDIR.TAG` is
#: what cargo writes into `target/`; the rest cover a submission that drove the
#: build from something other than the Makefile for its own convenience.
BUILD_DIR_MARKERS = ("CACHEDIR.TAG", "CMakeCache.txt", "build.ninja",
                     ".ninja_deps", ".rustc_info.json")

#: Names that identify a generated or vendored directory even when it holds no
#: marker.  `node_modules` is here as well as in the contract's forbidden names: the
#: contract makes its presence a finding, and this list stops the scan walking 40 MB
#: of third-party JavaScript to find out.
GENERATED_DIR_NAMES = {"__pycache__", ".git", "node_modules", ".cargo",
                       "incremental", ".fingerprint"}

#: JavaScript and TypeScript source extensions.  Taken from the contract, with this
#: as the fallback so a missing contract degrades to a scan that is still correct.
JS_SUFFIX_FALLBACK = (".js", ".mjs", ".cjs", ".jsx", ".ts", ".mts", ".cts",
                      ".tsx", ".node", ".wasm", ".map")

#: Compiled artefacts that have no business in a source submission.  The shape this
#: task has to catch is a Rust binary built somewhere else and committed, because a
#: `make` that copies a prebuilt binary into place is a rewrite nobody has to have
#: written.
BINARY_SUFFIXES = (".o", ".obj", ".a", ".lib", ".so", ".dylib", ".dll", ".rlib",
                   ".rmeta", ".exe", ".wasm", ".node")

#: Suffixes a text scan will open at all.
TEXT_SUFFIXES = (".rs", ".toml", ".md", ".txt", ".json", ".sh", ".py", ".yml",
                 ".yaml", ".cfg", ".lock", ".mk", ".in", ".js", ".mjs", ".cjs",
                 ".ts", "")

#: State A's benchmark inputs: six third-party bundles, not acorn's implementation.
#: Separated from implementation source because they are two orders of magnitude
#: larger than anything acorn wrote, and a similarity search that includes them
#: matches on jQuery rather than on the parser.
FIXTURE_PREFIX = "test/bench/fixtures/"

#: State A directories holding the implementation itself, as opposed to its tests,
#: its documentation or its benchmark inputs.  Derived from the contract's anchor
#: files, with this as the fallback.
IMPL_PREFIX_FALLBACK = ("acorn/src/", "acorn-loose/src/", "acorn-walk/src/")


def js_suffixes() -> tuple[str, ...]:
    """The forbidden extensions, from the contract."""
    listed = ((CONTRACT.get("forbidden_paths") or {}).get("extensions")
              or list(JS_SUFFIX_FALLBACK))
    return tuple(str(s).lower() for s in listed)


def forbidden_names() -> tuple[str, ...]:
    listed = (CONTRACT.get("forbidden_paths") or {}).get("names") or []
    return tuple(str(s) for s in listed)


def impl_prefixes() -> tuple[str, ...]:
    """The State A directories that hold the implementation.

    Derived from `state_a.anchor_files` -- the parent directory of each -- so the
    list follows the contract rather than restating it.  `acorn/src/bin/` collapses
    into `acorn/src/` because a prefix test only needs the shallowest.
    """
    anchors = (CONTRACT.get("state_a") or {}).get("anchor_files") or []
    prefixes = set()
    for entry in anchors:
        parts = str(entry).split("/")
        if len(parts) >= 2:
            prefixes.add("/".join(parts[:2]) + "/")
    return tuple(sorted(prefixes)) or IMPL_PREFIX_FALLBACK


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
    """A WebAssembly module, by its magic rather than by its name.

    Worth having separately from the suffix list: a `.wasm` renamed to `.dat` and
    loaded at run time is JavaScript's compiled form arriving under another name.
    """
    return magic(path, 4) == b"\x00asm"


def looks_binary(path: Path) -> bool:
    """A NUL in the first 8 KiB, which no source file in either language has."""
    try:
        with path.open("rb") as handle:
            return b"\x00" in handle.read(8192)
    except OSError:
        return False


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


def search_all(paths: list[Path], pattern: str, flags: int = 0,
               limit: int = 12) -> list[tuple[Path, int, str]]:
    """Every match of ``pattern``, as (path, line number, the line's text).

    Returns citations rather than a boolean, because everything this scan produces
    is read by someone who has to go and look: a finding that says "found it" and
    not where is a finding the reviewer has to rediscover.
    """
    out: list[tuple[Path, int, str]] = []
    compiled = re.compile(pattern, flags)
    for path in paths:
        text = read_text(path)
        for match in compiled.finditer(text):
            lineno = line_of(text, match.start())
            line = text.splitlines()[lineno - 1] if lineno else ""
            out.append((path, lineno, line.strip()[:200]))
            if len(out) >= limit:
                return out
    return out


def count_rust_logic_lines(paths: list[Path]) -> int:
    """Lines of Rust that are neither blank nor comment-only.

    The same counter the contract's floor was measured with, so the number in the
    contract and the number here mean the same thing.  Deliberately crude: a `//`
    inside a string literal ends the line early and a `/*` inside one starts a block
    that is not there.  Both mistakes undercount, which is the safe direction for a
    floor -- a submission cannot pass it by writing code this counter mis-reads.
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
            if stripped.startswith(("//", "*", "#![", "#[")):
                continue
            total += 1
    return total


def authored(paths: list[Path]) -> list[Path]:
    """Those of ``paths`` that are not byte-identical to State A's copy.

    The filter every text search in this suite runs through, and it is not an
    optimisation.  This repository is a JavaScript parser: `node`, `eval`,
    `Function`, `import`, `async`, `acorn` and `regexp` are its subject matter, not
    signals.  They appear in State A thousands of times and in every faithful port,
    so a search over the whole tree produces the same findings for every honest
    submission -- which is worse than producing none.  A reviewer who sees identical
    noise every run learns to skip the section, and the one run where the noise is a
    real finding looks exactly like the others.

    So the scope is what the submission *authored*.  A file whose bytes equal
    upstream's says only what upstream said, and upstream is not the thing under
    review.  The comparison is by relative path and then by hash, so a preserved file
    that was edited is authored again -- which is the right answer, because an edit
    to `acorn/CHANGELOG.md` is an edit to a document the contract requires to
    survive.
    """
    out: list[Path] = []
    for path in paths:
        relpath = rel(REPO, path)
        upstream = ORIGINAL / relpath
        if upstream.is_file() and path.is_file():
            if sha256(path) == sha256(upstream):
                continue
        out.append(path)
    return out


@pytest.fixture(scope="session")
def repo() -> Path:
    return REPO


@pytest.fixture(scope="session")
def original() -> Path:
    return ORIGINAL


@pytest.fixture(scope="session")
def contract() -> dict:
    return CONTRACT


@pytest.fixture(scope="session")
def files() -> list[Path]:
    return walk_source(REPO)


@pytest.fixture(scope="session")
def rust_files(files: list[Path]) -> list[Path]:
    return [p for p in files if p.suffix == ".rs"]


@pytest.fixture(scope="session")
def text_files(files: list[Path]) -> list[Path]:
    """Text the submission wrote or changed.

    Filtered through `authored`, so a preserved CHANGELOG does not produce a finding
    about the word `eval` on every honest run.  Nothing is lost by it: a preserved
    file that acquired the harness's vocabulary, or a swc reference, differs from
    upstream and is therefore authored again.  What the filter drops is only text
    that upstream already contained and the submission did not touch.

    The complement lives in the contract module: `authored` stops a preserved file
    from producing greps, and `test_retained_document_unmodified` reports that it
    changed at all.
    """
    return authored([p for p in files if p.suffix.lower() in TEXT_SUFFIXES])
