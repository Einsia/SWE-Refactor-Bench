"""Fixtures and helpers for the read-only scan.

Deliberately small.  A scan module gets two trees and a text editor's worth of
facility: walk a directory, open a file, sniff the first bytes of one to see what
kind of thing it is, hash one against its counterpart in State A.  It cannot
install, build or run the submission, and there is nothing here that would let it --
the stage-1 image has no node, no npm and no tsc, so a module that tried would fail
on the missing program rather than quietly grading a build.

Three decisions in this file carry most of its weight, and the first two are forced
by this language pair rather than chosen.

**`walk_source` keeps `dist/`, and no suffix is forbidden by itself.**  Every other
task in this benchmark can name the old language's extension and treat any file
carrying it as a finding.  Here `node` runs both States, `dist/` is *required* to be
full of emitted JavaScript, and `tools/build.js` is a legitimate build helper that
runs before anything is compiled.  So there is no extension whose mere presence
means anything, and a module that reported `.js` files would report the same dozen
findings for every honest submission.  What the scan does instead is report *where*
each one is and *what is in it*, and leave the judgement to a reader.  See
`javascript_report` and `diagnostic_code_density`.

**`authored` is the scope of every text search.**  A file whose bytes equal State
A's says only what State A said, and State A is not the thing under review.  On this
task that filter earns its keep twice over: `README.swerefactor.md` is 16,665 bytes
documenting the probe protocol, it names every one of the ten protocol codes and
quotes response JSON, and it is *supposed* to survive unedited.  A grep over the
whole tree would surface it every run, and a reviewer who sees identical noise every
run learns to skip the section -- so the one run where it is real looks like all the
others.  Scoping by what was written rather than by what it is called is also what
makes the scan hard to sidestep: it does not care about the extension.

**`count_logic_lines` is the contract's own counter.**  `source-contract.json`
states State A's 6,376 logic lines, and the floor the closure module compares a
submission against is a number in the same file.  Those two only mean the same thing
if one function produces both, so this is that function, and `tests/check-task.py`
re-runs it over the tarball to hold it to the contract's per-file rows.  It is crude
about string literals -- a `"//"` inside one truncates the line -- and the crudeness
undercounts, which is the safe direction for a floor: a submission cannot get over
one by writing code this counter mis-reads.  It is *not* crude about which delimiter
comes first, and that is not fussiness.  `src/jsonata.js:674` is
`// this is the equivalent of //* in XPath`; a counter that looks for `/*` before
`//` reads that as an unterminated block comment and swallows the thirteen lines of
`recurseDescendants` that follow it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

import pytest

#: Files that identify this checkout, used to resolve the mount.  The resolver runs
#: against *both* trees -- the submission at REPO and State A at ORIGINAL -- so a
#: marker is only useful if it survives in the tree it is asked about.
#:
#: All four of these are in State A and all four are preserved paths, which makes
#: them the set that identifies either tree.  That is a difference from the C#
#: task, where `package.json` marked a submission only: here State A is already a
#: node package, so the submission inherits the file rather than authoring it.
#:
#: `tsconfig.json` is appended as a submission-only marker for the case where every
#: preserved path was deleted -- the tree still has to resolve, so that the check
#: which reports the deletion is the one that speaks rather than a silent walk over
#: an empty directory.  Deliberately not `src/*.js`, which State B is expected to
#: have removed, and not `src/*.ts`, which State A does not have.
ROOT_MARKERS = ("package.json", "LICENSE", "README.swerefactor.md", "README.md",
                "tsconfig.json")


def _resolve(path: Path) -> Path:
    """``path``, or its single child, whichever is the repository root.

    `original.tar.gz` unpacks with a `repo/` prefix here, so whether
    `/opt/original` *is* the tree or *contains* it depends on how the operator
    unpacked it.  Getting that wrong is not a visible error: a scan pointed one
    level off walks a directory holding one entry, finds no TypeScript, and reports
    a clean tree.  Resolving it here costs a stat and removes the whole failure
    mode.

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
#: needs that is *about State A* comes from here or from ORIGINAL, never from a
#: literal in a test: a literal is a second description of the same release, free to
#: drift from the first.
CONTRACT_PATH = Path(os.environ.get(
    "SRB_CONTRACT", "/tests/audit/data/source-contract.json"))


def _load_contract() -> dict:
    try:
        return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


CONTRACT = _load_contract()

#: Directories that are never authored: a package manager's cache, a VCS store, an
#: interpreter's cache.  Dropped from the walk because nothing in them was written
#: by the submission.  `node_modules` is here *and* in the contract's
#: `forbidden_paths.directories`, which is not a contradiction: the walk does not
#: read what is inside one, and a separate check reports that it exists at all.
NEVER_AUTHORED_DIRS = {"node_modules", ".git", "__pycache__", ".npm", ".cache"}

