"""Fixtures and helpers for the read-only scan.

Deliberately small.  A scan module gets two trees and a text editor's worth of
facility: open a file, walk a directory, read the import block of a Go file.  It
cannot resolve, build, link or run the submission, and there is nothing here that
would let it -- the stage-1 image has no Go toolchain and no module mirror, so a
module that tried would fail on the missing compiler rather than quietly grading a
build.

The one non-trivial helper is ``imports``, and it exists because the alternative
is a regex over the whole file.  A regex counts the module path in a comment, in a
doc comment and in a URL; the import block is what the compiler reads.  It is not
a Go parser -- it does not resolve build tags, and a file excluded by one still has
its imports counted here -- which is another reason nothing in this stage is
scored.  A finding says "this file names gorilla/mux in its import block"; the
reviewer opens the file and says what that means.

Two helpers are specific to this task and worth naming, because they are where a
scan is most likely to mislead a reviewer.

``path_literals`` collects quoted strings that look like request paths.  A correct
port *must* contain some: the task's contract is written in terms of ``/files``,
``/upload`` and the string-prefix rule, so the literals are the contract rather
than a smell.  What the reviewer is being handed is where they are and how many
are collected in one place, not the fact that they exist.

``prefix_tests`` finds ``strings.HasPrefix``-shaped comparisons against those
literals.  State A's behaviour cannot be reproduced without one, so every honest
submission trips this.  It is reported so the reviewer can go and read the code
that implements the rule -- and decide whether it is a rule.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

REPO = Path(os.environ.get("SRB_REPO", "/opt/workspace"))
ORIGINAL = Path(os.environ.get("SRB_ORIGINAL", "/opt/original"))

_DATA = Path(__file__).resolve().parent.parent / "data"

#: The module this task retires.  Read from the same file the environment image
#: and the behavioural stage read, so there is one list in the tree and a second
#: retired module added to the task is covered without editing a scan.
RETIRED_FILE = Path(os.environ.get("SRB_RETIRED_MODULES",
                                   str(_DATA / "retired-modules.txt")))

#: The whole banned category, matched as module-path prefixes.  fw06 does not
#: retire one router and expect another; it retires third-party routing, so the
#: import graph is checked against 28 module prefixes rather than against one.
BANNED_FILE = Path(os.environ.get("SRB_BANNED_ROUTERS",
                                  str(_DATA / "banned-routers.txt")))

#: Prose and binary assets.  A migration is documented in Markdown and this
#: repository ships a PNG and a JSON config; none of them is a place to look for a
#: live dependency.
EXEMPT_SUFFIXES = (".md", ".rst", ".png", ".jpg", ".jpeg", ".webp", ".svg",
                   ".ico", ".gz", ".tgz", ".zip", ".bin", ".pem", ".key",
                   ".woff", ".woff2", ".ttf", ".map")

#: Directories a scan does not read.  ``vendor`` is exempt from *token* scans but
#: very much not from the vendoring check, which looks for it by name -- see
#: ``vendored_modules``.
EXEMPT_DIRS = (".git", "vendor", "node_modules", "__pycache__", ".cache",
               "testdata", "bin", "dist")

#: Suffixes a text scan will open at all.  The empty string catches Makefile,
#: Dockerfile, LICENSE and the extensionless fixtures.
TEXT_SUFFIXES = (".go", ".mod", ".sum", ".yaml", ".yml", ".json", ".toml",
                 ".sh", ".bash", ".txt", ".cfg", ".ini", ".tpl", ".html",
                 ".css", "")


def _list_file(path: Path) -> list[str]:
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        stripped = line.split("#", 1)[0].strip()
        if stripped:
            out.append(stripped)
    return out


def retired_modules() -> list[str]:
    """The retired module paths, in the order the ban list declares them."""
    return _list_file(RETIRED_FILE)


def banned_routers() -> list[str]:
    """Every third-party router module prefix the task refuses."""
    return _list_file(BANNED_FILE)


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
        if path.suffix in TEXT_SUFFIXES:
            yield path, rel


def go_files(repo: Path):
    """Every ``.go`` file, tests included."""
    for path, rel in source_files(repo):
        if path.suffix == ".go":
            yield path, rel


def go_sources(repo: Path):
    """Every ``.go`` file that is not a test.

    The split matters for almost every check here.  A test file is allowed to know
    it is a test, and a test that still constructs the retired router is a
    different finding from a handler that does.
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
    comment or a URL is not an import.  Both forms are handled -- the
    parenthesised block and the single-line ``import "x"`` -- along with named and
    blank imports (``foo "x"``, ``_ "x"``, ``. "x"``).

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
    modules_txt = vendor / "modules.txt"
    if modules_txt.is_file():
        out = [line[2:].split()[0] for line in read(modules_txt).splitlines()
               if line.startswith("# ")]
        if out:
            return sorted(set(out))
    # No modules.txt: report the directories that look like package roots.
    return sorted({str(p.relative_to(vendor))
                   for p in vendor.rglob("*") if p.is_dir()
                   and any(c.suffix == ".go"
                           for c in p.iterdir() if c.is_file())})


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


def locate(path: Path, needle: str) -> int | None:
    """The 1-based line ``needle`` first appears on, for a citation."""
    for lineno, line in enumerate(read(path).splitlines(), 1):
        if needle in line:
            return lineno
    return None


# --------------------------------------------------------------------------
# task-specific readers
# --------------------------------------------------------------------------

