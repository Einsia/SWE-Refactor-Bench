"""Reading tools for the scan, and the two trees it reads.

Deliberately narrow.  A scan module gets two directories and a text editor's worth
of facility: walk a tree, open a file, blank out its comments, pull the module
specifiers out of it, digest it.  It cannot install, build, start or import the
submission, and there is nothing here that would let it -- the stage-1 image has
no Node and no dependency tree, so a module that tried would fail on a missing
interpreter rather than quietly grading a build.

Regex over JavaScript is a blunt instrument, and this module is built around that
fact rather than in spite of it.  Comments and string literals are blanked before
matching, so a note in a comment saying "this used to call readFileSync" is not a
hit.  What survives is still only a string match, which is why nothing here is
scored: `swerefactor.scan` records every check advisory, and the seven gates are
answered by a model with both trees open.  These functions decide *where to look*.

Two things to keep in mind when adding a check.

A finding's last line becomes its one-line summary in the reviewer's prompt:
`swerefactor.pytest_module._summary` walks the failure output backwards for the
first line starting `E ` and hands that to `swerefactor.scan.digest`.  So put the
citation there -- `path:line, path:line` -- and the explanation above it, which is
the opposite of how an assertion message is usually written.

And report failures through `fail_if`, never with a bare `assert`.  The two are
not equivalent here, for a reason that is entirely about the paragraph above: see
`fail_if`.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

#: Files that identify a Stylus checkout, used to resolve the mount.  All three
#: are State A content the port keeps: `package.json` because the package stays a
#: package, `test/` because the task freezes it, `bin/` because the CLI has to
#: keep working.  A submission cannot move the root out from under the scan by
#: deleting them.
ROOT_MARKERS = ("package.json", "test", "bin")


def _resolve(path: Path) -> Path:
    """`path`, or its single child, whichever is the repository root.

    `original.tar.gz` for this task unpacks with a `repo/` prefix, so whether
    `/opt/original` *is* the tree or *contains* it depends on how the operator
    unpacked it.  Getting that wrong is not a visible error: a scan pointed one
    level off walks a directory holding one entry, finds no `src/`, derives an
    empty manifest and reports a clean tree.  Resolving it here costs a stat and
    removes the whole failure mode.

    One level only, and only when the level below looks like the repository.  A
    tree that matches nothing is returned unchanged, so the check that says "this
    mount is wrong" is the one that reports it.
    """
    if not path.is_dir():
        return path
    if any((path / m).exists() for m in ROOT_MARKERS):
        return path
    children = [c for c in path.iterdir() if c.is_dir()]
    if len(children) == 1 and any((children[0] / m).exists() for m in ROOT_MARKERS):
        return children[0]
    return path


#: The submission, exactly as delivered.  Read only; nothing here writes.
REPO = _resolve(Path(os.environ.get("SRB_REPO", "/opt/workspace")))

#: State A, the tree the agent started from.
ORIGINAL = _resolve(Path(os.environ.get("SRB_ORIGINAL", "/opt/original")))

# --- the shape instruction.md asks for ---------------------------------------
# Fixed paths, not discovered ones.  instruction.md names all four, and stage 2
# loads `src/core/index.js` into the realm and executes `bin/stylus` as a program
# -- so a submission that put the core somewhere else does not have a naming
# disagreement with this file, it has a submission stage 2 cannot run at all.
# What the review is asked to judge is what the code at these paths *does*.

CORE_ROOT = REPO / "src" / "core"
NODE_ROOT = REPO / "src" / "node"
CLI = REPO / "bin" / "stylus"
LEGACY_LIB = REPO / "lib"

#: Node built-ins, with or without the `node:` prefix.  The realm defines none of
#: them, so a core reaching for one cannot link there -- but it can still be
#: reached through the adapter, and this is where that shows up as text.
NODE_BUILTINS = frozenset(
    """
    assert async_hooks buffer child_process cluster console constants crypto dgram
    diagnostics_channel dns domain events fs http http2 https inspector module net
    os path perf_hooks process punycode querystring readline repl stream
    string_decoder sys timers tls trace_events tty url util v8 vm wasi
    worker_threads zlib
    """.split()
)

#: Extensions worth opening at all.
JS_SUFFIXES = frozenset({".js", ".mjs", ".cjs"})

#: Directories no scan should walk into: not part of the submission's own work.
#:
#: One upstream path contradicts that description and the set keeps matching it
#: anyway: `test/cases/import.lookup/node_modules/` holds three `.styl` files and a
#: `package.json` that are corpus data, the subject of the graded
#: `import.lookup.styl` case rather than anything installed.
#:
#: It is skipped for a scan cost rather than a coupling: this name is what keeps
#: `js_files` out of the 89-package install `npm ci` writes at the repository root,
#: and the fixture holds nothing a walk here would open regardless.
#:
#: Deliberately not symmetric with `task.toml`'s `[[artifacts]].exclude`, which must
#: not name `node_modules`: a bare name there matches at any depth, and Harbor would
#: delete that fixture from every submission.  `manifest()` is scoped to `test/`,
#: where the root install does not reach, so the fixture can be delivered intact and
#: `declared_inputs_unchanged` -- required -- still sees no difference.
#:
#: So what this name hides is one fixture directory in the digest, in both trees
#: equally.  A submission that deleted it is not caught here -- but the case that
#: imports through it is graded at stage 2, and an `@import` that no longer resolves
#: is the same finding arriving as a wrong answer instead of a missing file.
SKIP_DIRS = frozenset({"node_modules", ".git", ".npm", ".cache", "coverage",
                       ".nyc_output", "__pycache__"})


@dataclass(frozen=True)
class Hit:
    """One observation, with somewhere to look."""

    path: Path
    line: int
    text: str
    detail: str

    @property
    def where(self) -> str:
        try:
            return str(self.path.relative_to(REPO))
        except ValueError:
            return str(self.path)

    def __str__(self) -> str:
        return f"{self.where}:{self.line}: {self.detail} -- {self.text.strip()[:120]}"

    @property
    def cite(self) -> str:
        """`path:line`, for the closing summary line."""
        return f"{self.where}:{self.line}"


def cite_line(hits: list[Hit], *, limit: int = 6) -> str:
    """The closing line of a finding: what, and where to look.

    Kept under the 300 characters `scan.digest` renders, and self-contained,
    because it is read on its own in the reviewer's prompt with none of the
    explanation above it.
    """
    shown = [h.cite for h in hits[:limit]]
    more = len(hits) - len(shown)
    tail = f" (+{more} more)" if more > 0 else ""
    return f"{len(hits)} place(s): " + ", ".join(shown) + tail


# --- reading ------------------------------------------------------------------

_LINE_COMMENT = re.compile(r"//[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_STRINGS = re.compile(
    r"""'(?:\\.|[^'\\\n])*'|"(?:\\.|[^"\\\n])*"|`(?:\\.|[^`\\])*`""", re.S
)


