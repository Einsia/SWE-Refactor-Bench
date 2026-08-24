"""Diagnostics: the formatted message, decomposed into the parts that build it.

`utils.formatException` assembles every user-visible Stylus error out of five
pieces, and a port can get any one of them wrong independently:

    /proj/broken.styl:3:5          <- header: filename:lineno:column
       1| .a                       <- gutter: a numbered window of the source
       2|   color red
       3|   color                  <- the offending line
    ------------^                  <- caret: dashes then a marker
                                   <- one blank line
    expected "expression"          <- body: the original message
    from mixin at line 2           <- stack: `stylusStack`, when there is one

The suite scores those separately, so a port whose diagnostics are structurally
right but off by one column keeps the header and the gutter and loses the caret.
Comparing the message as one opaque string -- which is what `test_jsapi.py` does
for the eight cases in its own catalog -- would spend the whole dimension on a
single boolean per case.

Three details in that function are the ones ports actually get wrong, and each
gets cases built to expose it:

**The window is asymmetric, and the option that would resize it is dead.**
`context = options.context || 8`, then `context = context / 2`, then
`lines.slice(start, end)` with `start = max(lineno - 4, 1)` and
`end = min(lines.length, lineno + 4)`.  `slice` excludes its end index, so the
window is four lines before the error and *three* after; a port that reads the
code as "four each side" produces one extra line.

`options.context`, though, is never set on the way in.  `Renderer#render`'s catch
block builds `var options = {}` and copies exactly four fields into it --
`input`, `filename`, `lineno`, `column` -- so the renderer's own options never
reach `formatException` and the window is always the default 8.  Measured all
three ways: `renderer.set('context', 2)` and a constructor `{context: 2}` both
change nothing, while calling `formatException` directly with `{context: 2}`
really does narrow the window to two lines.  So the option is live code reachable
only from outside the public API, and `context-option-is-inert` pins that: a port
that "fixed" the omission would quietly narrow every diagnostic for any user who
has `context` set.

**The line array is 1-indexed by construction.**  `('\\n' + str).split('\\n')`
puts an empty string at index 0 so that `lines[n]` is source line `n`.  A port
that splits `str` directly is off by one everywhere, which the gutter rows catch.

**`Array(n).join('-')` yields n-1 dashes.**  The caret line is
`Array(lineno.toString().length + 5 + column).join('-') + '^'`, so the marker sits
at `len(str(lineno)) + 4 + column` dashes.  Two separate off-by-ones -- the join
idiom and the `+ 5` -- cancel into a result that is easy to reproduce by accident
and easy to get wrong by reasoning.  `line-number-widens-gutter` puts the error
past line 9 so the `toString().length` term changes, which a port that hardcoded
the width would miss.

Beyond the formatting there are two filesystem reads, and they fail in opposite
directions.  `Evaluator#visit` does

    try { err.input = fs.readFileSync(err.filename, 'utf8'); } catch { /* ignore */ }

so an error raised inside an imported file has its caret diagram drawn against
*that file's* text, freshly read -- and if the read fails, the assignment is
skipped and `formatException` silently falls back to the compiled string.  The
sourcemapper's read (see `harness/sourcemaps.py`) has no guard at all and turns a
successful compile into a failure.  A port that routes both through
`platform.readFile` with one shared error policy gets one of them wrong whichever
policy it picks, so `import-error-shows-imported-source` and
`unreadable-file-falls-back` are a pair that has to be satisfied together.

`Evaluator#visit` also decides *ownership*: `if (err.filename) throw err` means the
innermost frame to attach a filename keeps it, and every outer frame re-throws
untouched.  `nested-import-error-keeps-innermost` is the row for that, because a
port that assigned unconditionally would report the entry file for an error three
imports deep and still pass every single-file case.

Two constraints on the catalog rather than on the behaviour it measures.

Every case's source is distinct, for the reason `harness/jsapi.py` records:
`Parser.cache` is static and keyed on the source text, so two ops with identical
bytes share a parse and the second inherits the first's filename -- which for this
dimension would mean inheriting the wrong `header`.

And every case pins `filename` to a virtual path.  The header embeds it verbatim,
so a case that let it default would compare `stylus` against whatever the runner
happened to pass, and the `paths` a case needs are virtual for the same reason.
"""
from __future__ import annotations

