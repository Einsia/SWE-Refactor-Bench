"""Every render option upstream honours, paired with a source that reveals it.

An option only earns a check if turning it on changes the bytes.  Each entry here
was verified against pristine State A to produce output that differs from the same
case compiled without it -- otherwise a submission could ignore the option and
still pass, which measures nothing.

Two practical constraints shaped the table:

  * `linenos`, `firebug` and inline sourcemaps stat and read the source file and
    write its path into the CSS.  So every case names a *real* corpus file (the
    same file the sandbox has mounted in its VFS), and those entries set
    `revirt_css` so the oracle maps its own absolute prefix back to the virtual
    one before comparison.  Without that they would fail on the path, not the
    behaviour.

  * `include css` only bites on the quoted `@import 'x.css'` form; with the
    `url(...)` form the import is left alone either way.  The sources below use
    the form that discriminates.
"""
from __future__ import annotations

import functools
from dataclasses import dataclass, field

from . import layout


@dataclass(frozen=True)
class OptionCase:
    """One (options, source) pair, described once for both runners."""

    id: str
    #: A real file under `test/cases`, compiled with its real contents.
    case_file: str
    options: dict = field(default_factory=dict)
    #: Extra `renderer.set(k, v)` pairs beyond filename/paths.
    settings: dict = field(default_factory=dict)
    #: The option writes the source path into the CSS.
    revirt_css: bool = False
    #: What this entry is for, shown when it fails.
    note: str = ""

    @property
    def virtual_filename(self) -> str:
        return f"{layout.VPROJ}/test/cases/{self.case_file}"


#: The same `renderer.include()` arguments the corpus sweep uses.  `import.basic`
#: is on the list because several cases write `@import "a"` and reach
#: `test/cases/import.basic/a.styl` through it, not through `paths`.
INCLUDES: tuple[str, ...] = (
    f"{layout.VPROJ}/test/images",
    f"{layout.VPROJ}/test/cases/import.basic",
)


#: `dumb.styl` is small, uses a mixin and a loop, and exists on disk -- which the
#: debug-annotation options require.
_D = "dumb.styl"
_V = "variable.styl"

#: Absolute virtual paths used by the `imports` option.  Real files: `import.basic`
#: holds a.styl, b.styl, c.styl, clone.styl, clone2.styl.
_IMPORT_A = f"{layout.VPROJ}/test/cases/import.basic/a.styl"
_IMPORT_B = f"{layout.VPROJ}/test/cases/import.basic/b.styl"