#: The one directory a build writes into.  Asked explicitly rather than applied by
#: the walk -- see this module's docstring.
DECLARED_OUTPUT_DIRS = {"dist"}

#: Markers a generator leaves at the top of a directory it owns, for the case where
#: a submission emitted somewhere other than `dist/`.
BUILD_DIR_MARKERS = ("tsconfig.tsbuildinfo", ".tsbuildinfo", "CACHEDIR.TAG")

#: JavaScript, by extension.  Note what this is *not* used for: nothing here treats
#: a match as a finding.  It selects the files the JavaScript inventory describes.
JAVASCRIPT_SUFFIXES = (".js", ".mjs", ".cjs", ".jsx")

#: TypeScript, by extension.
TYPESCRIPT_SUFFIXES = (".ts", ".mts", ".cts", ".tsx")

#: Compiled artefacts with no business in a source submission.  Kept in step with
#: the contract's `forbidden_paths.extensions` by tests/check-task.py; the tuple is
#: here so a module can ask the question without loading JSON, not as a second
#: source of truth.
BINARY_SUFFIXES = (".node", ".wasm", ".so", ".dylib", ".dll", ".exe",
                   ".a", ".o", ".obj", ".class", ".jar", ".pyd")

#: Suffixes a text scan will open at all.
TEXT_SUFFIXES = (".ts", ".mts", ".cts", ".tsx", ".js", ".mjs", ".cjs", ".jsx",
                 ".json", ".jsonc", ".toml", ".md", ".txt", ".sh", ".bash",
                 ".py", ".yml", ".yaml", ".cfg", ".ini", ".xml", ".map",
                 ".d.ts", "")

#: JSONata's own diagnostic codes: one letter and four digits.  Derived from State A
#: by `state_a_diagnostic_codes()` rather than listed, because the list is 101 long
#: and a literal copy of it here would be a second description of the same release.
DIAGNOSTIC_CODE = re.compile(r"\b[TDS]\d{4}\b")


def _is_never_authored(path: Path) -> bool:
    return path.name in NEVER_AUTHORED_DIRS


def is_declared_output(path: Path, root: Path | None = None) -> bool:
    """Whether ``path`` sits inside a directory the build writes into.

    Asked explicitly rather than applied by the walk, because on this task both
    readings of a file under `dist/` are live at once: it is the graded artifact and
    it is the place a prebuilt answer would hide.  A check that means "this is
    ordinary build output" says so by calling this; a check that means "this file
    exists" does not have to care.
    """
    root = root or REPO
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        parts = path.parts
    return any(part in DECLARED_OUTPUT_DIRS for part in parts[:-1])


def has_build_marker(path: Path) -> bool:
    return any((path / marker).exists() for marker in BUILD_DIR_MARKERS)


def walk_source(root: Path) -> list[Path]:
    """Every submitted file, excluding only directories nobody authored."""
    out: list[Path] = []
    if not root.is_dir():
        return out
    for current, dirs, files in os.walk(root):
        here = Path(current)
        dirs[:] = sorted(d for d in dirs if not _is_never_authored(here / d))
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


def is_wasm(path: Path) -> bool:
    """A WebAssembly module.

    Worth a sniff of its own rather than a suffix check: wasm is the one way to put
    an implementation into a node process that no source read finds, and it does not
    have to be called `.wasm` to be loaded by `WebAssembly.instantiate`.
    """
    return magic(path, 4) == b"\x00asm"


def is_zip(path: Path) -> bool:
    """A zip container -- an npm pack, or an archive hiding a source tree.

    ``PK\\x03\\x04`` is a plain member, ``PK\\x05\\x06`` an empty archive and
    ``PK\\x07\\x08`` a spanned one; all three are containers a copy of State A could
    arrive in.
    """
    return magic(path, 4) in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")


def is_gzip(path: Path) -> bool:
    return magic(path, 2) == b"\x1f\x8b"


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