import base64
import functools
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import layout, vfs

#: Where this dimension's purpose-built trees live, virtually.
E = f"{layout.VPROJ}/errdiag"


@dataclass(frozen=True)
class ErrCase:
    """One compile that fails, and what its diagnostic is measured for."""

    id: str
    #: The entry file's text. Written to disk for the oracle and mounted into the
    #: sandbox VFS, never passed inline, so both sides compile identical bytes and
    #: `err.input`-from-disk means the same thing on both.
    source: str
    #: Extra files in the tree, virtual path -> text.
    files: dict[str, str] = field(default_factory=dict)
    #: Renderer settings. `filename` is filled in from the case id if absent.
    settings: dict = field(default_factory=dict)
    options: dict = field(default_factory=dict)
    #: Import search paths, virtual.
    paths: tuple[str, ...] = ()
    #: Which decomposed parts this case is expected to carry. `stack` is absent
    #: for anything that fails before evaluation, and `caret`/`gutter` are absent
    #: when the error has no position at all.
    parts: tuple[str, ...] = ("header", "gutter", "caret", "body")
    note: str = ""

    @property
    def entry(self) -> str:
        return f"{E}/{self.id}.styl"

    def to_op(self) -> dict:
        settings = {"filename": self.entry, **self.settings}
        if self.paths:
            settings["paths"] = list(self.paths)
        return {
            "id": self.id,
            "kind": "render",
            "sourceFile": self.entry,
            "set": settings,
            "options": dict(self.options),
        }


#: `PART_NAMES` is the row axis. Declared here rather than derived from the cases
#: so the number of rows is a property of this file, not of what the oracle did.
PART_NAMES: tuple[str, ...] = ("header", "gutter", "caret", "body", "stack")


# ----------------------------------------------------------------- lexer/parser
#
# These fail before evaluation, so they have no `stylusStack` and no filename
# attached by the evaluator -- the renderer's `err.filename || this.options.filename`
# fallback supplies the header instead. That fallback is itself worth a row: a port
# that only set the filename in the evaluator would emit `undefined:1:1` here.

_PARSE_CASES: tuple[ErrCase, ...] = (
    ErrCase(
        id="parse-unclosed-brace",
        source=".parse-unclosed-brace\n  color: rgba(0,0,0\n",
        parts=("header", "gutter", "caret", "body"),
        note="an unterminated call: the parser reports where it gave up, not where "
             "the paren opened.",
    ),
    ErrCase(
        id="parse-unexpected-outdent",
        source=".parse-unexpected-outdent\n    color red\n  padding 0\n",
        note="inconsistent indentation depth -- the lexer's outdent bookkeeping.",
    ),
    ErrCase(
        id="parse-mixed-indentation",
        source=".parse-mixed-indentation\n\tcolor red\n  padding 0\n",
        note="a tab-indented line followed by a space-indented one. The lexer has a "
             "dedicated `SyntaxError` for mixing the two, but it is unreachable "
             "this way -- indent bookkeeping raises a plain `ParseError` first -- so "
             "this measures the reachable path.",
    ),
    ErrCase(
        id="parse-bad-selector",
        source=".parse-bad-selector\n  color red\n\n{{{\n  color blue\n",
        note="interpolation opened where a selector was expected.",
    ),
    ErrCase(
        id="parse-stray-close-paren",
        source=".parse-stray-close-paren\n  width 1)\n",
        note="a close paren with nothing open, for the distinct body "
             "`unexpected \")\"`. Like most malformed input it is still reported at "
             "`eos` -- the lexer has already run to the end by the time the parser "
             "objects -- so the mid-line caret comes from "
             "`parse-double-comma-args` and the evaluation cases instead.",
    ),
    ErrCase(
        id="parse-double-comma-args",
        source="f(a,,b)\n  return a\n\n.parse-double-comma-args\n  width f(1, 2)\n",
        note="an empty parameter slot. Also mid-line, and on line 1, where the "
             "context window's lower bound clamps.",
    ),
    ErrCase(
        id="parse-unclosed-fn-paren",
        source="f(a\n  return a\n\n.parse-unclosed-fn-paren\n  width f(1)\n",
        note="`failed to find closing paren` comes from the lexer's own scan rather "
             "than from `Parser#error`, so the body is not built by the "
             "`{peek}` substitution the other parse cases go through.",
    ),
    ErrCase(
        id="parse-selector-only-comma",
        source=",\n  color red\n",
        settings={},
        note="fails at line 1 column 1, the degenerate case for both the window's "
             "lower clamp and the caret offset.",
    ),
    ErrCase(
        id="line-number-widens-gutter",
        source=".line-number-widens-gutter\n" + "".join(
            f"  a-{i}: {i}px\n" for i in range(1, 12)
        ) + "  color: rgba(\n",
        note="the error is past line 9, so `lineno.toString().length` is 2 and both "
             "the gutter padding and the caret offset shift. A port that hardcoded "
             "either passes every single-digit case and fails this one.",
    ),
)