def strip_noise(src: str, *, keep_specifiers: bool = False) -> str:
    """Blank comments, and optionally string bodies, preserving line numbers.

    Line numbers have to survive: a finding cites one, and a citation the reviewer
    cannot open is worse than no finding.
    """

    def blank(m: re.Match) -> str:
        return re.sub(r"[^\n]", " ", m.group(0))

    out = _BLOCK_COMMENT.sub(blank, src)
    out = _LINE_COMMENT.sub(blank, out)
    if not keep_specifiers:
        out = _STRINGS.sub(blank, out)
    return out


def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf8", errors="replace")
    except OSError:
        return ""


@functools.lru_cache(maxsize=16)
def js_files(root: Path) -> tuple[Path, ...]:
    if not root.is_dir():
        return ()
    return tuple(
        p for p in sorted(root.rglob("*"))
        if p.is_file() and p.suffix in JS_SUFFIXES
        and not SKIP_DIRS.intersection(p.parts)
    )


@functools.lru_cache(maxsize=32)
def _lines(root: Path, keep_specifiers: bool) -> tuple[tuple[Path, int, str], ...]:
    """Every cleaned line under `root`, cached.

    A module parametrises one row per file and each row asks a whole-tree
    question, so without this the tree is read O(files^2) times.
    """
    out = []
    for path in js_files(root):
        for i, line in enumerate(
            strip_noise(read(path), keep_specifiers=keep_specifiers).splitlines(), 1
        ):
            out.append((path, i, line))
    return tuple(out)


