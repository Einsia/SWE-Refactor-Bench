"""Source maps: the artifact a build tool consumes, compared field by field.

`lib/visitor/sourcemapper.js` is a `Compiler` subclass, so it is core, and §1.1
keeps it inside `src/core/`.  That makes it one of the harder pieces of the port
for a reason that is not obvious from its size: it is the only visitor that reads
a file *during compilation for a purpose other than `@import`*.  Upstream fills
`sourcesContent` with `fs.readFileSync(node.filename, 'utf-8')`, so the port has
to reach `platform.readFile` from inside the compile stage -- on the async path,
from inside a synchronous visitor method.

Three measured behaviours shape this catalog, none of which a corpus comparison
would reveal:

**`sourcesContent` comes off disk, not from the source you compiled.**  Compile a
string in memory, point `filename` at a real file, and the embedded content is
that file's bytes -- unrelated to what was compiled.  Measured directly: for
`inline-content-is-from-disk` the map's `sourcesContent[0]` is not the op's own
source text.  A port would naturally fill it from the string it already holds,
which is the sensible thing and the wrong one.

**An inline map with a filename that does not resolve throws.**  The read is not
guarded, so the ENOENT surfaces wearing a Stylus-formatted message -- caret
diagram included -- while keeping `errno` and `syscall` from the underlying
failure.  `inline-missing-file` pins that.

**`dest` outranks `basePath`, and `inline` outranks `comment: false`.**
`normalizePath` is `relative(this.dest || this.basePath, path)`, and the comment
is emitted when `this.inline || false !== this.comment`.  Both are one-line
conditions that a reimplementation gets backwards without any test noticing,
because each option alone behaves correctly.

Two constraints shape the catalog rather than the behaviour it measures.

Every case's source is distinct.  See `harness/jsapi.py` for why: `Parser.cache`
is static and keyed on the source text, so two ops with identical bytes share a
parse and the second inherits the first's filename.  It is the reason this module
names a selector after each case id instead of reusing one convenient body.

And no case relies on `basePath` defaulting to `'.'`.  `normalizePath` calls
`relative(dest || basePath, path)`, and a relative first argument resolves against
the process's cwd -- which is the oracle tree for one runner, the repo root for
another, and `platform.cwd` inside the realm.  A row built on the default would
compare three different answers and fail every submission for the harness's
choice of working directory, so each case pins an absolute virtual `basePath` or
an absolute virtual `dest` instead.  `no-filename` is the exception that needs
neither: both sides of its `relative()` are cwd-relative, so they cancel.
"""
from __future__ import annotations

import functools
from dataclasses import dataclass, field

from . import layout

#: Fixtures that exist in the corpus and are mounted in the sandbox VFS.
SM_DIR = f"{layout.VPROJ}/test/sourcemap"
#: A virtual output directory for `dest`.  Never written to -- the sourcemapper
#: only computes paths against it -- but absolute, because a relative `dest`
#: would resolve against each runner's own cwd.
VBUILD = f"{layout.VPROJ}/build"
CASES_DIR = f"{layout.VPROJ}/test/cases"
IMAGES_DIR = f"{layout.VPROJ}/test/images"
IMPORT_BASIC = f"{CASES_DIR}/import.basic"

#: Upstream's own sourcemap fixtures, compiled from their real bytes.
BASIC_STYL = f"{SM_DIR}/basic.styl"
#: A corpus case with a multi-line `@css` literal, which drives the
#: `visitLiteral` mapping branch -- one mapping per line of the literal.
LITERAL_STYL = f"{CASES_DIR}/literal.styl"
#: Four sources in one map, so `sources` ordering and `sourcesContent` alignment
#: are both observable.
IMPORTS_STYL = f"{CASES_DIR}/import.basic.styl"


def _body(cid: str, prefix: str = "") -> str:
    """A distinct two-rule body, named after the case that compiles it."""
    return f"{prefix}.{cid}\n  color red\n  &:hover\n    color blue\n"


