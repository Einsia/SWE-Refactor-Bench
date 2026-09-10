"""Fixtures and helpers for the read-only scan.

Deliberately small.  A scan module gets two trees and a text editor's worth of
facility: walk a directory, open a file, sniff the first four bytes of one to see
whether it is a class file, hash one against its counterpart in State A.  It
cannot configure, build, install or run the submission, and there is nothing here
that would let it -- the stage-1 image has no JDK and no CMake, so a module that
tried would fail on the missing program rather than quietly grading a build.

The distinction that matters most in this file is `walk_source`'s: a `.class`
inside an out-of-source build directory is a normal build output, and the same
file at the top of the tree is a prebuilt binary someone checked in.  Build
directories are identified by the markers their generators leave rather than by a
guessed name, so a submission that calls its build directory something unusual is
treated the same way -- and a submission that names a *source* directory `build`
is not given a free pass.

The second is `HEADER_ALLOWLIST` beside `REMOVABLE_PATHS`.  This task deletes
eighteen C sources and nine private headers; `zlib.h` must survive, because the
installed ABI contract is the thing the port has to stay faithful to.  But "must
survive" and "may survive" are different licences and this task grants both:
`zconf.h` and its two templates are on the contract's removable list, so keeping
them is as correct as dropping them.  A header is therefore a finding only when it
is in neither list, and both lists come from the contract rather than from a
literal here.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

import pytest

#: Files that identify a zlib checkout, used to resolve the mount.  All three are
#: State A content the migration preserves -- `LICENSE` and `zlib.h` are named in
#: source-contract.json's `preserved_paths`, and `CMakeLists.txt` stays the build
#: driver -- so a submission cannot move the root out from under the scan by
#: deleting them.
ROOT_MARKERS = ("CMakeLists.txt", "LICENSE", "zlib.h")


def _resolve(path: Path) -> Path:
    """``path``, or its single child, whichever is the repository root.

    `original.tar.gz` unpacks with a `repo/` prefix in three of this benchmark's
    four tasks and without one in the fourth, so whether `/opt/original` *is* the
    tree or *contains* it depends on how the operator unpacked it. Getting that
    wrong is not a visible error: a scan pointed one level off walks a directory
    holding one entry, finds no `zlib.h`, derives an empty list of translation
    units, and reports a clean tree. Resolving it here costs a stat and removes
    the whole failure mode.

    Only one level, and only when the level below looks like the repository. A
    tree matching nothing is returned unchanged, so the check that says "this
    mount is wrong" is the one that reports it.
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

#: The frozen description of State A, shipped beside the scan.  Every list a
#: module needs that is *about the upstream release* comes from here or from
#: ORIGINAL, never from a literal in a test: a literal is a second description of
#: the same release, free to drift from the first.
CONTRACT_PATH = Path(os.environ.get(
    "SRB_CONTRACT", "/tests/audit/data/source-contract.json"))


def _load_contract() -> dict:
    try:
        return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


CONTRACT = _load_contract()

#: Files a generator leaves at the top of a directory it owns.  `.gradle` and
#: `maven-status` are here because a Java submission may reasonably drive javac
#: from something other than CMake for its own convenience, and the output of
#: that is a build directory whatever it is called.
BUILD_DIR_MARKERS = ("CMakeCache.txt", "CACHEDIR.TAG", "build.ninja",
                     ".ninja_deps", "maven-status", ".gradle")

#: Names that identify a generated directory even when it holds no marker.
GENERATED_DIR_NAMES = {"__pycache__", ".git", ".mvn", "classes"}

#: C-family source extensions.  `.S` is assembly, which upstream ships none of
#: but a submission reaching for a fast CRC might.
C_SOURCE_SUFFIXES = (".c", ".cc", ".cpp", ".cxx", ".c++", ".m", ".mm", ".s",
                     ".sx", ".inc", ".hpp", ".hh", ".hxx")

#: Compiled artefacts that have no business in a source submission.  `.class` and
#: `.jar` matter more here than the ELF suffixes do: the shape this task has to
#: catch is a jar built somewhere else and committed, because that is a rewrite
#: nobody has to have written.
BINARY_SUFFIXES = (".o", ".obj", ".a", ".lib", ".so", ".dylib", ".dll",
                   ".class", ".jar", ".jmod", ".war")

#: The one header that survives: the installed ABI contract.  Read from the
#: contract, with the literal as a fallback so a missing contract degrades to a
#: scan that is still correct rather than one that flags every header.
#:
#: `header_allowlist` is nested under `forbidden_paths`, and reading it from the
#: top level -- as this line did -- silently returns None and leaves the literal in
#: force.  Harmless while the two agree, which is exactly what makes it worth
#: fixing: a contract edit would have changed nothing here and the scan would have
#: gone on enforcing a copy of the old value.  Every other contract read in this
#: suite goes through the nested path.
HEADER_ALLOWLIST = set(
    CONTRACT.get("forbidden_paths", {}).get("header_allowlist") or ["zlib.h"]
)

