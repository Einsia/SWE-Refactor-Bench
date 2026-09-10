"""Fixtures and helpers for the read-only scan.

Deliberately small.  A scan module gets two trees and a text editor's worth of
facility: open a file, walk a directory, blank out the comments, match a pattern.
It cannot install, build, start or import the submission, and there is nothing
here that would let it -- the stage-1 image has no Node, no npm cache and no
browser, so a module that tried would fail on a missing binary rather than
quietly grading a build.

Two decisions in here are worth reading before writing a module against it.

The walk is a *denylist*.  ``iter_files`` opens everything that is plausibly
text, minus dependency trees and build output, and decides what a file is from
its contents.  A retired framework does not stop being the retired framework
because of what the file is called.

Nothing here decides anything.  Every check a scan module writes is recorded with
``required = False`` by ``swerefactor.scan``, and the audit gate is the five
prose questions in evaluation.toml.  A module's job is to put a path and a line
number in front of a reader who can open the file.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

REPO = Path(os.environ.get("SRB_REPO", "/opt/workspace"))
ORIGINAL = Path(os.environ.get("SRB_ORIGINAL", "/opt/original"))

#: Package manifests and lock files.  Always read, whatever their size, because
#: a lock file is the one place a retired dependency can hide in plain sight.
MANIFEST_NAMES = frozenset({
    "package.json", "package-lock.json", "npm-shrinkwrap.json", "yarn.lock",
    "pnpm-lock.yaml", "bun.lockb", "bun.lock",
})

#: Not text, so not source.  Extension-based because the alternative is decoding
#: a 40 MB font to find out.
BINARY_SUFFIXES = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".ico", ".bmp", ".tiff",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".mp3", ".mp4", ".webm", ".ogg", ".wav", ".mov", ".avi",
    ".zip", ".gz", ".tgz", ".bz2", ".xz", ".zst", ".7z", ".rar", ".tar",
    ".pdf", ".so", ".dylib", ".dll", ".exe", ".wasm", ".node", ".class", ".jar",
    ".pyc", ".pyo", ".bin", ".dat", ".db", ".sqlite", ".lockb",
})

#: A file that cannot be read in this budget is not a source file.  Keeps a
#: hostile 500 MB blob from stalling the scan.
MAX_SCAN_BYTES = 4 * 1024 * 1024

#: Directories the walk does not enter.  Every entry is either a dependency tree,
#: VCS internals, or a path task.toml's [[artifacts]] exclude list already drops
#: before collection -- because a directory that survives collection and is not
#: read is a place to keep the retired implementation and compile from it at
#: build time.
SKIP_DIRS = frozenset({
    "node_modules", ".git",
    "dist", "build", "out", ".cache", ".parcel-cache", ".vite", ".next",
    ".nuxt", ".turbo", ".output", "coverage", ".nyc_output", "__pycache__",
})

#: Where the submission's own tests live.  Used only by the fixture-leak scan: a
#: test may name the data it asserts on.  It is deliberately *not* an exemption
#: from the framework scan -- no test needs to carry a single-file component.
TEST_PATH_RE = re.compile(
    r"(^|/)(tests?|__tests__|__mocks__|e2e|cypress|spec|playwright|fixtures?)(/|$)"
    r"|\.(test|spec)\.[a-z]+$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Reading the tree
# ---------------------------------------------------------------------------

def looks_like_text(path: Path) -> bool:
    """True if the first block decodes as UTF-8 and holds no NUL byte.

    Cheap, extension-independent, and wrong only in ways that favour the
    submission: a file this rejects is genuinely unreadable as source.
    """
    try:
        with path.open("rb") as fh:
            head = fh.read(8192)
    except OSError:
        return False
    if b"\x00" in head:
        return False
    try:
        head.decode("utf-8")
    except UnicodeDecodeError:
        # A multi-byte character split across the 8 KiB boundary is fine.
        try:
            head[:-4].decode("utf-8")
        except UnicodeDecodeError:
            return False
    return True


def iter_files(repo: Path):
    """Every file in the tree that is plausibly text, as ``(path, rel)``.

    Denylist by design -- see the module docstring.  Renaming a file cannot hide
    its contents from this walk.
    """
    for path in sorted(repo.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = str(path.relative_to(repo)).replace("\\", "/")
        if any(part in SKIP_DIRS for part in Path(rel).parts):
            continue
        if path.name in MANIFEST_NAMES:
            yield path, rel
            continue
        if path.suffix.lower() in BINARY_SUFFIXES:
            continue
        try:
            if path.stat().st_size > MAX_SCAN_BYTES:
                continue
        except OSError:
            continue
        if looks_like_text(path):
            yield path, rel


def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def load_json(path: Path):
    try:
        return json.loads(read(path))
    except (json.JSONDecodeError, ValueError):
        return None


def manifest(repo: Path) -> dict:
    """``package.json`` as a dict, or ``{}`` if it is missing or malformed."""
    data = load_json(repo / "package.json")
    return data if isinstance(data, dict) else {}


def declared_deps(repo: Path) -> dict[str, str]:
    """Every dependency the manifest declares, from every section, lowercased.

    All six sections plus ``resolutions`` and ``overrides``: a submission that
    moved the retired framework into ``optionalDependencies`` has not retired it,
    and ``npm install`` reads them all.
    """
    data = manifest(repo)
    out: dict[str, str] = {}
    sections = ("dependencies", "devDependencies", "peerDependencies",
                "optionalDependencies", "bundledDependencies",
                "bundleDependencies", "resolutions", "overrides")
    for section in sections:
        block = data.get(section)
        if isinstance(block, dict):
            for name, spec in block.items():
                out[str(name).lower()] = f"{section}: {name}@{spec}"
    return out


def lockfiles(repo: Path):
    """Every lock file in the tree, as ``(path, rel)``."""
    for path, rel in iter_files(repo):
        if path.name in MANIFEST_NAMES and path.name != "package.json":
            yield path, rel


def resolves_in_lockfile(text: str, package: str) -> str:
    """The line a lock file resolves ``package`` on, or ``""``.

    Matched at the package-name position in each of the four lock-file dialects,
    so ``vue`` does not hit inside ``vuex-persist`` or a URL fragment.
    """
    quoted = re.escape(package)
    patterns = (
        rf'"node_modules/{quoted}"',        # npm v7+ packages map
        rf'(?m)^\s*"{quoted}"\s*:',         # npm v6 dependencies map
        rf'(?m)^"{quoted}@',                # yarn v1
        rf'(?m)^\s*/{quoted}/\d',           # pnpm
        rf'(?m)^\s*{quoted}@[^:\n]*:\s*$',  # yarn v1, unquoted
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(0).strip()
    return ""


# ---------------------------------------------------------------------------
# Deciding what a file holds
# ---------------------------------------------------------------------------

#: Files that are prose rather than source.  Not exempt from the walk -- a
#: single-file component pasted into a fenced block is still one, and
#: :func:`sfc_shape` reads the raw text -- but code *quoted* in them is quoted.
PROSE_SUFFIXES = frozenset({".md", ".markdown", ".mdx", ".rst", ".txt", ".adoc"})


def strip_comments(text: str, rel: str = "") -> str:
    """Blank out comments so a pattern quoted in prose is not a hit.

    Deliberately crude: it over-blanks rather than under-blanks, because the cost
    of over-blanking is a missed lead and the cost of under-blanking is a reader
    sent to a line that says "we no longer do this".

    In a prose file, backticked text is blanked too.  That is the same rule one
    step further out: a migration note reading "components that used ``v-if`` are
    now conditional JSX" is a sentence about the retired framework.  Backticks are
    left alone in source, where they delimit template literals and a recorded
    value could genuinely be hiding in one.
    """
    if Path(rel).suffix.lower() in PROSE_SUFFIXES:
        text = re.sub(r"(?ms)^```.*?^```", " ", text)
        text = re.sub(r"(?ms)^(?: {4}|\t).*?$", " ", text)
        text = re.sub(r"`[^`\n]*`", " ", text)
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    text = re.sub(r"(?m)^\s*//.*$", " ", text)
    text = re.sub(r"(?m)^\s*\*.*$", " ", text)
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.DOTALL)
    return text


#: A single-file component has a shape, and the shape survives renaming.  Matched
#: on the RAW text: a template block is markup, not a comment, and blanking
#: comments first would let an ``<!-- -->`` wrapper hide the whole thing.
_SFC_TEMPLATE = re.compile(r"<template\s*>[\s\S]*?</template\s*>", re.IGNORECASE)
_SFC_DIRECTIVE = re.compile(
    r"\bv-(if|else-if|else|for|model|show|html|text|bind|on|once|cloak|pre)\b[=:\s>]"
    r"|\s[:@][A-Za-z][-A-Za-z0-9]*\s*="
    r"|\{\{[^}]*\}\}"
    r"|</?(router-link|router-view|keep-alive|transition(-group)?)\b",
)
_SFC_OPTIONS = re.compile(
    r"export\s+default\s*\{[\s\S]{0,4000}?\b(data|methods|computed|mounted|created|"
    r"props|watch|components|beforeRouteEnter|beforeRouteUpdate)\s*[:(]",
)


def sfc_shape(raw: str) -> str:
    """Why this text is a single-file component, or ``""`` if it is not.

    Extension-independent on purpose.  ``Home.vue`` renamed to ``Home.tpl``,
    ``.html``, ``.txt`` or nothing at all is still the retired implementation, and
    the only way to stop matching is to rewrite the file in the target stack --
    which is the task.
    """
    tpl = _SFC_TEMPLATE.search(raw)
    if not tpl:
        return ""
    inner = tpl.group(0)
    directive = _SFC_DIRECTIVE.search(inner)
    if directive:
        return (f"holds a template block using the retired framework's template "
                f"syntax ({directive.group(0).strip()!r})")
    if _SFC_OPTIONS.search(raw):
        return "holds a template block beside an options object"
    return ""


def find_pattern(body: str, pattern: str) -> str:
    """The matched snippet, trimmed for a message, or ``""``.

    ``body`` is expected to have been through :func:`strip_comments`, which is
    what keeps a pattern mentioned in prose from counting as an implementation.
    """
    match = re.search(pattern, body)
    return match.group(0).strip()[:60] if match else ""


def locate(path: Path, needle: str) -> int | None:
    """The 1-based line ``needle`` first appears on, for a citation."""
    for lineno, line in enumerate(read(path).splitlines(), 1):
        if needle in line:
            return lineno
    return None


def cite(path: Path, rel: str, pattern: str, limit: int = 6) -> list[str]:
    """``rel:lineno: text`` for each line matching ``pattern``, capped.

    The citation format the reviewer is asked to re-check, produced by the scan so
    a finding arrives with somewhere to look rather than as a file name.
    """
    out: list[str] = []
    try:
        compiled = re.compile(pattern)
    except re.error:
        return out
    for lineno, line in enumerate(read(path).splitlines(), 1):
        if compiled.search(line):
            out.append(f"{rel}:{lineno}: {line.strip()[:160]}")
            if len(out) >= limit:
                break
    return out


def fixture_scan_exempt(rel: str) -> str:
    """Why this file may name recorded data, or ``""`` if it may not."""
    if TEST_PATH_RE.search(rel):
        return "the submission's own tests may name the data they assert on"
    return ""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _require_tree(path: Path, role: str, var: str) -> None:
    """Refuse to scan against a tree that is not there.

    The retirement checks are *differential*: they ask what State A had that the
    submission no longer has.  Against an empty ``original`` that question inverts
    into "what does the submission have", and the answers still read as findings --
    a Vue component the submission was supposed to retire is reported as retired,
    and a reviewer has no way to tell that verdict from a real one, because nothing
    in it says the comparison had one side missing.

    ``/opt/original`` is a mount point in this stage's image and is empty by design
    at build time; an empty one at *runtime* means the harness did not mount State
    A.  Docker materialises a missing bind source as an empty directory rather than
    refusing, so a stage pointed at the wrong path inside ``original.tar.gz``
    produces exactly this.  It is an infrastructure fault and never something the
    submission did.

    ``pytrace=False`` deliberately: this fires once per test that takes the
    fixture, and lang01's ``_require_in_state_a`` records the reason -- a guard that
    prints a traceback each time pushes the real findings past ``scan.digest()``'s
    render limit.
    """
    if not path.is_dir() or not any(path.iterdir()):
        state = "does not exist" if not path.is_dir() else "is an empty directory"
        pytest.fail(
            f"INFRASTRUCTURE FAULT, not a finding about the submission: the tree "
            f"for {role} ({path}, from ${var}) {state}. Every check here is "
            f"differential and with one side absent its output is meaningless. "
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