def grep(root: Path, pattern: re.Pattern[str], detail: str, *,
         keep_specifiers: bool = False) -> list[Hit]:
    """Every cleaned line under `root` matching `pattern`."""
    return [
        Hit(path=path, line=line, text=text, detail=detail)
        for path, line, text in _lines(root, keep_specifiers)
        if pattern.search(text)
    ]


# --- module specifiers --------------------------------------------------------

_IMPORT_FROM = re.compile(
    r"""\bimport\b[^;\n]*?\bfrom\s*['"]([^'"]+)['"]""", re.S)
_IMPORT_BARE = re.compile(r"""\bimport\s*['"]([^'"]+)['"]""")
_EXPORT_FROM = re.compile(r"""\bexport\b[^;\n]*?\bfrom\s*['"]([^'"]+)['"]""", re.S)
_REQUIRE = re.compile(r"""\brequire\s*\(\s*['"]([^'"]+)['"]\s*\)""")
_IMPORT_CALL = re.compile(r"""\bimport\s*\(\s*['"]([^'"]+)['"]\s*\)""")

#: `import(expr)` where expr is not a literal -- a specifier no static read can
#: resolve.  Not wrong by itself; worth a look, because it is also the shape a
#: submission would use to hide one.  Public, unlike the patterns above it,
#: because a module matches with it directly rather than through `specifiers`.
IMPORT_COMPUTED = re.compile(r"""\bimport\s*\(\s*(?!['"])[^)\s]""")


def specifiers(src: str) -> list[str]:
    """Every statically written module specifier in `src`.

    String bodies are kept here (a specifier *is* a string) but comments are not,
    so a commented-out import does not count.
    """
    clean = strip_noise(src, keep_specifiers=True)
    found: list[str] = []
    for pat in (_IMPORT_FROM, _IMPORT_BARE, _EXPORT_FROM, _REQUIRE, _IMPORT_CALL):
        found.extend(pat.findall(clean))
    return found


def specifier_hits(root: Path, want) -> list[Hit]:
    """Specifiers under `root` accepted by the predicate `want`."""
    hits: list[Hit] = []
    for path in js_files(root):
        clean = strip_noise(read(path), keep_specifiers=True)
        lines = clean.splitlines()
        for pat in (_IMPORT_FROM, _IMPORT_BARE, _EXPORT_FROM, _REQUIRE, _IMPORT_CALL):
            for m in pat.finditer(clean):
                spec = m.group(1)
                if not want(spec):
                    continue
                line = clean.count("\n", 0, m.start()) + 1
                text = lines[line - 1] if line - 1 < len(lines) else spec
                hits.append(Hit(path=path, line=line, text=text,
                                detail=f"imports {spec!r}"))
    return hits


def is_node_builtin(spec: str) -> bool:
    return spec.removeprefix("node:").split("/")[0] in NODE_BUILTINS


def is_relative(spec: str) -> bool:
    return spec.startswith((".", "/"))


def is_bare(spec: str) -> bool:
    """A package specifier: neither relative nor a Node built-in.

    In the core this is the interesting class.  The realm has no resolver and no
    `node_modules`, so a bare specifier there does not link -- whatever it names.
    """
    return not is_relative(spec) and not is_node_builtin(spec)


# --- globals ------------------------------------------------------------------