CASES: tuple[OptionCase, ...] = (
    # --- baseline -----------------------------------------------------------
    OptionCase("plain", _D, {}, note="no options; the reference for every row below"),

    # --- whitespace and compression -----------------------------------------
    OptionCase("compress", _D, {"compress": True}, note="strips all optional whitespace"),
    OptionCase("indent-1", _D, {"indent spaces": 1}),
    OptionCase("indent-4", _D, {"indent spaces": 4}),
    OptionCase("indent-8", _D, {"indent spaces": 8}),
    # Deliberately equal to `compress` alone: that equality *is* the assertion.
    # A submission that let `indent spaces` win would emit indented output here.
    OptionCase("compress-beats-indent", _D, {"compress": True, "indent spaces": 8},
               note="compress wins; indent spaces must not reappear"),

    # --- debug annotations --------------------------------------------------
    # These three read the file and write its path into the output.
    OptionCase("linenos", _D, {"linenos": True}, revirt_css=True,
               note="/* line N : path */ before each rule"),
    OptionCase("firebug", _D, {"firebug": True}, revirt_css=True,
               note="@media -stylus-debug-info with the path escaped for font-family"),
    OptionCase("linenos+firebug", _D, {"linenos": True, "firebug": True}, revirt_css=True),
    OptionCase("linenos+compress", _D, {"linenos": True, "compress": True}, revirt_css=True,
               note="comments survive compression when linenos is on"),
    OptionCase("firebug+compress", _D, {"firebug": True, "compress": True}, revirt_css=True),
    OptionCase("linenos-on-import", "import.basic.styl", {"linenos": True}, revirt_css=True,
               note="each rule must be annotated with the file it came from, not the entry file"),

    # --- at-rule hoisting ---------------------------------------------------
    OptionCase("hoist", "hoist.at-rules.styl", {"hoist atrules": True},
               note="@charset and @import move to the top, in that order"),
    OptionCase("no-hoist", "hoist.at-rules.styl", {},
               note="the same file unhoisted; proves the option is what moved them"),
    OptionCase("hoist+compress", "hoist.at-rules.styl", {"hoist atrules": True, "compress": True}),

    # --- css imports --------------------------------------------------------
    # `import.include.basic.styl` is `@import "import.include._basic.css";` --
    # the quoted form, which is the only one the option affects.  Cases that
    # write `@import url(foo.css)` compile identically either way, so they would
    # award the check to a submission that ignored the option entirely.
    OptionCase("include-css", "import.include.basic.styl", {"include css": True},
               note="inlines the .css file instead of leaving the @import"),
    OptionCase("no-include-css", "import.include.basic.styl", {},
               note="the same import left alone; proves the option is what inlined it"),
    OptionCase("include-css-nested", "import.include.function/a.styl", {"include css": True},
               note="a .css import reached from inside a function body"),

    # --- selector prefixing -------------------------------------------------
    OptionCase("prefix", "prefix.extend.styl", {"prefix": "pfx-"},
               note="prefixes class selectors, including through @extend"),
    OptionCase("no-prefix", "prefix.extend.styl", {}),
    OptionCase("prefix-interpolation", "prefix.css.selector.interpolation.styl", {"prefix": "zz-"},
               note="prefixing has to survive selector interpolation"),

    # --- sourcemap comment in the CSS --------------------------------------
    # The map itself belongs to the sourcemaps dimension; these rows are about
    # what lands in the stylesheet.
    OptionCase("sourcemap", _D, {"sourcemap": True},
               note="appends /*# sourceMappingURL=... */"),
    OptionCase("sourcemap-basePath", _D, {"sourcemap": {"basePath": f"{layout.VPROJ}/test/cases"}},
               note="basePath shortens the URL in the comment"),
    OptionCase("sourcemap-inline", _D, {"sourcemap": {"inline": True}}, revirt_css=True,
               note="the whole map is base64'd into the comment"),
    OptionCase("sourcemap+compress", _D, {"sourcemap": True, "compress": True}),

    # --- injection ----------------------------------------------------------
    OptionCase("globals", _V, {"globals": {"injected": "7px"}},
               note="a global visible without any import"),
    OptionCase("imports-one", _V, {"imports": [_IMPORT_A]},
               note="auto-imported before the source, so its rules come first"),
    OptionCase("imports-two", _V, {"imports": [_IMPORT_A, _IMPORT_B]},
               note="order matters: a before b"),
    OptionCase("imports+compress", _V, {"imports": [_IMPORT_A], "compress": True}),
)

def op_for(c: OptionCase) -> dict:
    """One case in the shared op vocabulary, so both runners get the same job.

    `sourceFile` rather than inline text: the debug options read the file, and the
    sandbox has the identical bytes mounted at the same virtual path.
    """
    return {
        "id": c.id,
        "kind": "render",
        "sourceFile": c.virtual_filename,
        "set": {
            "filename": c.virtual_filename,
            "paths": [f"{layout.VPROJ}/test/cases"],
            **c.settings,
        },
        "include": list(INCLUDES),
        "options": dict(c.options),
        "revirtCss": c.revirt_css,
    }


def ops() -> list[dict]:
    return [op_for(c) for c in CASES]


@functools.lru_cache(maxsize=1)
def case_ids() -> tuple[str, ...]:
    return tuple(c.id for c in CASES)


#: Rows whose output is *expected* to equal another row's, with the reason.  The
#: suite still compares them against State A -- the point is that a reviewer
#: reading a collision report knows it was intended.
INTENDED_COLLISIONS: dict[str, str] = {
    "compress-beats-indent": "equals `compress`: that precedence is the assertion",
    "hoist+compress": "may equal `hoist` if the file has nothing to compress",
}


def by_id(cid: str) -> OptionCase:
    for c in CASES:
        if c.id == cid:
            return c
    raise KeyError(cid)


@functools.lru_cache(maxsize=1)
def describe() -> dict[str, str]:
    """`id -> human sentence`, used in failure messages."""
    out = {}
    for c in CASES:
        opts = ", ".join(f"{k}={v!r}" for k, v in sorted(c.options.items())) or "no options"
        out[c.id] = f"{c.case_file} with {opts}" + (f" -- {c.note}" if c.note else "")
    return out