@dataclass(frozen=True)
class MapCase:
    """One `sourcemap` render, described independently of who runs it."""

    id: str
    #: `options.sourcemap`: `True`, or the option object.
    sourcemap: object
    #: Inline source.  Mutually exclusive with `source_file`.
    source: str = ""
    #: Compile a real fixture's bytes instead -- required whenever the map has to
    #: carry `sourcesContent`, which is read from the file rather than the string.
    source_file: str = ""
    #: `options.filename`.  Virtual; both runners translate it.
    filename: str = ""
    #: `options.dest`, which outranks `sourcemap.basePath` in `normalizePath`.
    dest: str = ""
    #: Extra `renderer.set()` pairs (`compress`, `linenos`, `include css`, ...).
    settings: dict = field(default_factory=dict)
    #: `renderer.include()` arguments, for cases whose imports must resolve.
    include: tuple[str, ...] = ()
    #: True when the map is expected to be absent (`sourcemap: false`).
    expect_no_map: bool = False
    #: True when State A refuses the input.
    expect_failure: bool = False
    #: A setting on this case writes a source path into the CSS as well as into
    #: the map.  `sources` is always translated back to virtual paths; the CSS is
    #: only translated for the cases that carry one, because for the rest the
    #: bytes are the assertion and rewriting them would weaken it.  See
    #: `harness/optionmatrix.py`, which carries the same flag for the same reason.
    revirt_css: bool = False
    note: str = ""

    def to_op(self) -> dict:
        options: dict = {"sourcemap": self.sourcemap}
        if self.filename:
            options["filename"] = self.filename
        if self.dest:
            options["dest"] = self.dest
        op = {"id": self.id, "kind": "sourcemap", "options": options}
        if self.revirt_css:
            op["revirtCss"] = True
        if self.source_file:
            op["sourceFile"] = self.source_file
        else:
            op["source"] = self.source
        if self.settings:
            op["set"] = dict(self.settings)
        if self.include:
            op["include"] = list(self.include)
        return op


# --------------------------------------------------------------- the map itself