#: Names the browser realm does not define.  `require` and `module` are the CJS
#: pair; the rest are Node's.  Matched as whole words on cleaned text.
NODE_GLOBALS = {
    "require": r"\brequire\b",
    "module.exports": r"\bmodule\s*\.\s*exports\b",
    "exports": r"(?<![.\w])exports\s*(?:\.|\[|=)",
    "__dirname": r"\b__dirname\b",
    "__filename": r"\b__filename\b",
    "process": r"(?<![.\w])process\s*\.",
    "Buffer": r"(?<![.\w])Buffer\b",
    "global": r"(?<![.\w])global\s*\.",
    "globalThis.process": r"\bglobalThis\s*\.\s*process\b",
    "setImmediate": r"(?<![.\w])setImmediate\s*\(",
}


def node_globals(root: Path) -> list[Hit]:
    """Uses of a Node-only global anywhere under `root`."""
    hits: list[Hit] = []
    for name, pattern in sorted(NODE_GLOBALS.items()):
        hits.extend(grep(root, re.compile(pattern), f"uses {name}"))
    return sorted(hits, key=lambda h: (h.where, h.line))


# --- the shapes a submission uses to answer the letter of the task ------------

#: Reaching into the harness rather than being driven by it: a module that looks
#: for the test rig's own hooks is deciding what to do based on who is asking.
_HARNESS_HOOKS = re.compile(
    r"""\b(?:SRB_[A-Z_]+|__srb[A-Za-z_]*|srbHarness|__harness[A-Za-z_]*)\b""")

#: The same question asked a different way: is this code branching on whether it
#: is under test at all.
_TEST_AWARENESS = re.compile(
    r"""(?ix)
      \bprocess\s*\.\s*env\s*\.\s*(?:NODE_ENV|CI|SRB_[A-Z_]+)\b
    | \benv\s*\[\s*['"](?:NODE_ENV|CI|SRB_[A-Z_]+)['"]\s*\]
    | \bis(?:Test|Testing|UnderTest|Harness|Ci)\b
    | \b(?:JEST|VITEST|MOCHA|PYTEST)_\w+
    | \btypeof\s+(?:it|describe|expect)\s*(?:!==?|===?)
    | \bnavigator\s*\.\s*webdriver\b
    """)


def harness_hook_access(root: Path) -> list[Hit]:
    """Code under `root` that names one of the harness's own identifiers.

    String bodies are kept: the name usually appears as `env["SRB_..."]`.
    """
    return grep(root, _HARNESS_HOOKS, "names a harness identifier",
                keep_specifiers=True)


def test_awareness(root: Path) -> list[Hit]:
    """Code under `root` that could be asking whether it is being tested."""
    return grep(root, _TEST_AWARENESS, "reads test/CI state",
                keep_specifiers=True)


#: A function that ends in `Sync` and takes something path-shaped: the signature
#: of `fs`, wherever it is actually implemented.  A submission is free to define
#: these -- the platform object has to provide file access somehow -- so this is
#: a pointer for the reviewer, not a verdict.
_SYNC_SHIM = re.compile(
    r"""(?x)
      \b(?:function\s+|const\s+|let\s+|var\s+)?
      (read|write|append|stat|lstat|exists|readdir|realpath|access|mkdir|unlink)
      (?:File)?Sync\b
    """)


def shim_modules(root: Path) -> list[Hit]:
    """`*Sync` file-shaped call sites or definitions under `root`."""
    return grep(root, _SYNC_SHIM, "fs-shaped *Sync name")


def imports_of(root: Path, needle: str) -> list[Hit]:
    """Specifiers under `root` whose path mentions `needle`.

    Used to ask whether a tree still depends on a directory it was supposed to
    have stopped depending on.
    """
    return specifier_hits(root, lambda s: needle in s)


# --- duplication --------------------------------------------------------------

