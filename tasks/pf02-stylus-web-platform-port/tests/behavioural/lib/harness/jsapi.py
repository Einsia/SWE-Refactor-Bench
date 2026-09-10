"""The public JS surface, described once per behaviour rather than once per method.

This is the API `require('stylus')` hands an existing Node user, and instruction.md
1.5 says `src/node/` keeps it -- including the part that is hardest to keep once the
compiler underneath has become async: `render()` returning a String rather than a
Promise.  Everything here is asked of both State A and the submission's adapter, and
the answers are compared.

`.define` is the interesting half.  A plugin passes JavaScript functions in, and
State A calls them with *Stylus node objects* -- a `Unit` with `.val` and `.type`, an
`Ident`, a `String` whose `.string` is the text without quotes.  A port that swapped
those for plain JS values would compile every corpus case correctly and break every
plugin in the ecosystem, silently.  So the cases below make the defined function
report what it actually received, and that report is what gets compared.

The reporting trick, since only CSS crosses back: the defined function writes what it
saw into the value it returns, so the assertion reads a stylesheet.  Ugly, and the
alternative is a second serialisation protocol that would itself need testing.

One measured constraint shapes every source string below.  `Parser.cache` is a
*static* property -- one memo for the whole process -- keyed on `sha1(source +
prefix)`, and `MemoryCache.set` stamps the cached clone with whatever
`nodes.filename` happened to be at the time.  So two ops with byte-identical
source in one batch share a parse, and the second inherits the first's filename.
That was not a hypothesis: `set-linenos` passed alone and failed in the batch,
annotating `<oracle>/stylus` -- the default filename left behind by an earlier op
-- until every source here was made distinct.  A row whose answer depends on what
ran before it measures the batch, not the port, so `test_every_source_is_distinct`
holds the property in place.
"""
from __future__ import annotations

import functools
from dataclasses import dataclass, field

from . import layout

#: A `.styl` file that exists in the fixture corpus.  `linenos` and `firebug` call
#: `fs.realpathSync` on the source's filename, so those cases need a path that
#: really resolves -- on both sides, since the sandbox has the same tree mounted.
REAL_CASE = f"{layout.VPROJ}/test/cases/dumb.styl"

#: `include css` needs a real `.css` file to inline.  `import.include._basic.css`
#: is upstream's own two-line fixture for exactly this.
CSS_DIR = f"{layout.VPROJ}/test/cases"
CSS_FILE = "import.include._basic.css"
#: Any real path in that directory; it is the resolution anchor, not the source.
CSS_HOST = f"{layout.VPROJ}/test/cases/import.include.basic.styl"

#: `a.styl` imports `b`, which imports `c` -- upstream's transitive-import fixture.
IMPORT_BASIC = f"{layout.VPROJ}/test/cases/import.basic"


@dataclass(frozen=True)
class ApiCase:
    """One question about the API, asked identically of both sides."""

    id: str
    kind: str
    source: str = ""
    options: dict = field(default_factory=dict)
    settings: dict = field(default_factory=dict)
    #: `define(name, value)` with a plain value -- a global constant.
    define: dict = field(default_factory=dict)
    #: `define(name, fn)` where the value is JS source, compiled identically on
    #: both sides.  Separate from `define` because the two are different features:
    #: one makes a constant, the other installs a plugin.
    define_fn: dict = field(default_factory=dict)
    define_fn_raw: dict = field(default_factory=dict)
    include: tuple[str, ...] = ()
    css: str = ""
    note: str = ""
    #: Set when the output embeds a source path, so both sides re-virtualise first.
    revirt: bool = False

    def to_op(self) -> dict:
        op = {"id": self.id, "kind": self.kind}
        if self.kind == "convertCSS":
            op["css"] = self.css
            return op
        op["source"] = self.source
        if self.options:
            op["options"] = dict(self.options)
        if self.settings:
            op["set"] = dict(self.settings)
        if self.define:
            op["define"] = dict(self.define)
        if self.define_fn:
            op["defineFn"] = dict(self.define_fn)
        if self.define_fn_raw:
            op["defineFnRaw"] = dict(self.define_fn_raw)
        if self.include:
            op["include"] = list(self.include)
        if self.revirt:
            op["revirtCss"] = True
        return op