_SHAPE_CASES: tuple[MapCase, ...] = (
    MapCase(
        "true-shorthand", True, source=_body("sm-true-shorthand"),
        filename=f"{SM_DIR}/basic.styl", dest=f"{VBUILD}/out.css",
        note="`sourcemap: true` is the documented shorthand.  It must behave as "
             "`{}` does -- same map, same URL -- so the pair with `empty-object` "
             "is the assertion, not either row alone.  Both carry `dest` because "
             "the shorthand leaves `basePath` at its default of '.', which every "
             "runner would resolve against its own cwd; see the module note on "
             "why no case here depends on that.",
    ),
    MapCase(
        "empty-object", {}, source=_body("sm-empty-object"),
        filename=f"{SM_DIR}/basic.styl", dest=f"{VBUILD}/out.css",
        note="`sourcemap: {}` -- every field defaulted.  Paired with "
             "`true-shorthand`, which must produce exactly this.",
    ),
    MapCase(
        "base-path", {"basePath": SM_DIR}, source=_body("sm-base-path"),
        filename=f"{SM_DIR}/basic.styl",
        note="`basePath` makes `sources` relative to it, so the entry becomes a "
             "bare 'basic.styl' rather than the whole virtual path.",
    ),
    MapCase(
        "source-root", {"sourceRoot": "/", "basePath": SM_DIR},
        source=_body("sm-source-root"), filename=f"{SM_DIR}/basic.styl",
        note="`sourceRoot` is a URL prefix a consumer joins onto each source, not "
             "a path -- it is passed through untouched and adds a key to the map "
             "that is absent otherwise.",
    ),
    MapCase(
        "source-root-url", {"sourceRoot": "http://example.com/src/", "basePath": SM_DIR},
        source=_body("sm-source-root-url"), filename=f"{SM_DIR}/basic.styl",
        note="The same field holding something that is unambiguously not a path. "
             "A port that ran `sourceRoot` through its path algebra would "
             "normalise the '//' away and break every URL form.",
    ),
    MapCase(
        "no-filename", True, source=_body("sm-no-filename"),
        note="No `filename` at all.  The Renderer's default is the bare string "
             "'stylus', so `file` is 'stylus.css' and `sources` is ['stylus'] -- "
             "measured, because a port defaulting to '<anonymous>' or '' would "
             "produce a map that still looks reasonable.",
    ),
    MapCase(
        "comment-false", {"comment": False, "basePath": SM_DIR},
        source=_body("sm-comment-false"), filename=f"{SM_DIR}/basic.styl",
        note="`comment: false` suppresses the trailing `/*# sourceMappingURL */` "
             "while still producing the map object.  The CSS is compared too, so "
             "a port that emitted it anyway fails on the CSS row.",
    ),
    MapCase(
        "compress", {"basePath": SM_DIR}, source=_body("sm-compress"),
        filename=f"{SM_DIR}/basic.styl", settings={"compress": True},
        note="Compressed output puts several rules on one line, so the generated "
             "columns advance without the line ever changing.  The mappings are a "
             "different shape from every other case here, and `move()`'s "
             "column arithmetic is what produces them.",
    ),
    MapCase(
        "linenos", {"basePath": SM_DIR}, source=_body("sm-linenos"),
        filename=f"{SM_DIR}/basic.styl", settings={"linenos": True},
        revirt_css=True,
        note="`linenos` injects a comment before each rule, which shifts every "
             "generated line.  Two features that both track position, composed.  "
             "The comment it injects names the file, including the built-in "
             "library's own `index.styl`, so this is the one case here whose CSS "
             "carries a path: hence `revirt_css`.",
    ),
    MapCase(
        "dest-is-css-file", True, source=_body("sm-dest-css"),
        filename=f"{SM_DIR}/basic.styl", dest=f"{VBUILD}/out.css",
        note="A `dest` ending in `.css` renames the map: `file` becomes 'out.css', "
             "the URL 'out.css.map', and `sources` is relative to 'build/' rather "
             "than to basePath.  Three consequences of two lines in the "
             "constructor.",
    ),
    MapCase(
        "dest-is-directory", True, source=_body("sm-dest-dir"),
        filename=f"{SM_DIR}/basic.styl", dest=VBUILD,
        note="A `dest` without a `.css` extension is treated as a directory, so "
             "`basename` still comes from `filename` -- the other half of the "
             "same branch.",
    ),
    MapCase(
        "dest-outranks-base-path", {"basePath": CASES_DIR},
        source=_body("sm-dest-outranks"), filename=f"{SM_DIR}/basic.styl",
        dest=f"{VBUILD}/out.css",
        note="Both set, and they disagree.  `normalizePath` is "
             "`relative(this.dest || this.basePath, path)`, so `dest` wins and "
             "`basePath` is dead.  A port that preferred basePath, or joined "
             "them, passes every single-option row and fails this one.",
    ),
    MapCase(
        "sourcemap-false", False, source=_body("sm-false"),
        filename=f"{SM_DIR}/basic.styl", expect_no_map=True,
        note="`sourcemap: false` must leave `renderer.sourcemap` unset and emit no "
             "comment -- the Renderer picks the plain Compiler.  The negative "
             "case: without it, a port that always built a map would pass every "
             "other row here.",
    ),
)

# ------------------------------------------------------------ real fixture bytes