def string_literals(text: str) -> list[tuple[int, str, str]]:
    """Every string and template literal in ``text``, as ``(offset, quote, body)``.

    A scanner rather than a regex, and the reason is worth recording because the
    regex version shipped first and was wrong on all 107 of its hits.  A pattern
    like ``(['"`])((?:\\\\.|(?!\\1)[^\\\\])*)\\1`` reads the apostrophe in a comment
    -- ``it's``, ``State A's``, ``upstream's`` -- as an opening quote and runs to the
    next apostrophe several lines away, so it reports a 900-byte "string literal"
    that is actually two comments with code between them.  This repository is full
    of prose comments, so every one of the 107 was noise of that shape.  A reviewer
    handed 107 bogus citations reads none of them.

    So this walks the text once, tracking which of five states it is in: code, a
    line comment, a block comment, a quoted string, or a regex literal.  Only
    literals found in the code state are returned, which is the only way to get this
    right in a language where ``//`` can appear inside a string, a quote can appear
    inside a comment, and ``/`` can mean either division or the start of a regex.

    The regex-versus-division call uses the standard heuristic -- a ``/`` begins a
    regex unless the previous significant character could end an expression -- and it
    matters here specifically because ``src/probe-fixtures.js`` and
    ``src/datetime.js`` both contain regex literals holding quote characters.
    Mis-lexing one of those opens a phantom string that swallows the rest of the
    file.
    """
    out: list[tuple[int, str, str]] = []
    i, n = 0, len(text)
    prev_significant = ""
    while i < n:
        ch = text[i]
        if ch in "'\"`":
            start = i
            quote = ch
            i += 1
            body: list[str] = []
            while i < n:
                c = text[i]
                if c == "\\":
                    body.append(text[i:i + 2])
                    i += 2
                    continue
                if c == quote:
                    i += 1
                    break
                # An unterminated single- or double-quoted string ends at the line
                # break; a template literal legitimately spans lines.
                if c == "\n" and quote != "`":
                    break
                body.append(c)
                i += 1
            out.append((start, quote, "".join(body)))
            prev_significant = quote
            continue
        if ch == "/" and i + 1 < n:
            nxt = text[i + 1]
            if nxt == "/":
                i = text.find("\n", i)
                if i < 0:
                    break
                continue
            if nxt == "*":
                end = text.find("*/", i + 2)
                i = n if end < 0 else end + 2
                continue
            # Division or a regex literal.  If the previous significant character
            # could end an expression, this is division.
            if prev_significant and (prev_significant.isalnum()
                                     or prev_significant in "_$)]"):
                prev_significant = ch
                i += 1
                continue
            i += 1
            in_class = False
            while i < n:
                c = text[i]
                if c == "\\":
                    i += 2
                    continue
                if c == "[":
                    in_class = True
                elif c == "]":
                    in_class = False
                elif c == "/" and not in_class:
                    i += 1
                    break
                elif c == "\n":
                    break
                i += 1
            prev_significant = "/"
            continue
        if not ch.isspace():
            prev_significant = ch
        i += 1
    return out


def code_view(text: str) -> str:
    """``text`` with comments and string bodies blanked, offsets preserved.

    Same length as the input, and every newline kept where it was, so an offset into
    the result is an offset into the original and ``line_of`` still reports the true
    line.  Comments become spaces; a string literal keeps its quotes and its length
    but its body becomes ``x``.

    For the searches that are about *structure* rather than content -- where the
    array literals are, whether a `require` is real code or a line in a comment --
    this is the view to run them over.  A pattern applied to raw JavaScript finds
    matches in prose, and this repository's comments contain a great deal of prose
    about JavaScript.
    """
    out = list(text)
    i, n = 0, len(text)
    prev_significant = ""

    def blank(start: int, stop: int) -> None:
        for k in range(start, min(stop, n)):
            if out[k] != "\n":
                out[k] = " "

    while i < n:
        ch = text[i]
        if ch in "'\"`":
            quote = ch
            j = i + 1
            while j < n:
                c = text[j]
                if c == "\\":
                    j += 2
                    continue
                if c == quote:
                    j += 1
                    break
                if c == "\n" and quote != "`":
                    break
                j += 1
            for k in range(i + 1, min(j - 1, n)):
                if out[k] != "\n":
                    out[k] = "x"
            prev_significant = quote
            i = j
            continue
        if ch == "/" and i + 1 < n:
            nxt = text[i + 1]
            if nxt == "/":
                end = text.find("\n", i)
                end = n if end < 0 else end
                blank(i, end)
                i = end
                continue
            if nxt == "*":
                end = text.find("*/", i + 2)
                end = n if end < 0 else end + 2
                blank(i, end)
                i = end
                continue
            if not (prev_significant and (prev_significant.isalnum()
                                          or prev_significant in "_$)]")):
                j = i + 1
                in_class = False
                while j < n:
                    c = text[j]
                    if c == "\\":
                        j += 2
                        continue
                    if c == "[":
                        in_class = True
                    elif c == "]":
                        in_class = False
                    elif c == "/" and not in_class:
                        j += 1
                        break
                    elif c == "\n":
                        break
                    j += 1
                blank(i, j)
                prev_significant = "/"
                i = j
                continue
        if not ch.isspace():
            prev_significant = ch
        i += 1
    return "".join(out)