# --------------------------------------------------------------- define() probes
#
# Each of these defines a JS function under a name the stylesheet then calls.  What
# the function returns is a string that names what it was handed, so the CSS carries
# the answer.  `.type`, `.val`, `.string`, `.nodeName` are the properties a real
# plugin reads; a port that passes plain JS numbers would produce `undefined` here
# while compiling the corpus perfectly.

#: `typeof` plus the Stylus type tag, for whatever the first argument is.
_REPORT_TYPE = "function (v) { return typeof v + '/' + (v && v.nodeName) + '/' + (v && v.type); }"

_DEFINE_CASES: tuple[ApiCase, ...] = (
    ApiCase(
        "define-receives-unit", "render",
        source=".d-receives-unit\n  content: probe(42px)\n",
        define_fn={"probe": _REPORT_TYPE},
        note="a number literal arrives as a Unit node, not a JS number",
    ),
    ApiCase(
        "define-receives-string", "render",
        source=".d-receives-string\n  content: probe('hi')\n",
        define_fn={"probe": _REPORT_TYPE},
        note="and a quoted string as a String node",
    ),
    ApiCase(
        "define-receives-ident", "render",
        source=".d-receives-ident\n  content: probe(solid)\n",
        define_fn={"probe": _REPORT_TYPE},
        note="a bare word as an Ident",
    ),
    ApiCase(
        "define-receives-rgba", "render",
        source=".d-receives-rgba\n  content: probe(#fff)\n",
        define_fn={"probe": _REPORT_TYPE},
        note="a hex colour as an RGBA node",
    ),
    ApiCase(
        "define-unit-val-and-type", "render",
        source=".d-unit-val-and-type\n  content: probe(3em)\n",
        define_fn={"probe": "function (v) { return v.val + '|' + v.type; }"},
        note="`.val` is the number and `.type` the unit string -- the two "
             "properties every unit-manipulating plugin reads",
    ),
    ApiCase(
        "define-string-unquoted", "render",
        source=".d-string-unquoted\n  content: probe('quoted')\n",
        define_fn={"probe": "function (v) { return v.string; }"},
        note="`.string` is the text without its quotes",
    ),
    ApiCase(
        "define-returns-plain-number", "render",
        source=".d-returns-plain-number\n  width: probe()\n",
        define_fn={"probe": "function () { return 7; }"},
        note="a plain JS return value is coerced on the way back in",
    ),
    ApiCase(
        "define-returns-string", "render",
        source=".d-returns-string\n  content: probe()\n",
        define_fn={"probe": "function () { return 'plain'; }"},
    ),
    ApiCase(
        "define-arity-two", "render",
        source=".d-arity-two\n  width: add(2px, 3px)\n",
        define_fn={"add": "function (a, b) { return a.val + b.val; }"},
        note="two arguments, in order",
    ),
    ApiCase(
        "define-missing-arg-is-null", "render",
        source=".d-missing-arg-is-null\n  content: probe(1)\n",
        define_fn={"probe": "function (a, b) { return String(b === null || b === undefined); }"},
        note="an argument the stylesheet omitted",
    ),
    ApiCase(
        "define-constant-value", "render",
        source=".d-constant-value\n  width: $w\n",
        define={"$w": "12px"},
        note="define() with a non-function value makes a global constant",
    ),
    ApiCase(
        "define-raw-gets-nodes", "render",
        source=".d-raw-gets-nodes\n  content: probe(1, 2)\n",
        define_fn_raw={"probe": "function (args) { return String(args && args.nodeName); }"},
        note="the third argument to define() is `raw`: the function then receives "
             "one Expression holding every argument, rather than them spread",
    ),
    ApiCase(
        "define-called-once-per-use", "render",
        source=".d-called-once-per-use\n  content: probe()\n.d-called-once-per-use-2\n  content: probe()\n",
        define_fn={"probe": "(function () { var n = 0; return function () { return ++n; }; })()"},
        note="not memoised: two call sites, two calls",
    ),
    ApiCase(
        "define-overrides-builtin", "render",
        source=".d-overrides-builtin\n  width: unit(5px, 'em')\n",
        define_fn={"unit": "function () { return 'overridden'; }"},
        note="a user definition shadows the built-in of the same name",
    ),
    ApiCase(
        "define-throwing-function", "render",
        source=".d-throwing-function\n  content: probe()\n",
        define_fn={"probe": "function () { throw new Error('from the plugin'); }"},
        note="an exception from plugin code propagates out of render()",
    ),

    # --- the parts of the plugin API beyond the argument list ----------------
    ApiCase(
        "define-returns-node", "render",
        source=".d-returns-node\n  width: probe()\n",
        define_fn={"probe": "function () { return new this.renderer.nodes.Unit(9, 'px'); }"},
        note="`this` inside a defined function is the Evaluator and `this.renderer` "
             "is the Renderer, whose `.nodes` is the node constructors -- how a "
             "real plugin builds a typed return value instead of relying on "
             "coercion.  Measured, not assumed: `this.nodes` is undefined",
    ),
    ApiCase(
        "define-sees-renderer", "render",
        source=".d-sees-renderer\n  content: probe()\n",
        define_fn={"probe": "function () { return typeof this.renderer + '/' + typeof this.renderer.nodes; }"},
        note="the two hops that reach it, pinned separately from the use above",
    ),
    ApiCase(
        "define-reads-options", "render",
        source=".d-reads-options\n  content: probe()\n",
        define_fn={"probe": "function () { return String(this.options.compress); }"},
        options={"compress": False},
        note="and `this.options` is the render options, which plugins branch on",
    ),
    ApiCase(
        "define-uses-utils-unwrap", "render",
        source=".d-uses-utils\n  width: probe(4px)\n",
        define_fn={"probe": "function (v) { return v.nodes ? v.nodes.length : v.val; }"},
        note="whether a single argument arrives wrapped in an Expression",
    ),
    ApiCase(
        "define-mutates-argument", "render",
        source=".d-mutates-argument\n  width: probe(5px)\n",
        define_fn={"probe": "function (v) { v.val = v.val * 2; return v; }"},
        note="the node handed in is writable and the mutation survives",
    ),
    ApiCase(
        "define-list-argument", "render",
        source=".d-list-argument\n  content: probe(1 2 3)\n",
        define_fn={"probe": "function (v) { return v.nodeName + '/' + (v.nodes ? v.nodes.length : -1); }"},
        note="a space-separated list arrives as one Expression with three nodes",
    ),
    ApiCase(
        "define-boolean-return", "render",
        source=".d-boolean-return\n  content: probe()\n",
        define_fn={"probe": "function () { return true; }"},
        note="a JS boolean coerced back into a Stylus value",
    ),
    ApiCase(
        "define-null-return", "render",
        source=".d-null-return\n  content: probe()\n",
        define_fn={"probe": "function () { return null; }"},
        note="and null, which upstream turns into something specific",
    ),
    ApiCase(
        "define-undefined-return", "render",
        source=".d-undefined-return\n  content: probe()\n",
        define_fn={"probe": "function () { return undefined; }"},
        note="a function that returns nothing at all",
    ),
    ApiCase(
        "define-name-with-dash", "render",
        source=".d-name-with-dash\n  content: my-probe()\n",
        define_fn={"my-probe": "function () { return 'dashed'; }"},
        note="a dashed name, which is legal in Stylus and not in JS",
    ),
    ApiCase(
        "define-two-functions", "render",
        source=".d-two-functions\n  width: one()\n  height: two()\n",
        define_fn={"one": "function () { return 1; }", "two": "function () { return 2; }"},
        note="two definitions on one renderer",
    ),
    ApiCase(
        "define-called-from-mixin", "render",
        source="m()\n  width: probe()\n.d-called-from-mixin\n  m()\n",
        define_fn={"probe": "function () { return 3; }"},
        note="reached through a mixin rather than directly",
    ),
    ApiCase(
        "define-in-conditional", "render",
        source=".d-in-conditional\n  if probe()\n    color: red\n",
        define_fn={"probe": "function () { return true; }"},
        note="the return value used as a condition, which exercises truthiness",
    ),
    ApiCase(
        "define-in-interpolation", "render",
        source=".d-in-interpolation\n  {probe()}: 0\n",
        define_fn={"probe": "function () { return 'top'; }"},
        note="a defined function producing a property name",
    ),

    # --- globals --------------------------------------------------------------
    #
    # §1.5 names `globals`.  It is `define()`'s declarative twin: values injected
    # into the root scope through the options object rather than by a method call.
    ApiCase(
        "globals-string", "render",
        source=".g-string\n  content: $g\n",
        options={"globals": {"$g": "from-globals"}},
        note="a global injected through the options object",
    ),
    ApiCase(
        "globals-number", "render",
        source=".g-number\n  width: $n\n",
        options={"globals": {"$n": 3}},
    ),
    ApiCase(
        "globals-visible-in-mixin", "render",
        source="m()\n  width: $n\n.g-in-mixin\n  m()\n",
        options={"globals": {"$n": 4}},
        note="root scope, so a mixin body sees it",
    ),
    ApiCase(
        "globals-shadowed-locally", "render",
        source="$n = 9\n.g-shadowed\n  width: $n\n",
        options={"globals": {"$n": 4}},
        note="a stylesheet assignment of the same name: which one wins",
    ),
)