_FIXTURE_CASES: tuple[MapCase, ...] = (
    MapCase(
        "fixture-basic", {"basePath": SM_DIR}, source_file=BASIC_STYL,
        filename=BASIC_STYL,
        note="Upstream's own sourcemap fixture, whose expected `.map` ships in the "
             "repo.  It exercises `@css`, `@media`, nesting and a variable in one "
             "file, so the mappings are long enough that an off-by-one in "
             "`move()` cannot hide.",
    ),
    MapCase(
        "fixture-literal", {"basePath": CASES_DIR}, source_file=LITERAL_STYL,
        filename=LITERAL_STYL,
        note="A multi-line `@css` literal.  `visitLiteral` adds one mapping per "
             "line, with the column taken from the line's own indentation and "
             "+2 when the literal is CSS.  That branch is unreachable from any "
             "single-line source.",
    ),
    MapCase(
        "fixture-imports", {"basePath": CASES_DIR}, source_file=IMPORTS_STYL,
        filename=IMPORTS_STYL, include=(IMAGES_DIR, IMPORT_BASIC),
        note="Four files in one map.  `sources` order follows first appearance "
             "during compilation, not import order or alphabetical order, and a "
             "consumer indexes `sourcesContent` by it.",
    ),
    MapCase(
        "fixture-imports-inline", {"inline": True, "basePath": CASES_DIR},
        source_file=IMPORTS_STYL, filename=IMPORTS_STYL,
        include=(IMAGES_DIR, IMPORT_BASIC),
        note="The same four sources with their contents embedded, so each "
             "imported file has to be read a second time -- once to compile, once "
             "for `sourcesContent` -- through `platform.readFile`.",
    ),
)

# ---------------------------------------------------------------- inline payload

_INLINE_CASES: tuple[MapCase, ...] = (
    MapCase(
        "inline-basic", {"inline": True, "basePath": SM_DIR},
        source_file=BASIC_STYL, filename=BASIC_STYL,
        note="The map becomes a base64 data URI in the CSS instead of a file "
             "reference.  Compared both ways: the CSS byte-for-byte, and the "
             "decoded payload field by field.",
    ),
    MapCase(
        "inline-charset-utf8", {"inline": True, "basePath": SM_DIR},
        source=f"@charset 'utf-8'\n{_body('sm-inline-utf8')}",
        filename=f"{SM_DIR}/basic.styl",
        note="`visitCharset` sets a flag that adds `charset=utf-8;` to the data "
             "URI.  So an `@charset` rule in the *stylesheet* changes the shape "
             "of the URI comment -- an interaction between two features that look "
             "unrelated.",
    ),
    MapCase(
        "inline-charset-other", {"inline": True, "basePath": SM_DIR},
        source=f"@charset 'iso-8859-1'\n{_body('sm-inline-latin1')}",
        filename=f"{SM_DIR}/basic.styl",
        note="The same rule with a different value.  Only 'utf-8' flips the flag, "
             "so this URI has no charset -- the pair is what makes the previous "
             "row an assertion about the comparison rather than about `@charset` "
             "being present at all.",
    ),
    MapCase(
        "inline-content-is-from-disk", {"inline": True, "basePath": SM_DIR},
        source=_body("sm-content-from-disk"), filename=BASIC_STYL,
        note="An in-memory source whose `filename` names a real, different file. "
             "`sourcesContent` is that file's bytes, not the compiled string: "
             "upstream reads `node.filename` off disk.  Measured, and the row "
             "most likely to fail on a port that filled the field from the "
             "source it was handed.",
    ),
    MapCase(
        "inline-overrides-comment-false",
        {"inline": True, "comment": False, "basePath": SM_DIR},
        source_file=BASIC_STYL, filename=BASIC_STYL,
        note="`comment: false` is ignored when `inline` is set -- the emit "
             "condition is `this.inline || false !== this.comment`.  Without "
             "the URI there is nowhere for an inline map to live, so suppressing "
             "it would discard the map entirely.",
    ),
    MapCase(
        "inline-with-source-root", {"inline": True, "sourceRoot": "http://x/", "basePath": SM_DIR},
        source_file=BASIC_STYL, filename=BASIC_STYL,
        note="`sourceRoot` survives into the embedded JSON, so the base64 payload "
             "has to be built after the field is set.",
    ),
    MapCase(
        "inline-compress", {"inline": True, "basePath": SM_DIR},
        source_file=BASIC_STYL, filename=BASIC_STYL, settings={"compress": True},
        note="Compressed CSS with an inline map: the data URI is appended to a "
             "single long line, and `move()` has counted columns across all of "
             "it.",
    ),
    MapCase(
        "inline-missing-file", {"inline": True, "basePath": SM_DIR},
        source=_body("sm-missing-file"),
        filename=f"{SM_DIR}/does-not-exist.styl", expect_failure=True,
        note="`sourcesContent` reads `node.filename` with no guard, so a filename "
             "that does not resolve turns a successful compile into a failure. "
             "The message is Stylus-formatted -- caret diagram and all -- over an "
             "ENOENT.  A port that skipped the missing file, or reported a bare "
             "IO error, differs from State A either way.",
    ),
)