# -------------------------------------------------------------------- evaluation
#
# These reach the evaluator, so they carry `stylusStack` and a filename the
# evaluator attached from the failing node.

_EVAL_CASES: tuple[ErrCase, ...] = (
    ErrCase(
        id="eval-bad-color-arg",
        source='.eval-bad-color-arg\n  color lighten("nope", 10%)\n',
        parts=("header", "gutter", "caret", "body", "stack"),
        note="a colour built-in handed a string. The plainest evaluation failure, "
             "and the baseline for the stack row.",
    ),
    ErrCase(
        id="eval-bad-selector-arg",
        source=".eval-bad-selector-arg\n  width selector-exists(1)\n",
        parts=("header", "gutter", "caret", "body", "stack"),
        note="a different built-in with a different message, so the body row is not "
             "measuring one string across the whole dimension.",
    ),
    ErrCase(
        id="eval-mixin-arity",
        source="m(a, b)\n  width a + b\n\n.eval-mixin-arity\n  m(1)\n",
        parts=("header", "gutter", "caret", "body", "stack"),
        note="the stack has a frame for the mixin, so `stack` is non-empty here "
             "where it is the empty string for a top-level failure.",
    ),
    ErrCase(
        id="eval-deep-mixin-stack",
        source=("outer()\n  inner()\n\ninner()\n  deepest()\n\ndeepest()\n"
                "  color rgba()\n\n.eval-deep-mixin-stack\n  outer()\n"),
        parts=("header", "gutter", "caret", "body", "stack"),
        note="three frames. The stack row compares the whole rendering, so frame "
             "order and separator are both pinned.",
    ),
    ErrCase(
        id="eval-bad-extend",
        source=".eval-bad-extend\n  @extend .nothing-here\n",
        parts=("header", "gutter", "caret", "body"),
        note="`@extend` against a missing selector. Positioned but, per the jsapi "
             "measurements, carries no filename -- so the header comes from the "
             "renderer's option fallback.",
    ),
    ErrCase(
        id="eval-throwing-builtin",
        source=".eval-throwing-builtin\n  width unit(1px, 3)\n",
        parts=("header", "gutter", "caret", "body", "stack"),
        note="a built-in rejecting its argument: the error crosses from the "
             "function library back into the evaluator.",
    ),
)


# ------------------------------------------------------------ the two disk reads
#
# `import-error-shows-imported-source` and `unreadable-file-falls-back` are the
# pair described in the module docstring. Keep them together.