#: A quoted string that looks like a request path: starts with a slash, no
#: whitespace, and not a filesystem path the code would obviously be opening.
_PATH_LITERAL = re.compile(r'"(/[A-Za-z0-9_\-./{}*%+]*)"')

#: Paths that are not request paths on this repository.  ``/opt/app`` is the
#: original test suite's in-memory document root and appears in State A itself.
_NOT_REQUEST_PATHS = ("/opt", "/tmp", "/var", "/usr", "/etc", "/dev", "/proc")


def path_literals(path: Path) -> dict[str, list[int]]:
    """Request-path-looking string literals in a file, with their line numbers.

    Reported, never judged.  ``/files``, ``/upload`` and ``/`` are in this task's
    contract, so a submission with none of them is the surprising one.  What the
    reviewer wants to know is whether a *collection* of them sits in one place --
    see ``dispatch``'s lookup-shape check.
    """
    out: dict[str, list[int]] = {}
    for lineno, line in enumerate(read(path).splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("//"):
            continue
        for m in _PATH_LITERAL.finditer(line):
            lit = m.group(1)
            if lit.startswith(_NOT_REQUEST_PATHS):
                continue
            out.setdefault(lit, []).append(lineno)
    return out


#: A string-prefix test, in the three spellings Go offers.
_PREFIX_CALL = re.compile(
    r'strings\.HasPrefix\s*\(|'
    r'\bstrings\.CutPrefix\s*\(|'
    r'\[\s*:\s*len\s*\(')


def prefix_tests(path: Path) -> list[int]:
    """Lines that perform a string-prefix test.

    Every correct submission has at least one: State A's ``PathPrefix("/files")``
    is a string-prefix match and ServeMux cannot express it, so the rule has to be
    written by hand.  This is evidence about *where* that happened.
    """
    return [lineno for lineno, line in enumerate(read(path).splitlines(), 1)
            if _PREFIX_CALL.search(line) and not line.strip().startswith("//")]


#: ServeMux and default-mux registration, plus the fields that mount a handler.
_REGISTRATION = re.compile(
    r'\.HandleFunc\s*\(|\.Handle\s*\(|'
    r'http\.HandleFunc\s*\(|http\.Handle\s*\(|'
    r'\bNewServeMux\s*\(|'
    r'\bHandler\s*:')

#: The pattern argument of a registration, as written.  Go 1.22 method patterns
#: put the method in the same string ("GET /files/{path...}"), which is why the
#: method is captured out of the literal rather than looked for in a call chain.
_PATTERN_ARG = re.compile(r'\(\s*"((?:[A-Z]+\s+)?[^"]*)"')

#: The methods a Go 1.22 pattern may carry.
_METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS",
            "CONNECT", "TRACE")


def registrations(path: Path) -> list[tuple[int, str, str]]:
    """``(lineno, call, pattern)`` for every routing registration in a file.

    ``pattern`` is the literal as written, empty when the call passes a variable.
    A ``Handler:`` field is reported with an empty pattern too -- it mounts one
    handler for everything, which is the shape the ``dispatch_is_rehosted`` gate
    cares most about.
    """
    out: list[tuple[int, str, str]] = []
    for lineno, line in enumerate(read(path).splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("//"):
            continue
        m = _REGISTRATION.search(line)
        if not m:
            continue
        call = m.group(0).strip().rstrip("(").strip()
        pat = _PATTERN_ARG.search(line[m.end() - 1:])
        out.append((lineno, call, pat.group(1) if pat else ""))
    return out


def pattern_method(pattern: str) -> str:
    """The method a Go 1.22 pattern declares, or "" for a method-less pattern."""
    head = pattern.split(None, 1)
    if len(head) == 2 and head[0] in _METHODS:
        return head[0]
    return ""


@pytest.fixture(scope="session")
def repo() -> Path:
    _require_tree(REPO, "the submission", "SRB_REPO")
    return REPO


@pytest.fixture(scope="session")
def original() -> Path:
    _require_tree(ORIGINAL, "State A", "SRB_ORIGINAL")
    return ORIGINAL


def _require_tree(path: Path, role: str, var: str) -> None:
    """Refuse to scan against a tree that is not there.

    Every comparison in this stage is *differential*: it asks what the submission
    has that State A did not.  Against an empty `original` that question inverts
    into "what does the submission have", and the answers still look like findings
    -- measured, an unmounted `/opt/original` turned 5 findings into 13, of which
    three were phantom reports naming every file in a correctly ported tree and
    two were `AttributeError` on a parse of nothing.  A reviewer reads those as
    evidence, because nothing in them says the comparison had one side missing.

    So the failure has to arrive here, once, naming the mount, rather than as a
    dozen plausible-looking defects downstream.  `/opt/original` is a mount point
    in this stage's image (empty by design); an empty one at *runtime* means the
    harness did not mount State A, which is an infrastructure fault and never
    something the submission did.
    """
    if not path.is_dir() or not any(path.iterdir()):
        state = "does not exist" if not path.is_dir() else "is an empty directory"
        pytest.fail(
            f"INFRASTRUCTURE FAULT, not a finding about the submission: the tree "
            f"for {role} ({path}, from ${var}) {state}. This stage compares the "
            f"submission against State A, and every check it runs is differential; "
            f"with one side absent the comparisons still produce output, and that "
            f"output is meaningless. Mount {role} at {path} and re-run. Do not "
            f"read any finding from this run.")


@pytest.fixture(scope="session")
def retired() -> list[str]:
    return retired_modules()


@pytest.fixture(scope="session")
def banned() -> list[str]:
    return banned_routers()