# ------------------------------------------------------------ include-css bridge

_INCLUDE_CSS_CASES: tuple[MapCase, ...] = (
    MapCase(
        "include-css-inline", {"inline": True, "basePath": CASES_DIR},
        source_file=f"{CASES_DIR}/import.include.basic.styl",
        filename=f"{CASES_DIR}/import.include.basic.styl",
        settings={"include css": True}, include=(IMAGES_DIR, IMPORT_BASIC),
        note="With `include css`, the inlined CSS arrives as a Literal whose "
             "filename is the `.css` file, so the map's only source is the CSS "
             "and its `sourcesContent` is CSS text.  Measured: the `.styl` host "
             "does not appear at all.",
    ),
)


@functools.lru_cache(maxsize=1)
def cases() -> tuple[MapCase, ...]:
    return _SHAPE_CASES + _FIXTURE_CASES + _INLINE_CASES + _INCLUDE_CSS_CASES


@functools.lru_cache(maxsize=1)
def ops() -> tuple[dict, ...]:
    return tuple(c.to_op() for c in cases())


@functools.lru_cache(maxsize=1)
def _by_id() -> dict[str, MapCase]:
    return {c.id: c for c in cases()}


def by_id(cid: str) -> MapCase:
    return _by_id()[cid]


def ids() -> tuple[str, ...]:
    return tuple(c.id for c in cases())


def compiled_ids() -> tuple[str, ...]:
    """Cases State A compiles, which is every one but `inline-missing-file`.

    For the rows that compare something *in* the output -- the CSS, the comment
    that trails it, whether a map was exposed.  A case declared `expect_failure`
    has no output for those to be about, and it is a fact about State A rather than
    about a submission: the file `sourcesContent` tries to read is not there, and
    `cases()` says so at collection.

    So the row is not collected rather than collected and excused.  A skip is
    charged 0 rather than left out of the denominator, so excusing four rows on
    every submission would cost each of them four checks it could not answer; a
    module's size has to be the number of questions it can put, the same number
    for every submission.

    The case is not ungraded -- it is the one this dimension pins hardest.
    `test_failure_shape_of_a_missing_source` asks, per property, that it fails with
    State A's `.name`, `.message`, `.lineno`, `.column` and `.filename`, and
    `test_node_adapter_produces_the_same_css` asserts the adapter reproduces the
    same error rather than skipping it.
    """
    return tuple(c.id for c in cases() if not c.expect_failure)


def inline_ids() -> tuple[str, ...]:
    """Cases whose CSS embeds the map, so the payload can be decoded."""
    return tuple(
        c.id for c in cases()
        if isinstance(c.sourcemap, dict) and c.sourcemap.get("inline") and not c.expect_failure
    )


def mapped_ids() -> tuple[str, ...]:
    """Cases State A answers with a map -- everything but the negatives."""
    return tuple(c.id for c in cases() if not c.expect_no_map and not c.expect_failure)


def describe(cid: str) -> str:
    c = by_id(cid)
    bits = [f"sourcemap case {cid!r}"]
    bits.append(f"  sourcemap: {c.sourcemap!r}")
    if c.filename:
        bits.append(f"  filename:  {c.filename}")
    if c.dest:
        bits.append(f"  dest:      {c.dest}")
    if c.settings:
        bits.append(f"  set:       {c.settings!r}")
    if c.source_file:
        bits.append(f"  compiling: {c.source_file}")
    else:
        bits.append(f"  source:    {c.source!r}")
    if c.note:
        bits.append(f"  why:       {c.note}")
    return "\n".join(bits)
