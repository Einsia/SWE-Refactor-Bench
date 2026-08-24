"""Helpers for the read-only scan.

Deliberately small.  A scan module gets two trees and a text editor's worth of
facility: open a file, walk a directory, split a line, parse a manifest.  It
cannot install, build, start or import the submission, and there is nothing here
that would let it -- the stage-1 image has no node and no npm, so a module that
tried would fail on the missing interpreter rather than quietly grade a build.

There is no AST here, and that is the honest position rather than a shortcut.
fw01 can parse Python with the standard library; there is no JavaScript parser in
a python:slim image and vendoring one into a grading image to support advisory
findings would be a large dependency bought for nothing.  So this module reads
JavaScript as text, and every helper below is named for what it actually does:
``specifier_lines`` finds lines that look like a module specifier, not imports.
The distinction matters because it is exactly what the reviewer is told in the
prompt -- a finding here is a line number, and the reviewer opens the line.

What is *not* approximate is anything in a manifest or a lockfile: those are JSON,
and JSON is parsed.  A dependency that is declared or locked is a fact.

The exemptions are the ones instruction.md publishes to the agent, and they live
here rather than in each module because a scan that searched ``public/`` for the
word "express" would flag the shipped HTML of a correct migration.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

REPO = Path(os.environ.get("SRB_REPO", "/opt/workspace"))
ORIGINAL = Path(os.environ.get("SRB_ORIGINAL", "/opt/original"))

#: The retired list, shipped in this stage's data/ as the byte-identical copy of
#: the one the agent was given.  Read from a file rather than duplicated into a
#: constant so the two cannot drift.
RETIRED_LIST = Path(os.environ.get(
    "SRB_RETIRED_PACKAGES",
    str(Path(__file__).resolve().parent.parent / "data" / "retired-packages.txt"),
))

#: Prose and binary assets.  A migration is documented in Markdown and ships an
#: icon; neither is a place to look for a live dependency.
EXEMPT_SUFFIXES = (".md", ".markdown", ".rst", ".ico", ".png", ".jpeg", ".jpg",
                   ".gif", ".webp", ".svg", ".woff", ".woff2", ".ttf", ".eot",
                   ".map", ".gz", ".tgz", ".zip", ".lock", ".orig", ".rej")

#: Directories never scanned.  ``public`` and ``altpublic`` are the static roots
#: the service serves: their HTML is content, and State A's own index.html says
#: "json-server" and links to Express documentation.
EXEMPT_DIRS = ("public", "altpublic", ".git", "node_modules", "coverage",
               ".nyc_output", "__pycache__", ".pytest_cache", ".husky/_",
               ".cache")

#: Suffixes a text scan will open at all.
TEXT_SUFFIXES = (".js", ".mjs", ".cjs", ".jsx", ".ts", ".mts", ".cts", ".json",
                 ".yml", ".yaml", ".sh", ".bash", ".babelrc", ".eslintrc", "")

#: The JavaScript-family suffixes, for the checks that are about code.
CODE_SUFFIXES = (".js", ".mjs", ".cjs", ".jsx", ".ts", ".mts", ".cts")

#: Where the project keeps its own tests and their data.  Exempt from the
#: canned-answer question -- fixture data is data -- and not exempt from anything
#: else: a ported test suite that still imports a retired package is a finding
#: about the ported test suite.
TEST_DIRS = ("__tests__", "__fixtures__", "test", "tests")

#: Files whose *content* is data the service is supposed to read.
DATA_FILES = ("db.seed.json", "db.json", "routes.json", "package-lock.json",
              "npm-shrinkwrap.json")


def retired_names() -> list[str]:
    """The retired distributions, in the order the published list gives them."""
    out: list[str] = []
    try:
        text = RETIRED_LIST.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        name = line.split("#", 1)[0].strip()
        if name:
            out.append(name)
    return out


def is_exempt(rel: str) -> bool:
    if any(rel == d or rel.startswith(d + "/") for d in EXEMPT_DIRS):
        return True
    return rel.endswith(EXEMPT_SUFFIXES)


def in_tests(rel: str) -> bool:
    parts = rel.split("/")
    return any(p in TEST_DIRS for p in parts)


def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def load_json(path: Path):
    """A parsed JSON file, or ``None``.  Used for manifests, never for source."""
    try:
        return json.loads(read(path))
    except (ValueError, TypeError):
        return None


def source_files(repo: Path):
    """Every text file worth reading, as ``(path, repo-relative string)``."""
    for path in sorted(repo.rglob("*")):
        if not path.is_file():
            continue
        rel = str(path.relative_to(repo))
        if is_exempt(rel):
            continue
        if path.suffix in TEXT_SUFFIXES or path.name.startswith("."):
            yield path, rel


def code_files(repo: Path):
    """The JavaScript-family files, which is what the code checks look at."""
    for path, rel in source_files(repo):
        if path.suffix in CODE_SUFFIXES:
            yield path, rel


def lines(path: Path):
    """``(lineno, text)`` for each line, 1-based, for citations."""
    return enumerate(read(path).splitlines(), 1)


# --------------------------------------------------------------------------- #
# Reading JavaScript as text
# --------------------------------------------------------------------------- #

#: Anything that looks like a module specifier: require('x'), import ... from
#: 'x', import('x'), export ... from 'x'.  A comment matches too, which is the
#: point: the finding is a line, and the reviewer reads it.
_SPECIFIER = re.compile(
    r"""(?:require\s*\(\s*|
         (?:^|[\s;{(])import\s*\(\s*|
         \bfrom\s+|
         ^\s*import\s+)
        (?P<q>['"])(?P<spec>[^'"]+)(?P=q)""",
    re.VERBOSE | re.MULTILINE,
)

#: A line comment or the opening of a block comment.  Crude, and only ever used
#: to *annotate* a finding as "this line looks like a comment", never to drop it.
#:
#: ``#`` is deliberately absent: in JavaScript ``#count = 0`` is a private class
#: field, which is code.
_COMMENT = re.compile(r"^\s*(//|/\*|\*)")

#: The other comment marker in this tree.  Shell, YAML, Dockerfile, .npmrc.
#:
#: ``#!`` is excluded: a shebang is an instruction to the kernel about which
#: interpreter runs the file, so ``#!/usr/bin/env node`` is live and a flag saying
#: "looks like a comment" about it would be wrong in the same direction this whole
#: fix is about.
_HASH_COMMENT = re.compile(r"^\s*#(?!!)")

#: Files whose comment marker is ``#``.  Matched by name because Python gives a
#: dotfile no suffix -- ``Path(".npmrc").suffix`` is ``""`` -- and because the
#: stage's Dockerfiles are ``Dockerfile.audit`` and the like.
_HASH_SUFFIXES = (".sh", ".bash", ".yml", ".yaml", ".env", ".toml", ".ini",
                  ".cfg", ".conf", ".gitignore", ".dockerignore", ".npmignore")
_HASH_NAMES = (".npmrc", ".nvmrc", ".yarnrc", "Procfile", "Makefile")


def specifier_lines(path: Path):
    """``(lineno, specifier, line)`` for every line that looks like an import.

    Text, not an import graph.  A match inside a comment or a string is included;
    ``looks_commented`` says which ones looked like comments so a module can put
    that in its own summary instead of pretending to know.
    """
    for lineno, line in lines(path):
        for match in _SPECIFIER.finditer(line):
            yield lineno, match.group("spec"), line


def looks_commented(line: str, path: Path | None = None) -> bool:
    """Does this line look like a comment, in the language of the file it came from?

    ``path`` is optional and defaults to JavaScript's markers, which is what every
    caller that walks ``code_files`` wants.  Pass it when the walk is
    ``source_files``, whose files include ``serve.sh``, the CI workflow, the
    Dockerfile and ``.npmrc`` -- there, a ``#`` line is a comment, and a flag that
    reported ``false`` for it would be a worse answer than no flag at all: the
    caveat that fix 15 added to the headline exists to stop a reviewer reading a
    comment as live code, and an authoritative ``false`` does the opposite.

    ``#`` is not in the JavaScript branch on purpose.  ``#count = 0`` is a private
    class field, and ``#!/usr/bin/env node`` is a shebang -- the first is code and
    the second is not a comment in any language, so a bare ``#`` rule would
    mislabel real code in exactly the files most checks look at.
    """
    if path is not None:
        name = path.name
        if name in _HASH_NAMES or name.startswith("Dockerfile") \
                or name.endswith(_HASH_SUFFIXES):
            return bool(_HASH_COMMENT.match(line))
    return bool(_COMMENT.match(line))


def comment_caveat(offenders: list[dict]) -> str:
    """How many of these matches looked like comments, for the headline.

    The module docstring above says ``looks_commented`` exists so a check "can put
    that in its own summary instead of pretending to know".  Five checks collected
    the flag and none said it: it went into the JSON body, and a scan finding reaches
    the reviewer as the *first line* of its message, capped at 300 characters.  So on
    fw02's real submission two findings -- ``res.sendStatus(`` and ``express.static``,
    every match a comment in a file documenting the Express behaviour the port
    replicates -- arrived as a bare assertion that Express's API surface "appears
    here", with the one fact that distinguishes a comment from a live call sitting
    below the cut.

    Returned as a clause for the same line rather than a second sentence, because a
    second sentence is exactly what the 300-character cut removes.
    """
    flags = [bool(o.get("looks_commented")) for o in offenders
             if isinstance(o, dict) and "looks_commented" in o]
    if not flags:
        return ""
    commented, total = sum(flags), len(flags)
    if not commented:
        return ""
    if commented == total:
        return (f" -- and all {total} look like comments, which this check cannot "
                "tell from code, so read the lines")
    return f" -- {commented} of {total} look like comments"


def package_of(spec: str) -> str:
    """The distribution a specifier names: ``@scope/name`` or ``name``.

    ``fastify/lib/x`` is fastify; ``./local`` and ``node:fs`` are neither.
    """
    spec = spec.strip()
    if not spec or spec.startswith((".", "/", "node:")):
        return ""
    parts = spec.split("/")
    if spec.startswith("@"):
        return "/".join(parts[:2]) if len(parts) >= 2 else spec
    return parts[0]


def declared_dependencies(repo: Path) -> dict[str, str]:
    """Every name in every dependency table of package.json."""
    manifest = load_json(repo / "package.json") or {}
    out: dict[str, str] = {}
    for table in ("dependencies", "devDependencies", "optionalDependencies",
                  "peerDependencies", "bundleDependencies",
                  "bundledDependencies"):
        block = manifest.get(table)
        if isinstance(block, dict):
            for name, spec in block.items():
                out[str(name)] = str(spec)
        elif isinstance(block, list):          # bundleDependencies may be a list
            for name in block:
                out.setdefault(str(name), "")
    return out


def runtime_dependencies(repo: Path) -> dict[str, str]:
    """`dependencies` only -- what a published install would pull.

    Separate from :func:`declared_dependencies` because the two answer different
    questions.  "Is a retired package declared anywhere" wants every table; "did
    a capability leave with its dependency" wants runtime only, since a migration
    that drops the babel build legitimately drops the whole babel toolchain.
    """
    manifest = load_json(repo / "package.json") or {}
    block = manifest.get("dependencies")
    if not isinstance(block, dict):
        return {}
    return {str(name): str(spec) for name, spec in block.items()}


def locked_names(repo: Path) -> set[str]:
    """Every distribution a shipped lockfile resolves."""
    names: set[str] = set()
    for candidate in ("package-lock.json", "npm-shrinkwrap.json"):
        lock = load_json(repo / candidate)
        if not isinstance(lock, dict):
            continue
        for key in (lock.get("packages") or {}):
            if key:
                names.add(str(key).split("node_modules/")[-1])
        for name in (lock.get("dependencies") or {}):
            names.add(str(name))
    return names


def find_line(path: Path, needle: str) -> int | None:
    """The 1-based line ``needle`` first appears on, for a citation."""
    for lineno, line in lines(path):
        if needle in line:
            return lineno
    return None


def marker_citation(path: Path, rel: str, needle: str) -> dict | None:
    """The first line holding ``needle``, cited with its text and comment flag.

    ``find_line`` returns a number, and three checks cited nothing else -- so a
    finding read ``'path-to-regexp' appears here: [{"path": "src/server/rewriter.js",
    "line": 2}]``, and a reviewer deciding a required gate had no way to see that
    line 2 is a comment header saying which behaviour the port reimplements.

    Measured on fw02's round-3 submission: that submission declares no
    ``path-to-regexp``, has none in its lockfile, and reimplemented the rewriter --
    documenting the compatibility in the comment the check then reported as evidence
    of the retired router surviving.

    ``find_line`` keeps its signature, since one caller wants only the number and
    asserts the line *is* present rather than absent.
    """
    for lineno, line in lines(path):
        if needle in line:
            return {"path": rel, "line": lineno, "quote": line.strip()[:160],
                    "looks_commented": looks_commented(line, path)}
    return None


def cite(rel: str, lineno: int | None, text: str) -> dict:
    """One evidence entry in the shape the runner re-checks."""
    entry: dict = {"path": rel, "quote": text.strip()[:200]}
    if lineno:
        entry["line"] = lineno
    return entry


def pytest_collection_modifyitems(items):
    """A scan check that did not apply is not a miss.

    ``swerefactor.pytest_module`` promotes an unlicensed skip to ``fail``, and that
    policy is right where it was written: stage 2 runs against a fixed offline
    environment it built itself, so nothing there is skipped unless something is
    broken.  This suite is the other case.  Every skip in it is conditional on the
    *submission's* shape -- ``Dockerfile is not in the tree``, ``package.json
    declares no build script``, ``State A has no routes.json to compare against`` --
    and json-server has no Dockerfile to find a retired name in.

    Left unlicensed, ten of these reached the reviewer as flagged findings whose
    entire text was the word "skipped", competing for the digest's 60 lines with
    the findings that had something to say.  ``scan.digest`` already knows the right
    shape and counts them separately as "N did not apply" -- a branch that could
    never run while the promotion turned every skip into a failure first.

    The license is here, once, rather than as a marker on eighteen call sites: it
    is a property of the suite, not a judgement about individual checks, and a
    nineteenth skip added later should not have to remember to opt in.  It is scoped
    by the ``scan`` marker and by this plugin being loaded only by
    ``lib/run-scan.sh``; stage 2 loads ``srbfixtures`` and never sees this hook.
    """
    for item in items:
        if item.get_closest_marker("scan") is not None:
            item.add_marker("srb_skip_ok")


@pytest.fixture(scope="session")
def repo() -> Path:
    return REPO


@pytest.fixture(scope="session")
def original() -> Path:
    return ORIGINAL


@pytest.fixture(scope="session")
def retired() -> list[str]:
    return retired_names()


@pytest.fixture(scope="session")
def manifest(repo: Path):
    return load_json(repo / "package.json")
