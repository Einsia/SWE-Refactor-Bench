"""Purpose-built import trees, for the parts of resolution the corpus cannot reach.

The corpus resolves every import in exactly one way, so it never says which
candidate *wins* when several match.  That precedence is the most breakable part
of the algorithm, and it is invisible until two candidates exist -- so each
scenario here builds a tiny tree where they do.

Every rule below was measured against pristine State A before it was written
down; the notes name what was observed.  The two that matter most:

  * lookup paths are searched **last-first** (`lib/utils.js`: `while (i--)`), so
    with `paths = [one, two]` a name present in both resolves to `two`.  A port
    that iterates forwards passes the whole corpus and fails here.
  * `@import` of the same file twice emits it twice; `@require` emits it once.
    A cycle through `@import` throws, and the same cycle through `@require`
    compiles.

Scenarios are described in virtual paths.  The oracle gets them materialised into
a scratch directory and mounted at `/proj`; the sandbox gets the identical bytes
as its VFS.  Neither reads the corpus, so nothing here can be perturbed by the
submission's own `test/` directory.
"""
from __future__ import annotations

import base64
import functools
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import layout, vfs

P = layout.VPROJ


@dataclass(frozen=True)
class Scenario:
    """One resolution question, and the tree that asks it."""

    id: str
    #: Virtual path -> text contents.  Written for the oracle, mounted for the sandbox.
    files: dict[str, str]
    #: Which file to compile, as a virtual path.
    entry: str
    #: `renderer.set('paths', ...)`, virtual.  Order is significant; see the note.
    paths: tuple[str, ...] = ()
    options: dict = field(default_factory=dict)
    #: True when State A raises for this tree; the check then compares the failure.
    throws: bool = False
    note: str = ""


def _s(sid: str, files: dict, entry: str, **kw) -> Scenario:
    """Prefixes every path with the scenario id, so trees cannot collide."""
    pref = f"{P}/{sid}"
    return Scenario(
        id=sid,
        files={f"{pref}/{k}": v for k, v in files.items()},
        entry=f"{pref}/{entry}",
        paths=tuple(f"{pref}/{p}" for p in kw.pop("paths", ())),
        **kw,
    )