def count_logic_lines(paths: list[Path]) -> int:
    """Lines that are neither blank nor comment-only.

    The counter that produced every ``logic_lines`` figure in
    ``source-contract.json``, applied here to the submission's TypeScript so that
    the floor and State A's 6,376 are the same measurement.  See this module's
    docstring for what it is and is not careful about, and
    ``tests/check-task.py::test_logic_line_counter_reproduces_contract`` for the
    check that keeps it honest.
    """
    total = 0
    for path in paths:
        in_block = False
        for line in read_text(path).splitlines():
            s = line.strip()
            code = False
            while True:
                if in_block:
                    end = s.find("*/")
                    if end < 0:
                        s = ""
                        break
                    s = s[end + 2:].lstrip()
                    in_block = False
                    continue
                line_comment = s.find("//")
                block_comment = s.find("/*")
                if line_comment >= 0 and (block_comment < 0
                                          or line_comment < block_comment):
                    if s[:line_comment].strip():
                        code = True
                    s = ""
                    break
                if block_comment >= 0:
                    if s[:block_comment].strip():
                        code = True
                    s = s[block_comment + 2:]
                    in_block = True
                    continue
                break
            if code or (s.strip() and not s.strip().startswith("*")):
                total += 1
    return total


#: The name the source contract and ``check-task.py`` use; the generic one is what
#: the other tasks' copies of this module export.
count_ts_logic_lines = count_logic_lines


def authored(paths: list[Path]) -> list[Path]:
    """Those of ``paths`` that are not byte-identical to State A's copy.

    See this module's docstring for why every text search runs through this.  The
    comparison is by relative path and then by hash, so a preserved file that was
    edited is authored again -- which is the right answer, because an edit to
    `LICENSE` or to `README.swerefactor.md` is a change the review should see.
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


def state_a_files() -> list[Path]:
    """Every file in State A, for the modules that compare against it."""
    return walk_source(ORIGINAL)


def state_a_hashes() -> dict[str, str]:
    """State A by digest, keyed by relative path.

    Used for "is a byte-identical copy of this file anywhere in the submission",
    which is a different question from "is it still at its old path" -- and the one
    that finds `src/jsonata.js` moved to `reference/engine.txt`.
    """
    return {rel(ORIGINAL, p): sha256(p) for p in state_a_files() if p.is_file()}


def state_a_diagnostic_codes() -> set[str]:
    """JSONata's diagnostic codes, read out of State A.

    101 of them.  Derived rather than listed for the reason given on
    ``DIAGNOSTIC_CODE``: a literal here would be a second description of the same
    release, free to drift from the first.
    """
    codes: set[str] = set()
    for path in state_a_files():
        if path.is_file() and path.suffix.lower() in TEXT_SUFFIXES:
            codes |= set(DIAGNOSTIC_CODE.findall(read_text(path)))
    return codes


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
def ts_files(files: list[Path]) -> list[Path]:
    """TypeScript the submission wrote, excluding build output.

    `.d.ts` is included and on this task that matters more than it looks.  With
    `"types": []` and no `@types/node` in reach, a port has to declare whatever of
    node it touches, so a `.d.ts` in the source tree is ordinary authored work --
    and it is also where an ambient `require` would be declared.  `authored` still
    runs, though State A's only TypeScript is the hand-written `jsonata.d.ts`, which
    is exactly the file a submission must not have carried across unchanged.
    """
    return authored([p for p in files
                     if p.suffix in TYPESCRIPT_SUFFIXES
                     and not is_declared_output(p)])


@pytest.fixture(scope="session")
def js_files(files: list[Path]) -> list[Path]:
    """JavaScript anywhere in the submission, build output included.

    Not filtered through `authored` and not filtered by location, because both
    filters would remove the thing a reader needs: whether a `.js` is State A's
    untouched file matters, and so does whether it is under `dist/`.  The module
    that reports these groups them; nothing treats the list itself as a finding.
    """
    return [p for p in files if p.suffix in JAVASCRIPT_SUFFIXES]


@pytest.fixture(scope="session")
def text_files(files: list[Path]) -> list[Path]:
    """Text the submission wrote or changed.

    Filtered through `authored`, so `README.swerefactor.md` does not produce a finding
    about the word `corpus` on every honest run.  Nothing is lost by it: a preserved
    file that acquired the harness's vocabulary differs from upstream and is
    therefore authored again.  What the filter drops is only text State A already
    contained and the submission did not touch.

    The complement lives in the contract module: `authored` stops a preserved file
    from producing greps, and a separate check reports that it changed at all.
    """
    return authored([p for p in files if p.suffix.lower() in TEXT_SUFFIXES])


@pytest.fixture(scope="session")
def authored_code(files: list[Path]) -> list[Path]:
    """Text the submission wrote, outside build output.

    The scope for every awareness and provenance sweep: what the author put in the
    source tree.  `dist/` is excluded here specifically -- a token in emitted
    JavaScript is a token in the `.ts` it came from, and reporting both doubles
    every finding.
    """
    return authored([p for p in files
                     if p.suffix.lower() in TEXT_SUFFIXES
                     and not is_declared_output(p, REPO)])