# ------------------------------------------------------------- the rest of the API

_CORE_CASES: tuple[ApiCase, ...] = (
    ApiCase("version", "version", note="stylus.version is the published string"),
    ApiCase("api-surface", "apiSurface",
            note="the exported names, sorted -- the shape a plugin destructures"),
    ApiCase("callable", "callable",
            note="`stylus(str)` itself, not just `stylus.render`"),
    ApiCase("middleware-present", "middlewarePresent",
            note="the connect middleware still exists on the Node side"),

    # --- render() and its variants ------------------------------------------
    #
    # Each selector is named after its case, which is what keeps the sources
    # distinct and so keeps the parse cache from crossing rows.  See the module
    # docstring: it is a real coupling, not a stylistic preference.
    ApiCase("render-sync-returns-string", "render", source=".render-sync\n  color: red\n",
            note="the crux of 1.5: a String, not a Promise"),
    ApiCase("render-top-level", "renderTopLevel", source=".render-top\n  color: red\n",
            note="stylus.render(str, opts) -- the one-shot form, which is its own "
                 "export and takes no renderer configuration"),
    ApiCase("render-callback", "renderCallback", source=".render-cb\n  color: red\n",
            note="render(cb), which upstream calls back before it returns"),
    ApiCase("render-empty-source", "render", source="",
            note="empty input is valid and produces empty output"),
    ApiCase("render-whitespace-only-source", "render", source="\n\n   \n",
            note="and so is input that is only whitespace"),
    ApiCase("render-with-compress", "render", source=".render-compress\n  color: red\n",
            options={"compress": True}),
    ApiCase("render-with-filename-option", "render", source=".render-filename\n  color: red\n",
            options={"filename": "given.styl"},
            note="filename is metadata on this path; nothing is read from disk"),

    # --- what render() does with input it cannot compile ---------------------
    #
    # These have to fail, and the row asserts *how*.  `.name`, `.lineno`,
    # `.column` and `.filename` are the properties every build tool reads off a
    # Stylus error to point at the offending line; a port that threw a bare
    # `Error` would still fail the compile and would still break every one of
    # them.  §1.5's `errors` clause is what these hold to.
    ApiCase("error-parse", "render", source=".error-parse\n  color: (\n",
            note="an unclosed paren -- ParseError, with a position"),
    ApiCase("error-bad-extend", "render",
            source=".error-extend\n  @extend .nothing-defined-anywhere\n",
            note="@extend of a selector that does not exist"),
    ApiCase("error-mixin-arity", "render",
            source="fn(a)\n  width: a\n.error-arity\n  fn()\n",
            note="a mixin called with too few arguments"),
    ApiCase("error-unit-arg", "render",
            source=".error-unit\n  width: unit(red, 'px')\n",
            note="a built-in given the wrong node type"),
    ApiCase("error-missing-import", "render",
            source="@import 'nope-not-a-real-file'\n.error-import\n  top: 0\n",
            note="an @import that resolves nowhere"),
    ApiCase("error-filename-reported", "render",
            source=".error-filename\n  color: (\n",
            settings={"filename": REAL_CASE},
            note="the same failure with a filename set: `.filename` names it, and "
                 "the message quotes the source around the fault"),
    ApiCase("error-in-callback", "renderCallback",
            source=".error-cb\n  color: (\n",
            note="render(cb) reports the failure through `err`, not by throwing -- "
                 "the two paths differ and both are contract"),

    # --- set() --------------------------------------------------------------
    ApiCase("set-compress", "render", source=".set-compress\n  color: red\n  margin: 0px\n",
            settings={"compress": True},
            note="set() reaches the same option as the constructor"),
    ApiCase("set-linenos", "render", source=".set-linenos\n  color: red\n",
            settings={"linenos": True, "filename": REAL_CASE}, revirt=True,
            note="writes the filename into the CSS, so the path is virtualised; "
                 "note the built-in library gets annotated too"),
    ApiCase("set-firebug", "render", source=".set-firebug\n  color: red\n",
            settings={"firebug": True, "filename": REAL_CASE}, revirt=True,
            note="the same path, escaped for a font-family value"),
    ApiCase("set-include-css", "render",
            source=f"@import '{CSS_FILE}'\n.set-include-css\n  color: red\n",
            settings={"include css": True, "filename": CSS_HOST,
                      "paths": [CSS_DIR]},
            note="'include css' -- a key with a space in it, and the only option "
                 "that makes an @import of a .css file be read rather than kept"),
    ApiCase("set-include-css-off", "render",
            source=f"@import '{CSS_FILE}'\n.set-include-css-off\n  color: red\n",
            settings={"filename": CSS_HOST, "paths": [CSS_DIR]},
            note="the same source without it: the @import survives into the CSS. "
                 "The pair is the assertion -- one row alone would pass on a port "
                 "that always inlined, or never did"),
    ApiCase("set-hoist-atrules", "render",
            source=".set-hoist\n  color: red\n@charset \"utf-8\"\n",
            settings={"hoist atrules": True}),
    ApiCase("set-prefix", "render", source=".set-prefix\n  color: red\n",
            settings={"prefix": "zz-"}),
    ApiCase("set-indent-spaces", "render",
            source=".set-indent\n  color: red\n  .b\n    top: 0\n",
            settings={"indent spaces": 4},
            note="an option the corpus never exercises"),
    ApiCase("set-two-settings", "render", source=".set-two\n  color: red\n",
            settings={"compress": True, "prefix": "p-"},
            note="two settings at once, both honoured"),
    ApiCase("set-unknown-key-is-inert", "render", source=".set-unknown\n  color: red\n",
            settings={"no such option": "whatever"},
            note="set() is a plain option write, so an unrecognised key is "
                 "accepted and changes nothing -- a port that validated keys "
                 "would throw here and break plugins that pass their own"),

    # --- include() and import resolution ------------------------------------
    ApiCase("include-path", "render", source="@import 'a'\n.include-path\n  top: 0\n",
            include=(IMPORT_BASIC,),
            note="include() adds a lookup path that @import then finds"),
    ApiCase("include-two-imports", "render",
            source="@import 'a'\n@import 'b'\n.include-two\n  top: 0\n",
            include=(IMPORT_BASIC,)),
    ApiCase("include-order-first-wins", "render",
            source="@import 'a'\n.include-order\n  top: 0\n",
            include=(IMPORT_BASIC, f"{layout.VPROJ}/test/cases"),
            note="two include paths that both resolve; which one is searched first"),
    ApiCase("paths-option", "render", source="@import 'a'\n.paths-option\n  top: 0\n",
            options={"paths": [IMPORT_BASIC]},
            note="the `paths` option, which include() appends to"),
    ApiCase("include-after-paths", "render",
            source="@import 'a'\n.include-after-paths\n  top: 0\n",
            options={"paths": [f"{layout.VPROJ}/test/cases"]},
            include=(IMPORT_BASIC,),
            note="both at once: the resolution order between them is observable"),

    # --- deps() -------------------------------------------------------------
    ApiCase("deps-one-import", "deps", source="@import 'a'\n.deps-one\n  top: 0\n",
            include=(IMPORT_BASIC,),
            note="the resolved absolute path of each import"),
    ApiCase("deps-none", "deps", source=".deps-none\n  color: red\n",
            note="an empty list, not null"),
    ApiCase("deps-transitive", "deps", source="@import 'c'\n.deps-transitive\n  top: 0\n",
            include=(IMPORT_BASIC,),
            note="whether deps() recurses or reports only what this sheet named"),
    ApiCase("deps-duplicate-import", "deps",
            source="@import 'a'\n@import 'a'\n.deps-dup\n  top: 0\n",
            include=(IMPORT_BASIC,),
            note="the same file twice: is it listed once or twice?"),
    ApiCase("deps-missing-import", "deps",
            source="@import 'nope-does-not-exist'\n.deps-missing\n  top: 0\n",
            include=(IMPORT_BASIC,),
            note="deps() on an unresolvable import -- upstream's answer here is "
                 "whatever it is, and the port has to give the same one"),

    # --- convertCSS() -------------------------------------------------------
    ApiCase("convert-simple", "convertCSS", css=".a { color: red; }\n"),
    ApiCase("convert-nested-media", "convertCSS",
            css="@media screen {\n  .a { color: red }\n}\n"),
    ApiCase("convert-multiple-selectors", "convertCSS",
            css=".a, .b, .c { color: red; margin: 0 }\n"),
    ApiCase("convert-comment", "convertCSS", css="/* keep me */\n.a { color: red }\n"),
    ApiCase("convert-empty", "convertCSS", css=""),
    ApiCase("convert-atrule", "convertCSS",
            css="@font-face { font-family: x; src: url(a.woff) }\n"),
)