#: State A material the submission *may* drop -- the contract's own word is that
#: removing it "is expected", and the brief calls the same list "May go".  So a
#: file here is a finding in neither direction: keeping it is licensed and dropping
#: it is licensed.
#:
#: Kept apart from HEADER_ALLOWLIST because the two words mean opposite things to
#: the two stages.  Stage 2's `gate_no_private_headers` computes
#: `missing = allowlist - present` and *fails* on it, so a name added to the
#: allowlist becomes a name the submission is required to keep -- which would turn
#: the other half of the same licence into a graded failure.  "May be present" and
#: "must be present" cannot share a key.
#:
#: Entries are basenames, relative paths, and directory prefixes (`win32/`), so
#: callers match on whichever form they hold.
REMOVABLE_PATHS = frozenset(
    CONTRACT.get("removable_paths", {}).get("paths") or ()
)

#: Suffixes a text scan will open at all.
TEXT_SUFFIXES = (".java", ".toml", ".cmake", ".txt", ".in", ".h", ".md", ".sh",
                 ".py", ".yml", ".yaml", ".json", ".cfg", ".xml", ".gradle",
                 ".properties", ".cmakein", ".map", ".mf", "")


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


def is_class_file(path: Path) -> bool:
    """A JVM class file, by its magic rather than by its name.

    Worth having separately from the suffix list: the shape to catch is bytecode
    committed into the tree, and bytecode does not have to be called `.class` to
    be loadable.
    """
    return magic(path, 4) == b"\xca\xfe\xba\xbe"


def is_zip(path: Path) -> bool:
    """A zip container, which is what a jar is.

    `PK\\x03\\x04` is a plain member, `PK\\x05\\x06` an empty archive and
    `PK\\x07\\x08` a spanned one; all three are archives a jar could arrive as.
    """
    head = magic(path, 4)
    return head in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")


def locate(path: Path, needle: str) -> int | None:
    """The 1-based line ``needle`` first appears on, for a citation."""
    for lineno, line in enumerate(read_text(path).splitlines(), 1):
        if needle in line:
            return lineno
    return None


def find_text(path: Path, needle: str) -> tuple[int, str] | None:
    """``needle``'s first occurrence, as ``(1-based lineno, that line)``.

    The plain-substring counterpart to `find_name`: no word boundaries, because
    the needles this serves -- `java.util.zip`, `/logs/verifier` -- are distinctive
    strings rather than project names, and a boundary rule would be the wrong
    semantics for them.

    It exists to return the *line*, and that is not a convenience.  A finding whose
    evidence slot holds the search string tells a reviewer nothing it did not
    already know from the check's own name: `('PORTING.md', 4, 'java.util.zip',
    'contains java.util.zip')` renders identically for `import
    java.util.zip.Deflater;` and for a comment saying the port deliberately does
    not use it.  Both are things a correct submission for this task writes -- the
    brief forbids the API, so a conscientious port documents that it avoided it --
    and the gate being fed is `no-jdk-deflate`, the sharpest one here.  Measured:
    the two shapes produced byte-identical findings, while the sibling check
    twenty lines away quoted the line and was dismissible at a glance.

    So the quote is part of the return type rather than something each call site
    remembers to compute.  Two sites hand-rolled it and one of them forgot; a
    helper that cannot return a hit without a line is the version of this that does
    not regress.

    Containment is tested against the whole text, not line by line, so this fires
    on exactly what `needle in read_text(path)` fired on -- the point is to change
    what a finding shows, never which findings there are.  Case-sensitive, for the
    same reason.
    """
    text = read_text(path)
    offset = text.find(needle)
    if offset < 0:
        return None
    lineno = line_of(text, offset)
    lines = text.splitlines()
    quote = lines[lineno - 1] if 0 < lineno <= len(lines) else needle
    return lineno, quote.strip()[:200]


#: A camelCase boundary, treated as a word break before a name search.
_CAMEL_BREAK = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def find_name(path: Path, name: str) -> tuple[int, str] | None:
    """The first line naming ``name``, as ``(1-based lineno, that line)``.

    A project name has to match as a *name*, not as a fragment of a longer word.
    `tinflate` is not mentioned by `testInflate` -- which is what zlib's own
    `test_inflate` is called once it is spelled in Java -- but a bare
    ``name.lower() in text`` says it is, because "tes|tinflate" contains it.

    That mattered: the finding it produced was `mentions tinflate` against
    `Example.java`, i.e. an accusation of vendoring a third-party decoder, aimed at
    a faithful port of the library's own test driver, feeding the required
    `no-foreign-port` gate.  Nothing before a real submission could show it up,
    because the C spells the name `test_inflate` and the underscore breaks the
    accidental match -- so State A passes and every correct Java port fails.

    A camelCase boundary therefore counts as a word break, and a match may not
    begin part-way into a word.  The right-hand side stays open, because vendoring
    genuinely does appear as a prefix -- `JZlibDeflater`, `pakoInflate` -- and
    demanding a break there would miss what the check exists for.  Returning the
    line as well as its number is the other half: a reviewer who is shown
    `private static void testInflate(byte[] compr, ...)` can dismiss a false
    positive without opening the file, and `locate()` returns ``None`` rather than a
    line number for a name that only matched case-insensitively.
    """
    pattern = re.compile(r"(?<![0-9A-Za-z])" + re.escape(name), re.IGNORECASE)
    for lineno, line in enumerate(read_text(path).splitlines(), 1):
        if pattern.search(_CAMEL_BREAK.sub(" ", line)):
            return lineno, line.strip()[:200]
    return None


