"""Fixtures and helpers for the read-only scan.

Deliberately small.  A scan module gets two trees and a text editor's worth of
facility: open a file, walk a directory, parse Python into an AST.  It cannot
install, build, start or import the submission, and there is nothing here that
would let it -- the stage-1 image has no wheelhouse and no submission runtime, so
a module that tried would fail on the missing interpreter rather than quietly
grading a build.

The exemptions are the ones instruction.md publishes to the agent, and they are
here rather than in each module because a scan that searched
``httpbin/templates/`` for the word "flask" would flag the shipped HTML of a
correct migration.
"""

from __future__ import annotations

import ast
import os
import warnings
from pathlib import Path

import pytest

REPO = Path(os.environ.get("SRB_REPO", "/opt/workspace"))
ORIGINAL = Path(os.environ.get("SRB_ORIGINAL", "/opt/original"))

#: Prose and binary assets. A migration is documented in Markdown and ships an
#: icon; neither is a place to look for a live dependency.
EXEMPT_SUFFIXES = (".md", ".rst", ".txt.orig", ".ico", ".png", ".jpeg",
                   ".jpg", ".webp", ".svg", ".woff", ".woff2", ".ttf",
                   ".map", ".gz", ".whl")
EXEMPT_DIRS = ("httpbin/templates", "httpbin/static", ".git", "__pycache__",
               ".pytest_cache", "node_modules", ".tox")

#: Suffixes a text scan will open at all.
TEXT_SUFFIXES = (".py", ".toml", ".cfg", ".ini", ".in", ".txt", ".yml",
                 ".yaml", ".bash", ".sh", ".json", "")


def is_generated(rel: str) -> bool:
    """Is this path machine-written rather than submitted?

    One predicate, callable, rather than a literal tuple of directory names.  Two
    reasons it has to be a function: ``*.egg-info`` varies with the distribution
    and cannot be spelled as a literal, and the directory check in the closure
    module does its own walk -- it needs directory names, which ``source_files``
    never yields -- so a rule the walks inherit rather than call is a rule the two
    walks can disagree about.

    What that disagreement costs falls entirely on submissions that followed the
    brief.  ``pip install -e .``, which instruction.md §7 instructs, writes
    ``httpbin.egg-info/SOURCES.txt``, and a line in it naming a template the tree
    does ship reads as a vendored-source finding.  The same install writes
    ``build/lib/httpbin/templates/flasgger``, which is setuptools' copy of a path
    the finding list already holds -- one asset directory reported twice.

    Both are gitignored, and State A has neither directory, so State A scores zero
    such findings while a submission that did as it was told scores several.  That
    is the wrong direction for a scan to be wrong in.
    """
    first = rel.split("/", 1)[0]
    return first in ("build", "dist") or first.endswith(".egg-info")


def is_exempt(rel: str) -> bool:
    if is_generated(rel):
        return True
    if any(rel == d or rel.startswith(d + "/") for d in EXEMPT_DIRS):
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


def python_files(repo: Path):
    for path, rel in source_files(repo):
        if path.suffix == ".py":
            yield path, rel


def parse(path: Path) -> ast.AST | None:
    """The file's AST, or ``None`` if it does not parse.

    Warnings are suppressed rather than shown.  Compiling somebody else's source
    raises ``SyntaxWarning`` for things like ``"\\d"`` in a plain string, and a
    scan that reported those would be reporting on the *submission's* lint state
    in the middle of a report about its migration.  A file that does not parse at
    all is not silently skipped: ``test_every_python_file_parses`` says so.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            return ast.parse(read(path))
        except (SyntaxError, ValueError):
            return None


def imported_names(path: Path) -> set[str]:
    """Top-level module names a Python file imports, via AST rather than regex.

    A regex counts the word in a comment and misses ``from . import x as
    flask``.  The AST counts what the interpreter would.
    """
    tree = parse(path)
    if tree is None:
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                names.add(node.module.split(".")[0])
    return names


def locate(path: Path, needle: str) -> int | None:
    """The 1-based line ``needle`` first appears on, for a citation."""
    for lineno, line in enumerate(read(path).splitlines(), 1):
        if needle in line:
            return lineno
    return None


def cite(path: Path, needle: str, rel: str, limit: int = 3) -> str:
    """``rel:l1,l2,l3`` over every occurrence, not just the first.

    ``locate`` returns the first line, and a finding built on it cites whichever
    instance happens to sit highest in the file -- which is not the strongest one.
    A worked example, on a submission that still serves ``/flasgger_static/``:
    the marker ``flasgger`` cited as ``httpbin/core.py:543`` is a compatibility
    branch inside ``url_for`` that maps an old endpoint name.  A reviewer who
    opens 543, as the message tells them to, finds a naming shim and reasonably
    moves on.  The route registration is at 1676 and the handler at 1561, and it
    is those that show the tree still *serves* the path -- which §4 of the brief
    forbids by name.  One citation, pointing away from the finding.

    A cap is needed -- the reviewer sees a truncated summary (~300 chars) -- and
    taking the *first* ``limit`` hits rebuilds the same bug one level down.  On
    the same tree the first three hits are 543, 544 and 1411, so the handler and
    the route still fall off the end, and 543 and 544 are one statement: a
    two-line shim crowding out the whole finding.

    So collapse runs of adjacent lines to their first line -- consecutive hits are
    one site, not several -- and when sites still exceed the cap keep both ends
    and sample the middle, because file order is not evidence order and the
    strongest instance is as likely to be last as first.  The count stays visible
    so the omission is never silent.
    """
    hits = [n for n, line in enumerate(read(path).splitlines(), 1)
            if needle in line]
    if not hits:
        return rel

    sites = [n for i, n in enumerate(hits) if i == 0 or n != hits[i - 1] + 1]

    if len(sites) <= limit:
        shown, dropped = sites, 0
    else:
        # First, last, and an even spread between them.
        middle = sites[1:-1]
        step = max(1, round(len(middle) / max(1, limit - 2)))
        picked = middle[::step][:limit - 2]
        shown = [sites[0]] + picked + [sites[-1]]
        dropped = len(sites) - len(shown)

    extra = (f" (+{dropped} more site{'s' if dropped != 1 else ''})"
             if dropped else "")
    return f"{rel}:{','.join(str(n) for n in shown)}{extra}"


@pytest.fixture(scope="session")
def repo() -> Path:
    return REPO


@pytest.fixture(scope="session")
def original() -> Path:
    return ORIGINAL