#: Two files sharing at least this many non-trivial lines, and that much of the
#: smaller file, are near-duplicates.  A port that copies a module rather than
#: moving it leaves both -- and then the reviewer needs to know which one runs.
#:
#: 20 rather than a rounder 40, because 40 was measured and found to be twice as
#: high as it needed to be.  Stylus's 137 `lib/` modules have a median of 17
#: significant lines, so a threshold of 40 could only ever see the 31 largest of
#: them: the median module could be copied verbatim and go unreported.  Sweeping
#: the pristine tree gives 0 pairs at 20 lines / 60%, 3 at 15 (`binop`/`member`,
#: `feature`/`property`, `ident`/`literal` -- sibling AST node classes that really
#: do look alike), and 8 at 12.  20 is the lowest setting that is silent on
#: unmodified upstream, which is the only calibration point that means anything
#: here: a threshold justified by argument rather than by a sweep is a threshold
#: nobody has checked.
#:
#: Small files are still invisible to this, and no threshold fixes that -- which is
#: what `identical_files` is for.
DUP_MIN_SHARED_LINES = 20
DUP_MIN_FRACTION = 0.6

#: Below this many bytes, two identical files are not evidence of anything: a
#: one-line re-export is legitimately repeated across a tree.
IDENTICAL_MIN_BYTES = 60


def _significant(src: str) -> set[str]:
    """Lines worth comparing: no comments, no punctuation-only, no boilerplate."""
    out = set()
    for line in strip_noise(src).splitlines():
        s = line.strip()
        if len(s) >= 12 and not s.startswith(("import ", "export ", "require(")):
            out.add(s)
    return out


def duplicate_files(*roots: Path) -> list[tuple[Path, Path, int, float]]:
    """Near-duplicate file pairs across `roots`, worst overlap first."""
    seen: list[tuple[Path, set[str]]] = []
    for root in roots:
        for path in js_files(root):
            body = _significant(read(path))
            if len(body) >= DUP_MIN_SHARED_LINES:
                seen.append((path, body))

    pairs: list[tuple[Path, Path, int, float]] = []
    for i, (pa, a) in enumerate(seen):
        for pb, b in seen[i + 1:]:
            shared = len(a & b)
            if shared < DUP_MIN_SHARED_LINES:
                continue
            fraction = shared / min(len(a), len(b))
            if fraction >= DUP_MIN_FRACTION:
                pairs.append((pa, pb, shared, fraction))
    return sorted(pairs, key=lambda t: (-t[3], -t[2]))


def identical_files(*roots: Path) -> list[list[Path]]:
    """Groups of byte-identical files, ignoring leading and trailing whitespace.

    The complement of `duplicate_files` and the more useful of the two, because it
    has no threshold to calibrate and no size floor beyond "long enough to be a
    module".  Copying a file is the most common way to end up with two
    implementations, and it is the one case where a similarity score is a worse
    instrument than equality: a 12-line module copied verbatim is invisible to any
    line-overlap threshold that is quiet on unmodified upstream, and obvious here.

    Sweeping pristine Stylus finds no group at all, across all 137 modules -- so a
    finding here is something the submission introduced.
    """
    groups: dict[str, list[Path]] = {}
    for root in roots:
        for path in js_files(root):
            body = read(path).strip()
            if len(body) < IDENTICAL_MIN_BYTES:
                continue
            key = hashlib.sha256(body.encode("utf8", "replace")).hexdigest()
            groups.setdefault(key, []).append(path)
    return [sorted(g) for g in groups.values() if len(g) > 1]


# --- what State A said --------------------------------------------------------

def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16] if path.is_file() else ""


def manifest(root: Path, sub: str) -> dict[str, str]:
    """`relative path -> short digest` for every file under `root/sub`.

    Comparing two of these is how a module asks whether a directory the task
    freezes came through unchanged -- byte equality, not a similarity score, so
    there is no threshold to argue about.
    """
    base = root / sub
    if not base.is_dir():
        return {}
    return {
        str(p.relative_to(base)): digest(p)
        for p in sorted(base.rglob("*"))
        if p.is_file() and not SKIP_DIRS.intersection(p.parts)
    }


def package_json(root: Path) -> dict:
    try:
        return json.loads(read(root / "package.json") or "{}")
    except json.JSONDecodeError:
        return {}


def count_pattern(path: Path, pattern: re.Pattern[str]) -> int:
    """How many times `pattern` occurs in `path`.

    For the file the task permits rewriting: a digest says only "changed", which
    is expected, so the question becomes whether what it *contains* still adds up.
    """
    return len(pattern.findall(read(path)))


