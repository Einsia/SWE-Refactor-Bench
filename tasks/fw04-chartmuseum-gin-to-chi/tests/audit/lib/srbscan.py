"""Fixtures and helpers for the read-only scan.

Deliberately small.  A scan module gets two trees and a text editor's worth of
facility: open a file, walk a directory, read the import block of a Go file.  It
cannot resolve, build, link or run the submission, and there is nothing here that
would let it -- the stage-1 image has no Go toolchain and no module mirror, so a
module that tried would fail on the missing compiler rather than quietly grading
a build.

The one non-trivial helper is ``imports``, and it exists because the alternative
is a regex over the whole file.  A regex counts the module path in a comment, in a
docstring and in a URL; the import block is what the compiler reads.  It is not a
Go parser -- it does not resolve build tags, and a file excluded by one still has
its imports counted here -- which is another reason nothing in this stage is
scored.  A finding says "this file names Gin in its import block"; the reviewer
opens the file and says what that means.

The exemptions are the ones instruction.md publishes to the agent, and they are
here rather than in each module because a scan that searched ``testdata/`` for the
word "gin" would flag the frozen chart fixtures a correct migration still ships.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

REPO = Path(os.environ.get("SRB_REPO", "/opt/workspace"))
ORIGINAL = Path(os.environ.get("SRB_ORIGINAL", "/opt/original"))

#: The three modules the task retires.  Read from the same file the environment
#: image and the behavioural stage read, so there is one list in the tree and a
#: fourth module added to the task is covered without editing a scan.
RETIRED_FILE = Path(os.environ.get(
    "SRB_RETIRED_MODULES",
    str(Path(__file__).resolve().parent.parent / "data" / "retired-modules.txt")))

#: Prose and binary assets.  A migration is documented in Markdown and ships a
#: logo, a packaged chart and a signing key; none of them is a place to look for a
#: live dependency.
EXEMPT_SUFFIXES = (".md", ".rst", ".png", ".jpg", ".jpeg", ".webp", ".svg",
                   ".ico", ".gz", ".tgz", ".prov", ".pem", ".key", ".pub",
                   ".secret", ".zip", ".woff", ".woff2", ".ttf", ".map")

#: Directories a scan does not read.  ``testdata`` holds the frozen charts, whose
#: bytes are load-bearing and whose contents are nobody's implementation;
#: ``vendor`` is exempt from *token* scans but very much not from the vendoring
#: check, which looks for it by name -- see ``vendored_modules``.
EXEMPT_DIRS = (".git", "vendor", "testdata", "_dist", "bin", "testbin",
               "node_modules", "__pycache__", ".cache")

#: Suffixes a text scan will open at all.  The empty string catches Makefile,
#: Dockerfile, OWNERS, KEYS and the extensionless scripts in ``scripts/``.
TEXT_SUFFIXES = (".go", ".mod", ".sum", ".yaml", ".yml", ".json", ".toml",
                 ".sh", ".bash", ".txt", ".cfg", ".ini", ".tpl", ".html",
                 ".css", "")

#: How much of a file decides whether it is text.  An ELF header carries NUL
#: padding in its first sixteen bytes, so one block is more than enough.
_SNIFF = 8192


def looks_binary(path: Path) -> bool:
    """True for a file a text scan must not read.

    The empty entry in ``TEXT_SUFFIXES`` is there for Makefile and friends, and it
    also matches a compiled binary left in the tree -- which is how this was
    found: an agent's ``go build`` wrote a 95 MB ``./chartmuseum``, the manifest
    did not exclude it, and the token sweeps grepped the executable.  That is not
    a harmless waste of a read.  A Go binary embeds the import paths and type
    names it was linked against, so the State A oracle binary contains
    ``github.com/gin-gonic/gin`` nine times and ``gin.Context`` eight -- measured.
    A submission that leaves any pre-migration or intermediate build behind would
    therefore hand the reviewer findings that say the retired framework is still
    in the tree, cited to offsets in an executable, with the citation lines
    rendering as raw bytes.  The reviewer is told the scan's findings are places
    to look; ``gin_retired`` is required, and a required gate failing is the whole
    submission.  So a finding has to come from something a human could read.
    """
    try:
        with path.open("rb") as fh:
            return b"\0" in fh.read(_SNIFF)
    except OSError:
        return True


def retired_modules() -> list[str]:
    """The retired module paths, in the order the ban list declares them.

    Raises rather than returning nothing.  Ten of `closure`'s checks are
    parametrised over this list, so an empty one does not shorten the scan
    visibly -- it removes the ten checks that ask the scan's actual question and
    leaves 27 that pass, which reads exactly like a clean tree.  The stage above
    already handles a scan that dies (it becomes a note, and the reviewer reads
    the trees itself); it has no way to notice a scan that ran and measured less
    than it was asked to.

    This is reachable by configuration, not just by a corrupt image: the fallback
    below resolves inside the image, but `SRB_RETIRED_MODULES` overrides it, and
    the path that is correct for the behavioural image is not correct for this one.
    """
    try:
        lines = RETIRED_FILE.read_text().splitlines()
    except OSError as exc:
        raise RuntimeError(
            f"the retired-module ban list is unreadable at {RETIRED_FILE}: {exc}. "
            f"Every parametrised check in `closure` is generated from it, so an "
            f"empty list is not a smaller scan, it is a scan that cannot fail. "
            f"If SRB_RETIRED_MODULES is set, it is set to a path that does not "
            f"exist in this image."
        ) from exc
    names = [ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")]
    if not names:
        raise RuntimeError(
            f"the retired-module ban list at {RETIRED_FILE} names no modules; "
            f"this task retires three, and a scan parametrised over none of them "
            f"reports a clean tree whatever the submission did"
        )
    return names


def is_exempt(rel: str) -> bool:
    parts = rel.split("/")
    if any(p in EXEMPT_DIRS for p in parts):
        return True
    return rel.endswith(EXEMPT_SUFFIXES)


def read(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


def source_files(repo: Path):
    """Every text file worth reading, as ``(path, repo-relative string)``."""
    for path in sorted(repo.rglob("*")):
        if not path.is_file():
            continue
        rel = str(path.relative_to(repo))
        if is_exempt(rel):
            continue
        if path.suffix not in TEXT_SUFFIXES:
            continue
        if looks_binary(path):
            continue
        yield path, rel


def go_files(repo: Path):
    """Every ``.go`` file, tests included."""
    for path, rel in source_files(repo):
        if path.suffix == ".go":
            yield path, rel


def go_sources(repo: Path):
    """Every ``.go`` file that is not a test.

    The split matters for almost every check here.  A test file is allowed to know
    it is a test, and a ported test suite that still drives the old framework's
    test helpers is a different finding from a handler that still uses them.
    """
    for path, rel in go_files(repo):
        if not rel.endswith("_test.go"):
            yield path, rel


def go_tests(repo: Path):
    for path, rel in go_files(repo):
        if rel.endswith("_test.go"):
            yield path, rel


_IMPORT_LINE = re.compile(r'^\s*import\s+(?:[\w.]+\s+)?"([^"]+)"')
_IMPORT_OPEN = re.compile(r'^\s*import\s*\($')
_IMPORT_SPEC = re.compile(r'^\s*(?:[\w.]+\s+)?"([^"]+)"')


def imports(path: Path) -> set[str]:
    """The module paths a Go file imports, read from its import block.

    Line-based rather than regex-over-the-file, for the reason in the module
    docstring: the block is what the compiler reads, and a module path in a
    comment or a URL is not an import.  Both forms are handled -- the parenthesised
    block and the single-line ``import "x"`` -- along with named and blank imports
    (``foo "x"``, ``_ "x"``, ``. "x"``).

    Not a Go parser.  ``import`` may only appear at the top of a file, before any
    declaration, so scanning until the first closing paren is exact for
    well-formed source; a file that does not compile may be read wrongly, and
    ``test_every_go_file_has_a_readable_import_block`` says which files those are.
    """
    found: set[str] = set()
    inside = False
    for line in read(path).splitlines():
        if inside:
            if line.strip().startswith(")"):
                inside = False
                continue
            m = _IMPORT_SPEC.match(line)
            if m:
                found.add(m.group(1))
            continue
        if _IMPORT_OPEN.match(line):
            inside = True
            continue
        m = _IMPORT_LINE.match(line)
        if m:
            found.add(m.group(1))
    return found


def importers(repo: Path, module: str, *, tests: bool = True) -> dict[str, list[str]]:
    """Files whose import block names ``module`` or a package inside it.

    Keyed by repo-relative path, valued by the import paths found, so a finding
    names the subpackage rather than only the module.
    """
    walker = go_files if tests else go_sources
    out: dict[str, list[str]] = {}
    for path, rel in walker(repo):
        hit = sorted(p for p in imports(path)
                     if p == module or p.startswith(module + "/"))
        if hit:
            out[rel] = hit
    return out


def gomod(repo: Path) -> str:
    return read(repo / "go.mod")


_REQUIRE_LINE = re.compile(r'^\s*(?:require\s+)?([\w.\-]+\.[\w.\-/]+)\s+v\S+')


def required_modules(repo: Path) -> dict[str, str]:
    """Module path -> version, from go.mod's require blocks.

    ``replace`` and ``exclude`` are deliberately not merged in: a ``replace`` that
    redirects a retired module somewhere else is its own finding, and flattening it
    into this mapping would hide it.  ``replace_directives`` reads those.
    """
    out: dict[str, str] = {}
    section = False
    for line in gomod(repo).splitlines():
        stripped = line.strip()
        if stripped.startswith("require ("):
            section = True
            continue
        if section and stripped.startswith(")"):
            section = False
            continue
        if stripped.startswith(("replace", "exclude", "retract")):
            continue
        if not (section or stripped.startswith("require ")):
            continue
        m = _REQUIRE_LINE.match(stripped)
        if m:
            fields = stripped.split()
            version = next((f for f in fields if f.startswith("v")), "")
            out[m.group(1)] = version
    return out


def replace_directives(repo: Path) -> list[str]:
    """Every ``replace`` line in go.mod, as written."""
    out: list[str] = []
    section = False
    for line in gomod(repo).splitlines():
        stripped = line.strip()
        if stripped.startswith("replace ("):
            section = True
            continue
        if section and stripped.startswith(")"):
            section = False
            continue
        if stripped.startswith("replace "):
            out.append(stripped)
        elif section and "=>" in stripped:
            out.append(stripped)
    return out


def vendored_modules(repo: Path) -> list[str]:
    """Module paths that have a directory under ``vendor/``.

    ``vendor/`` is in EXEMPT_DIRS so no token scan reads it, which is exactly why
    this exists: the question is not what is written inside a vendored copy, it is
    whether one is there.
    """
    vendor = repo / "vendor"
    if not vendor.is_dir():
        return []
    out = []
    for modules_txt in (vendor / "modules.txt",):
        if modules_txt.is_file():
            for line in read(modules_txt).splitlines():
                if line.startswith("# "):
                    out.append(line[2:].split()[0])
    if out:
        return sorted(set(out))
    # No modules.txt: report the deepest directories that look like module roots.
    return sorted({str(p.relative_to(vendor))
                   for p in vendor.rglob("*") if p.is_dir()
                   and any(c.suffix == ".go" for c in p.iterdir() if c.is_file())})


def cite(path: Path, rel: str, token: str, limit: int = 6) -> list[str]:
    """Every line ``token`` appears on, as ``rel:lineno: text`` citations.

    Whole lines, because a reviewer is going to re-open the file at that line and
    a bare line number does not say what to expect there.
    """
    out = []
    for lineno, line in enumerate(read(path).splitlines(), 1):
        if token in line:
            out.append(f"{rel}:{lineno}: {line.strip()[:140]}")
            if len(out) >= limit:
                out.append(f"{rel}: (further hits not listed)")
                break
    return out


def cite_classified(path: Path, rel: str, token: str,
                    limit: int = 6) -> tuple[list[str], int, int]:
    """``cite``, plus a count of how many hits are code and how many are not.

    The token sweep cannot tell a dependency from a sentence about one, and it is
    not supposed to -- but the reviewer is handed the finding through
    ``scan.digest``, which caps the summary and cuts the detail at 240 characters.
    Measured on a real submission, the cut landed one character before the ``//``
    on the first citation and the second file did not appear at all, so a tree
    with no Gin anywhere rendered to a required gate as "'gin.Context' appears in
    2 file(s)".  The counts exist to be said in the first sentence, before any
    truncation can reach them.

    For a Go file "code" means outside ``//`` and ``/* */``; string literals count
    as code, because a router that dispatches on a string is dispatching.  For
    anything else the classification is per-line and cruder -- a ``#`` comment in
    YAML or shell -- which is honest for what it is used for: the sweep's Go hits
    are the ones that can be a live reference.
    """
    cites: list[str] = []
    code = prose = 0
    for lineno, line, in_code in _classified_lines(path, token):
        cites.append(f"{rel}:{lineno}: {'code' if in_code else 'comment'}: "
                     f"{line.strip()[:140]}")
        if in_code:
            code += 1
        else:
            prose += 1
        if len(cites) >= limit:
            cites.append(f"{rel}: (further hits not listed)")
            break
    return cites, code, prose


def _classified_lines(path: Path, token: str):
    """Yield ``(lineno, line, token_is_in_code)`` for lines containing ``token``.

    A single pass, because ``/* */`` spans lines and a per-line test cannot see
    that it is inside one.  Only the *first* occurrence of the token on a line is
    classified: a line holding the token twice, once in code and once in a
    trailing comment, is a line the reviewer has to open anyway.
    """
    go = path.suffix == ".go"
    in_block = False
    for lineno, line in enumerate(read(path).splitlines(), 1):
        at = line.find(token)
        if at < 0:
            if go and not in_block:
                in_block = _opens_block(line)
            elif go and in_block and "*/" in line:
                in_block = False
            continue
        if not go:
            # `#` is the only comment marker among the remaining text suffixes.
            stripped = line.lstrip()
            yield lineno, line, not stripped.startswith("#")
            continue
        if in_block:
            yield lineno, line, False
            if "*/" in line:
                in_block = False
            continue
        yield lineno, line, _in_go_code(line, at)
        in_block = _opens_block(line)


def _opens_block(line: str) -> bool:
    """Does ``line`` leave an unterminated ``/*`` open?"""
    i, open_at = 0, -1
    while True:
        i = line.find("/*", i)
        if i < 0:
            break
        open_at, i = i, i + 2
        end = line.find("*/", i)
        if end < 0:
            return True
        i = end + 2
    return False