def line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def first_match(text: str, pattern: str, flags: int = 0):
    return re.search(pattern, text, flags)


def count_java_logic_lines(paths: list[Path]) -> int:
    """Lines of Java that are neither blank nor comment-only.

    The same counter State A was measured with, so the floor in the contract and
    the number here mean the same thing.  Deliberately crude: a `//` inside a
    string literal ends the line early and a `/*` inside one starts a block that
    is not there.  Both mistakes undercount, which is the safe direction for a
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
            if stripped.startswith("//") or stripped.startswith("*"):
                continue
            total += 1
    return total


def authored(paths: list[Path]) -> list[Path]:
    """Those of ``paths`` that are not byte-identical to State A's copy.

    The filter every text search in this suite runs through, and it is not an
    optimisation.  This task *preserves* README, ChangeLog and rfc1950/1951/1952,
    and between them those five files discuss gzip at length, name `puff` and
    `miniz`, use the word "native", and mention Sun.  A grep over the whole tree
    therefore produces the same half-dozen findings for every honest submission,
    which is worse than producing none: a reviewer who sees the identical noise
    every run learns to skip the section, and the one run where the noise is a
    real finding looks exactly like the others.

    So the scope is what the submission *authored*.  A file whose bytes equal
    upstream's says only what upstream said, and upstream is not the thing under
    review.  The comparison is by relative path and then by hash, so a preserved
    file that was edited is authored again -- which is the right answer, because an
    edit to rfc1951 is an edit to the specification the port is graded against.

    The comparison is two-sided, and that makes it a silent no-op when ORIGINAL is
    empty: `upstream.is_file()` is False for every path, nothing is dropped, and
    every preserved file becomes "authored" -- so the filter stops doing the one
    thing it exists to do at exactly the moment nothing else can cover for it.
    `/opt/original` is a mountpoint the Dockerfile creates empty for Harbor to fill,
    so a hand-driven run reaches this state by omitting one `-v`.

    Measured on an otherwise-clean tree with the mount empty: 9 flagged
    observations, 4 of them upstream's own text -- README:38 for `java.util.zip`,
    ChangeLog:5 and README:52 for `miniz`, ChangeLog:190 for `puff`, ChangeLog:1123
    for `-lz`.  Real lines in real files, shaped exactly like a real finding.  The
    damage is to the digest -- observations that are true of upstream and say
    nothing about the port, in the section whose whole value is that a reviewer
    still reads it on the run where it carries something real.

    Still deliberately not guarded here, after measuring all three ways of guarding
    it, because the report belongs one check away and does not need to be made here
    to be made.  `closure.test_both_trees_are_mounted` asserts that `/opt/original`
    holds one of `ROOT_MARKERS`, which an empty mount does not, so a blind run fails
    there with a message naming the cause, in the same digest those observations
    land in.  A hard failure and not an abstention: it does not stop the ladder, and
    a reviewer reading past it can still score the tree.  It is the whole of the
    protection, which is worth knowing before anyone weakens it.

    What guarding it here costs, each way worse than the noise:

      * `pytest.skip` in this function: the shared plugin converts an unlicensed
        skip to a fail (correctly -- the environment is offline, so a skip means the
        artifact was never produced), and `srb_skip_ok` is a per-test marker a helper
        cannot apply to its caller.  130 checks, 51 flagged, 42 of them the same
        converted skip, burying the one real finding in the same digest.
      * a guard in run-scan.sh, before pytest: contributes an error *unit* and zero
        *checks*, and `digest` renders checks -- so `{{findings}}` renders as "(the
        scan produced no findings)" with `result.status` still `'ok'` and
        `scan.summary()` reporting `errored: 0`, because that counts checks too.  A
        dead scan reading to the reviewer as reassurance.
      * `return []`: the text searches pass having looked at nothing, which is the
        one outcome a reviewer cannot tell from a real pass.

    The operational guard belongs in whatever drives the stage, not in the filter.
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
def java_files(files: list[Path]) -> list[Path]:
    return [p for p in files if p.suffix == ".java"]


@pytest.fixture(scope="session")
def text_files(files: list[Path]) -> list[Path]:
    """Text the submission wrote or changed.

    Filtered through `authored`, so a preserved README does not produce a finding
    about the word "gzip" on every honest run.  Nothing is lost by it: a preserved
    file that acquired the harness's vocabulary, or a jzlib reference, differs from
    upstream and is therefore authored again.  What the filter drops is only text
    that upstream already contained and the submission did not touch.

    The complement lives in the contract module: `authored` stops a preserved file
    from producing greps, and `test_upstream_documentation_unmodified` reports that
    it changed at all.
    """
    return authored([p for p in files if p.suffix.lower() in TEXT_SUFFIXES])