def skip_unless_original() -> None:
    """State A has to be mounted for the comparison modules to mean anything."""
    if not (ORIGINAL / "package.json").is_file():
        pytest.skip(f"State A not mounted at {ORIGINAL}; nothing to compare against")


# --- how a check reports ------------------------------------------------------

def fail_if(bad, message: str) -> None:
    """Fail with `message` when `bad` is truthy.  The only way to report here.

    This exists because `assert cond, message` does not survive the rendering path
    this suite's messages are written for.  `pytest_module._summary` takes the last
    line of the failure that starts with `E `, and pytest's assertion rewriting
    appends its own explanation of the expression *after* the message -- so the
    line the reviewer sees is not the citation the check put last, it is pytest's
    `repr` of whatever was on the left of the comparison.  Measured, on the four
    idioms this suite would otherwise use:

        assert not hits, msg        -> "assert not [Hit(path=PosixPath('/opt/wo..."
        assert "lib/" not in text   -> "?                             ++++"
        assert isinstance(x, dict)  -> "+  where False = isinstance(None, dict)"
        assert files, msg           -> "assert []"
        fail_if(hits, msg)          -> the message's own last line, verbatim

    Four of the five leads in a do-nothing submission's digest read like the first
    four before this function existed: a prompt section headed "Flagged, in the
    scan's own words" in which the scan's own words had been replaced by pytest's.
    The full message survives in `detail` and is rendered below the summary, so
    nothing was lost -- but the summary is the line that decides whether a lead
    gets followed, and a lead that reads `? ++++` does not get followed.

    `__tracebackhide__` keeps this frame out of the traceback, so `detail` still
    opens at the check's own line rather than at this file: a reviewer following a
    finding should land in the check that made it.
    """
    __tracebackhide__ = True
    if bad:
        pytest.fail(message)


# --- fixtures -----------------------------------------------------------------
# This file is loaded with `-p srbscan`, so these are available to every module
# without a conftest.  Session-scoped and returning constants: they exist so a
# module reads `repo` in its signature rather than a global, which makes it
# obvious at the call site which tree a check is looking at.

@pytest.fixture(scope="session")
def repo() -> Path:
    return REPO


@pytest.fixture(scope="session")
def original() -> Path:
    return ORIGINAL


@pytest.fixture(scope="session")
def core() -> Path:
    return CORE_ROOT


@pytest.fixture(scope="session")
def node() -> Path:
    return NODE_ROOT


# --- what a skip means here ---------------------------------------------------

def pytest_collection_modifyitems(items) -> None:
    """License every skip in this suite, because none of them is a miss.

    `swerefactor.pytest_module` rewrites a skip without `srb_skip_ok` into a *fail*
    whose summary is the single word "skipped".  That is the right policy in stage
    2, where a skip means a build did not produce the artifact a check was going
    to measure and nothing else explains the gap.  It is the wrong one here, and
    measurably: on a submission with no `src/core/`, the eighteen closure checks
    that cannot read a tree that isn't there arrived in the reviewer's prompt as
    eighteen flagged findings reading `[fail] ...test_core_is_esm_not_commonjs:
    skipped`.  Twenty-four in total, against five real ones -- the leads list
    became mostly entries that said a check had not looked at anything, presented
    to the reviewer as things the scan had found.

    Blanket rather than per-check because it follows from a property of the whole
    suite: `scan.Scanner` sets `required = False` on every check here, so nothing
    in this file can be scored, so a skip cannot pay for itself the way it could
    in stage 2.  The only cost of one is invisibility -- `scan.digest` renders
    skips as a bare count, "24 did not apply", with no reason attached -- and that
    is paid for by `closure::test_the_core_tree_exists` and
    `frozen::test_state_a_is_mounted`, which fail rather than skip so that a
    missing tree is one legible finding instead of two dozen mute ones.
    """
    for item in items:
        item.add_marker(pytest.mark.srb_skip_ok)