def _in_go_code(line: str, at: int) -> bool:
    """Is offset ``at`` outside every comment on this Go line?

    Walks the line rather than searching for ``//``, so a ``//`` inside a string
    literal -- a URL, which this task's own tree has -- does not turn the rest of
    the line into a comment.
    """
    i = 0
    quote = ""
    while i < at:
        c = line[i]
        if quote:
            if c == "\\" and quote != "`":
                i += 2
                continue
            if c == quote:
                quote = ""
        elif c in "\"'`":
            quote = c
        elif line.startswith("//", i):
            return False
        elif line.startswith("/*", i):
            end = line.find("*/", i + 2)
            if end < 0 or end > at:
                return False
            i = end + 2
            continue
        i += 1
    return True


def locate(path: Path, needle: str) -> int | None:
    """The 1-based line ``needle`` first appears on, for a citation."""
    for lineno, line in enumerate(read(path).splitlines(), 1):
        if needle in line:
            return lineno
    return None


def _require_tree(path: Path, role: str, var: str) -> None:
    """Refuse to scan against a tree that is not there.

    Eleven checks in this suite take the ``original`` fixture, across closure,
    entrypoint and provenance, and every one of them is *differential*: it asks
    what State A had that the submission does not.  Against an empty ``original``
    that question inverts into "what does the submission have", and the answers
    still read as findings -- a gin route the submission was supposed to retire is
    reported as retired, and nothing in that verdict says the comparison had one
    side missing.

    ``/opt/original`` is a mount point in this stage's image and is empty by design
    at build time; an empty one at *runtime* means the harness did not mount State
    A.  Docker materialises a missing bind source as an empty directory rather than
    refusing, so a stage pointed at the wrong path inside ``original.tar.gz``
    produces exactly this.  It is an infrastructure fault and never something the
    submission did.

    ``pytrace=False`` deliberately: eleven tests take this fixture, and lang01's
    ``_require_in_state_a`` records what happens otherwise -- a guard that prints a
    traceback each time pushes the real findings past ``scan.digest()``'s render
    limit, which is the harm it exists to prevent.
    """
    if not path.is_dir() or not any(path.iterdir()):
        state = "does not exist" if not path.is_dir() else "is an empty directory"
        pytest.fail(
            f"INFRASTRUCTURE FAULT, not a finding about the submission: the tree "
            f"for {role} ({path}, from ${var}) {state}. Every check that takes it "
            f"is differential and with one side absent its output is meaningless. "
            f"Mount {role} at {path} and re-run; read no finding from this run.",
            pytrace=False)


@pytest.fixture(scope="session")
def repo() -> Path:
    _require_tree(REPO, "the submission", "SRB_REPO")
    return REPO


@pytest.fixture(scope="session")
def original() -> Path:
    _require_tree(ORIGINAL, "State A", "SRB_ORIGINAL")
    return ORIGINAL


@pytest.fixture(scope="session")
def retired() -> list[str]:
    return retired_modules()