@functools.lru_cache(maxsize=1)
def cases() -> tuple[ApiCase, ...]:
    all_cases = _CORE_CASES + _DEFINE_CASES
    seen = set()
    for c in all_cases:
        if c.id in seen:
            raise AssertionError(f"duplicate ApiCase id {c.id!r}")
        seen.add(c.id)
    return all_cases


@functools.lru_cache(maxsize=1)
def ops() -> list[dict]:
    return [c.to_op() for c in cases()]


@functools.lru_cache(maxsize=1)
def case_ids() -> list[str]:
    return [c.id for c in cases()]


def by_id(cid: str) -> ApiCase:
    for c in cases():
        if c.id == cid:
            return c
    raise KeyError(cid)


@functools.lru_cache(maxsize=1)
def describe() -> dict[str, str]:
    out = {}
    for c in cases():
        bits = [c.kind]
        if c.define:
            bits.append(f"define({', '.join(c.define)}) as a constant")
        if c.define_fn:
            bits.append(f"define({', '.join(c.define_fn)}) as a function")
        if c.define_fn_raw:
            bits.append(f"define({', '.join(c.define_fn_raw)}, raw)")
        if c.settings:
            bits.append(f"set({', '.join(c.settings)})")
        if c.options:
            bits.append(f"options({', '.join(c.options)})")
        if c.include:
            bits.append(f"include x{len(c.include)}")
        line = " ".join(bits)
        if c.note:
            line += f"   -- {c.note}"
        out[c.id] = line
    return out


#: Ids whose result is a CSS string, grouped for the comparison sweep.
def css_ids() -> list[str]:
    return [c.id for c in cases() if c.kind in ("render", "renderTopLevel", "renderCallback")]


def deps_ids() -> list[str]:
    return [c.id for c in cases() if c.kind == "deps"]


def convert_ids() -> list[str]:
    return [c.id for c in cases() if c.kind == "convertCSS"]