_IMPORT_CASES: tuple[ErrCase, ...] = (
    ErrCase(
        id="import-error-shows-imported-source",
        source="@import 'broken-a'\n\n.import-error-shows-imported-source\n  color red\n",
        files={
            f"{E}/broken-a.styl":
                "// a distinctive first line nothing else has\n"
                ".broken-a\n"
                "  width unquote(3)\n",
        },
        paths=(E,),
        parts=("header", "gutter", "caret", "body", "stack"),
        note="the caret diagram must show `broken-a.styl`'s lines, which only "
             "appear if `err.input` was re-read from that file. A port that left "
             "`err.input` unset would draw the diagram against the entry file and "
             "produce a gutter of the wrong text at a plausible line number.",
    ),
    ErrCase(
        id="import-error-header-names-imported-file",
        source="@import 'broken-b'\n\n.import-error-header-names-imported-file\n  color red\n",
        files={
            f"{E}/broken-b.styl": ".broken-b\n  width basename(3)\n",
        },
        paths=(E,),
        parts=("header", "gutter", "caret", "body", "stack"),
        note="the header names the imported file, not the entry. Split from the "
             "case above so a port that reads the file but reports the wrong name "
             "loses one row rather than two.",
    ),
    ErrCase(
        id="nested-import-error-keeps-innermost",
        source="@import 'mid-c'\n\n.nested-import-error-keeps-innermost\n  color red\n",
        files={
            f"{E}/mid-c.styl": "@import 'deep-c'\n\n.mid-c\n  color blue\n",
            f"{E}/deep-c.styl": '.deep-c\n  color darken("nope", 10%)\n',
        },
        paths=(E,),
        parts=("header", "gutter", "caret", "body", "stack"),
        note="`if (err.filename) throw err` -- the first frame to claim the name "
             "wins and every frame outside it must decline. `darken` is defined in "
             "the built-in `.styl` library rather than in JavaScript, so the "
             "claimant is `index.styl` and all three files here -- entry, mid, deep "
             "-- are frames that must leave it alone.",
    ),
    ErrCase(
        id="import-parse-error-in-imported-file",
        source="@import 'badsyntax-d'\n\n.import-parse-error-in-imported-file\n  color red\n",
        files={
            f"{E}/badsyntax-d.styl": ".badsyntax-d\n    color red\n  padding 0\n",
        },
        paths=(E,),
        parts=("header", "gutter", "caret", "body"),
        note="a *parse* error inside an import, which fails in `Evaluator#visitImport`'s "
             "own catch rather than the generic `visit` wrapper -- a different "
             "assignment site for the same four properties.",
    ),
)


# ------------------------------------------------------------- the context option

def _padded(cid: str, before: int, after: int) -> str:
    """A source whose failing line sits `before` lines in and `after` lines from the end."""
    return (f".{cid}\n"
            + "".join(f"  p-{i}: {i}px\n" for i in range(1, before + 1))
            + "  width unit(1px, 3)\n"
            + "".join(f"  q-{i}: {i}px\n" for i in range(1, after + 1)))


#: The window is observable; the option that would resize it is not. `Renderer#render`
#: builds `var options = {}` in its catch and copies only input/filename/lineno/column
#: into it, so `formatException`'s `options.context` is always undefined and the window
#: is always the default 8. These cases measure the window's three observable shapes
#: plus the inertness itself.
_CONTEXT_CASES: tuple[ErrCase, ...] = (
    ErrCase(
        id="window-mid-file",
        source=_padded("window-mid-file", 9, 9),
        parts=("header", "gutter", "caret", "body", "stack"),
        note="neither bound clamps, so the full default window shows: `8` halved to "
             "4, and `slice` excluding its end index, giving four lines before the "
             "error and three after. A port reading the code as symmetric emits one "
             "extra trailing line.",
    ),
    ErrCase(
        id="window-clamps-at-file-start",
        source=_padded("window-clamps-at-file-start", 1, 9),
        parts=("header", "gutter", "caret", "body", "stack"),
        note="`start = max(lineno - 4, 1)` clamps, so the window is short at the top. "
             "The `('\\n' + str)` prefix is what makes line 1 land at index 1; a port "
             "that split the source directly shows one line too few here.",
    ),
    ErrCase(
        id="window-clamps-at-file-end",
        source=_padded("window-clamps-at-file-end", 9, 1),
        parts=("header", "gutter", "caret", "body", "stack"),
        note="`end = min(lines.length, lineno + 4)` clamps against the line count, "
             "which the trailing newline makes one larger than the visible lines.",
    ),
    ErrCase(
        id="window-whole-short-file",
        source=".window-whole-short-file\n  width unit(1px, 3)\n",
        parts=("header", "gutter", "caret", "body", "stack"),
        note="both bounds clamp at once: the entire file is inside the window.",
    ),
    ErrCase(
        id="context-option-is-inert",
        source=_padded("context-option-is-inert", 9, 9),
        settings={"context": 2},
        parts=("header", "gutter", "caret", "body", "stack"),
        note="`context: 2` must change nothing. The option looks live -- "
             "`formatException` reads `options.context` and honours it when called "
             "directly -- but `Renderer#render`'s catch builds a fresh `options` "
             "object and never copies it across, so no public route reaches it. A "
             "port that tidied this up by passing its own options through would "
             "produce a narrower window than upstream for every user who happens to "
             "have `context` set, which is why the inertness is asserted rather "
             "than assumed.",
    ),
)