SCENARIOS: tuple[Scenario, ...] = (
    # --- which candidate wins ------------------------------------------------
    _s("file-beats-dir-index",
       {"foo.styl": ".from-file{a:1}\n",
        "foo/index.styl": ".from-dir-index{a:2}\n",
        "entry.styl": "@import 'foo'\n"},
       "entry.styl",
       note="`foo.styl` is tried before `foo/index.styl`"),

    _s("ext-beats-extensionless",
       {"foo.styl": ".styl-file{a:1}\n",
        "foo": ".extensionless{a:2}\n",
        "entry.styl": "@import 'foo'\n"},
       "entry.styl",
       note="the name is always suffixed with .styl first"),

    _s("index-fallback",
       {"pkg/index.styl": ".pkg-index{a:1}\n",
        "entry.styl": "@import 'pkg'\n"},
       "entry.styl",
       note="`pkg/index.styl` when `pkg.styl` is absent"),

    _s("dir-named-file-fallback",
       {"pkg/pkg.styl": ".pkg-self{a:1}\n",
        "entry.styl": "@import 'pkg'\n"},
       "entry.styl",
       note="`pkg/pkg.styl`, the third candidate, after .styl and /index.styl"),

    _s("index-beats-dir-named-file",
       {"pkg/index.styl": ".idx{a:1}\n",
        "pkg/pkg.styl": ".self{a:2}\n",
        "entry.styl": "@import 'pkg'\n"},
       "entry.styl",
       note="/index.styl outranks /<name>.styl"),

    _s("dotted-dir-index",
       {"my.pkg/index.styl": ".dotted{a:1}\n",
        "entry.styl": "@import 'my.pkg'\n"},
       "entry.styl",
       note="a dot in the directory name is not an extension"),

    # --- lookup path order ---------------------------------------------------
    # The rule most likely to be implemented backwards.
    _s("lookup-last-path-wins",
       {"one/shared.styl": ".from-one{a:1}\n",
        "two/shared.styl": ".from-two{a:2}\n",
        "entry.styl": "@import 'shared'\n"},
       "entry.styl", paths=("one", "two"),
       note="paths are searched last-first, so `two` wins"),

    _s("lookup-order-is-not-alphabetical",
       {"one/shared.styl": ".from-one{a:1}\n",
        "two/shared.styl": ".from-two{a:2}\n",
        "entry.styl": "@import 'shared'\n"},
       "entry.styl", paths=("two", "one"),
       note="the same two directories, listed the other way: `one` wins now"),

    _s("lookup-three-deep",
       {"a/s.styl": ".a{a:1}\n", "b/s.styl": ".b{a:2}\n", "c/s.styl": ".c{a:3}\n",
        "entry.styl": "@import 's'\n"},
       "entry.styl", paths=("a", "b", "c"),
       note="last of three"),

    _s("relative-beats-lookup-path",
       {"sibling.styl": ".local{a:1}\n",
        "far/sibling.styl": ".far{a:2}\n",
        "entry.styl": "@import 'sibling'\n"},
       "entry.styl", paths=("far",),
       note="the importing file's own directory is consulted first"),

    # --- relative forms ------------------------------------------------------
    _s("nested-relative",
       {"sub/inner.styl": "@import 'deeper'\n.inner{a:1}\n",
        "sub/deeper.styl": ".deeper{a:2}\n",
        "entry.styl": "@import 'sub/inner'\n"},
       "entry.styl",
       note="a nested import resolves against its own file, not the entry"),

    _s("parent-relative",
       {"up.styl": ".up{a:1}\n",
        "sub/inner.styl": "@import '../up'\n.inner{a:2}\n",
        "entry.styl": "@import 'sub/inner'\n"},
       "entry.styl",
       note="`../` climbs from the importing file"),

    _s("explicit-dot-slash",
       {"here.styl": ".here{a:1}\n",
        "entry.styl": "@import './here'\n"},
       "entry.styl"),

    _s("deep-chain",
       {"a/b/c/d.styl": ".d{a:4}\n",
        "a/b/c.styl": "@import 'c/d'\n.c{a:3}\n",
        "a/b.styl": "@import 'b/c'\n.b{a:2}\n",
        "entry.styl": "@import 'a/b'\n.e{a:1}\n"},
       "entry.styl",
       note="four levels, each resolving against its own directory"),

    _s("underscore-partial",
       {"_partial.styl": ".partial{a:1}\n",
        "entry.styl": "@import '_partial'\n"},
       "entry.styl",
       note="Stylus has no special rule for leading underscores"),

    # --- globs ---------------------------------------------------------------
    # Upstream globs with `glob@7`, which sorts its results unless told not to.
    # A port implementing globs over `platform.readDir` therefore has to sort --
    # and the harness hands it an unsorted directory listing to make sure it does.
    _s("glob-flat",
       {"parts/a.styl": ".pa{a:1}\n", "parts/b.styl": ".pb{a:2}\n",
        "parts/c.styl": ".pc{a:3}\n",
        "entry.styl": "@import 'parts/*'\n"},
       "entry.styl",
       note="expanded in sorted order, a then b then c"),

    _s("glob-sorted-not-readdir-order",
       {"parts/zebra.styl": ".z{a:1}\n", "parts/apple.styl": ".a{a:2}\n",
        "parts/mango.styl": ".m{a:3}\n",
        "entry.styl": "@import 'parts/*'\n"},
       "entry.styl",
       note="names whose sorted order is not their creation order"),

    _s("glob-recursive",
       {"p/a.styl": ".a{a:1}\n", "p/sub/b.styl": ".b{a:2}\n",
        "p/sub/deep/c.styl": ".c{a:3}\n",
        "entry.styl": "@import 'p/**/*'\n"},
       "entry.styl",
       note="`**` descends; the order is still sorted"),

    _s("glob-suffixed",
       {"p/a.styl": ".a{a:1}\n", "p/b.styl": ".b{a:2}\n", "p/notes.txt": "x\n",
        "entry.styl": "@import 'p/*.styl'\n"},
       "entry.styl",
       note="a non-matching extension in the same directory is skipped"),

    _s("glob-no-match", {"entry.styl": "@import 'empty/*'\n"}, "entry.styl",
       throws=True, note="a glob matching nothing is a failed import, not a no-op"),

    # --- import vs require ---------------------------------------------------
    _s("import-twice-duplicates",
       {"dup.styl": ".dup{a:1}\n", "entry.styl": "@import 'dup'\n@import 'dup'\n"},
       "entry.styl",
       note="@import has no memory: the rules appear twice"),

    _s("require-twice-dedupes",
       {"dup.styl": ".dup{a:1}\n", "entry.styl": "@require 'dup'\n@require 'dup'\n"},
       "entry.styl",
       note="@require emits once, and must remember across the whole compile"),

    _s("require-then-import",
       {"dup.styl": ".dup{a:1}\n", "entry.styl": "@require 'dup'\n@import 'dup'\n"},
       "entry.styl",
       note="the @import is not suppressed by the earlier @require"),

    _s("require-glob-dedupes",
       {"p/one.styl": ".one{q:1}\n", "p/two.styl": ".two{q:2}\n",
        "entry.styl": "@require 'p/*'\n@require 'p/one'\n"},
       "entry.styl",
       note="dedup is by resolved path, so a glob and a direct name agree"),

    # --- cycles --------------------------------------------------------------
    _s("cycle-through-import",
       {"a.styl": "@import 'b'\n.a{a:1}\n", "b.styl": "@import 'a'\n.b{a:2}\n",
        "entry.styl": "@import 'a'\n"},
       "entry.styl", throws=True,
       note="State A raises rather than recursing forever"),

    _s("cycle-through-require",
       {"a.styl": "@require 'b'\n.a{a:1}\n", "b.styl": "@require 'a'\n.b{a:2}\n",
        "entry.styl": "@require 'a'\n"},
       "entry.styl",
       note="the same cycle compiles, because @require breaks it"),

    # Measured, not assumed: this throws.  `utils.lookup` takes the importing
    # file as its `ignore` argument and skips it, so a file that names itself
    # finds no candidate at all rather than resolving to a cycle.
    _s("self-require",
       {"entry.styl": "@require 'entry'\n.only{a:1}\n"},
       "entry.styl", throws=True,
       note="the importing file is excluded from its own candidate list"),

    # --- css imports ---------------------------------------------------------
    _s("css-import-passthrough",
       {"thing.css": ".css{a:1}\n", "entry.styl": "@import 'thing.css'\n"},
       "entry.styl",
       note="left in the output verbatim, and nothing is read"),

    _s("css-import-inlined",
       {"thing.css": ".css{a:1}\n", "entry.styl": "@import 'thing.css'\n"},
       "entry.styl", options={"include css": True},
       note="the same tree with the option on, which does read the file"),

    # --- failures ------------------------------------------------------------
    _s("missing-import", {"entry.styl": "@import 'nope'\n"}, "entry.styl",
       throws=True, note="every candidate is tried, then it fails"),

    _s("missing-nested",
       {"sub/inner.styl": "@import 'gone'\n", "entry.styl": "@import 'sub/inner'\n"},
       "entry.styl", throws=True,
       note="the error names the nested file, not the entry"),

    _s("import-of-directory-without-index",
       {"bare/other.styl": ".o{a:1}\n", "entry.styl": "@import 'bare'\n"},
       "entry.styl", throws=True,
       note="a directory with no index.styl and no same-named file"),
)