# ---------------------------------------------------------------- degradation

_FALLBACK_CASES: tuple[ErrCase, ...] = (
    ErrCase(
        id="unreadable-file-falls-back",
        source=".unreadable-file-falls-back\n  width unit(1px, 3)\n",
        settings={"filename": f"{E}/not-on-disk-at-all.styl"},
        parts=("header", "gutter", "caret", "body", "stack"),
        note="the compiled source is passed in but the filename names nothing on "
             "disk, so `readFileSync` throws and the swallowing catch leaves "
             "`err.input` unset. `formatException` then falls back to the compiled "
             "string and the diagnostic is still complete. A port that let the read "
             "failure escape reports ENOENT instead of the real error; one that "
             "skipped the read entirely passes this and fails the import pair.",
    ),
)


_ALL: tuple[ErrCase, ...] = (
    _PARSE_CASES + _EVAL_CASES + _IMPORT_CASES + _CONTEXT_CASES + _FALLBACK_CASES
)


def cases() -> tuple[ErrCase, ...]:
    return _ALL


def ops() -> list[dict]:
    return [c.to_op() for c in _ALL]


@functools.lru_cache(maxsize=1)
def _by_id() -> dict[str, ErrCase]:
    return {c.id: c for c in _ALL}


def by_id(cid: str) -> ErrCase:
    return _by_id()[cid]


def ids() -> tuple[str, ...]:
    return tuple(c.id for c in _ALL)


def ids_with(part: str) -> tuple[str, ...]:
    """The cases declared to carry `part`, which is what the rows parametrise on."""
    return tuple(c.id for c in _ALL if part in c.parts)


def stack_ids() -> tuple[str, ...]:
    return ids_with("stack")


def import_ids() -> tuple[str, ...]:
    return tuple(c.id for c in _IMPORT_CASES)


def context_ids() -> tuple[str, ...]:
    return tuple(c.id for c in _CONTEXT_CASES)


@functools.lru_cache(maxsize=1)
def all_files() -> dict[str, str]:
    """Every case's tree at once: entry files plus their imports.

    One tree for the whole dimension costs one oracle process and one sandbox
    process. Collisions are an error rather than a silent overwrite -- two cases
    sharing a path would mean one of them compiles the other's text.
    """
    out: dict[str, str] = {}
    for c in _ALL:
        for path, text in ({c.entry: c.source} | c.files).items():
            if path in out and out[path] != text:
                raise AssertionError(f"error-case trees collide at {path}")
            out[path] = text
    return out


@functools.lru_cache(maxsize=1)
def scratch_root() -> Path:
    """Materialises the trees on disk for the oracle.

    `unreadable-file-falls-back` names a path deliberately absent from this tree,
    so nothing here creates it.
    """
    root = layout.WORK / "errdiag-tree"
    if root.exists():
        shutil.rmtree(root)
    for vpath, text in all_files().items():
        rel = vpath[len(E):].lstrip("/")
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf8")
    return root


def oracle_mounts() -> dict[str, str]:
    return {
        layout.VRUNTIME: str(layout.STATE_A / "lib" / "functions"),
        E: str(scratch_root()),
    }


@functools.lru_cache(maxsize=1)
def sandbox_files() -> dict[str, str]:
    out = {p: base64.b64encode(t.encode("utf8")).decode("ascii")
           for p, t in all_files().items()}
    runtime = vfs.find_runtime_root()
    if runtime is not None:
        out.update(vfs.runtime_files(runtime))
    return out


def describe(cid: str) -> str:
    c = by_id(cid)
    lines = [f"error case {cid!r}", f"    entry:    {c.entry}"]
    if c.paths:
        lines.append(f"    paths:    {list(c.paths)}")
    if c.settings:
        lines.append(f"    settings: {c.settings}")
    lines.append(f"    parts:    {list(c.parts)}")
    if c.note:
        lines.append(f"    why:      {c.note}")
    return "\n".join(lines)