#: Scenarios whose CSS is expected to equal another scenario's, with the reason.
#: Declared so that a collision report is readable: an undeclared one means two
#: scenarios are measuring the same thing.
INTENDED_COLLISIONS: dict[str, str] = {
    "require-then-import": (
        "equals `import-twice-duplicates`: the point is that the earlier @require "
        "does not suppress the later @import, so the duplicate must still appear"
    ),
}


@functools.lru_cache(maxsize=1)
def scenario_ids() -> tuple[str, ...]:
    return tuple(s.id for s in SCENARIOS)


def by_id(sid: str) -> Scenario:
    for s in SCENARIOS:
        if s.id == sid:
            return s
    raise KeyError(sid)


@functools.lru_cache(maxsize=1)
def all_files() -> dict[str, str]:
    """Every scenario's tree at once, as virtual path -> text.

    One tree for all scenarios is safe because `_s` namespaces each by id, and it
    means the whole dimension costs one oracle process and one sandbox process.
    """
    out: dict[str, str] = {}
    for s in SCENARIOS:
        for path, text in s.files.items():
            if path in out and out[path] != text:
                raise AssertionError(f"scenario trees collide at {path}")
            out[path] = text
    return out


@functools.lru_cache(maxsize=1)
def scratch_root() -> Path:
    """Materialises the trees on disk for the oracle, and returns the root.

    Rebuilt from scratch each run so a stale file cannot answer a question.
    """
    root = layout.WORK / "resolution-tree"
    if root.exists():
        shutil.rmtree(root)
    for vpath, text in all_files().items():
        rel = vpath[len(P):].lstrip("/")
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf8")
    return root


def oracle_mounts() -> dict[str, str]:
    """`/proj` onto the scratch tree, `/runtime` onto State A's built-in library."""
    return {
        layout.VRUNTIME: str(layout.STATE_A / "lib" / "functions"),
        P: str(scratch_root()),
    }


@functools.lru_cache(maxsize=1)
def sandbox_files() -> dict[str, str]:
    """The same trees as a VFS payload, plus the submission's built-in library."""
    out = {p: base64.b64encode(t.encode("utf8")).decode("ascii")
           for p, t in all_files().items()}
    runtime = vfs.find_runtime_root()
    if runtime is not None:
        out.update(vfs.runtime_files(runtime))
    return out


def op_for(s: Scenario) -> dict:
    settings: dict = {"filename": s.entry}
    if s.paths:
        settings["paths"] = list(s.paths)
    return {
        "id": s.id,
        "kind": "render",
        "sourceFile": s.entry,
        "set": settings,
        "options": dict(s.options),
    }


def ops() -> list[dict]:
    return [op_for(s) for s in SCENARIOS]


def diagnostic(error: dict | None) -> str:
    """The last line of a Stylus error message -- the sentence that says what went wrong.

    A Stylus error is a location, a source excerpt with a caret, and then one
    diagnostic line: `failed to locate @import file nope.styl`, or `import loop
    has been found`.  The excerpt is formatting; the last line is the finding, and
    it is what distinguishes "this import does not resolve" from "this file is
    part of a cycle".

    Comparing it matters because `not ok` on its own is too weak: a port that
    cannot compile anything raises for every negative scenario too, and would be
    credited for all of them.
    """
    if not error:
        return ""
    lines = [ln.strip() for ln in str(error.get("message") or "").splitlines() if ln.strip()]
    return lines[-1] if lines else ""
