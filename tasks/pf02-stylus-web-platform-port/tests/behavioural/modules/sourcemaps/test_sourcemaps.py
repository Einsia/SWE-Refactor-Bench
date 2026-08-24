"""The source map as an artifact, compared field by field.

`lib/visitor/sourcemapper.js` is a `Compiler` subclass, so §1.1 puts it inside
`src/core/` and the sandboxed realm is where it gets measured.  A small parity set
runs the same cases through `src/node/` as well, because §1.5 lists
`options.sourcemap` among the options the adapter must keep working.

**Absence is compared, not skipped.**  A map with no `sourceRoot` key is a
different artifact from one whose `sourceRoot` is null, and a non-inline map that
embedded `sourcesContent` would put every source file into every build.  So the
field rows assert `None` against `None` where State A omits a key.  That is the
opposite of the choice `test_jsapi.py` makes for error properties, and for a
reason: an error object that gained a helpful `.filename` is not worse, while a
serialized map that gained a key no consumer asked for is.

**The mappings string is one row.**  It is VLQ-encoded relative deltas, so a
single off-by-one in `move()`'s column arithmetic corrupts every segment after
it.  There is nothing to decompose inside it -- either the port's position
tracking agrees with State A's or the artifact is unusable -- and splitting it
into per-segment rows would report a port that got the first line right as
mostly working.
"""
from __future__ import annotations

import base64
import json

import pytest

from harness import layout, runners, sourcemaps
from srbstylus import require_core

#: Every key State A ever puts in a map, so a row exists for each whether the
#: case produces it or not.  Declared rather than read off the oracle: the row
#: count has to be the same whatever happens at verify time.
MAP_FIELDS: tuple[str, ...] = (
    "version", "file", "sources", "sourceRoot", "names", "mappings", "sourcesContent",
)

#: Cases whose CSS carries no `sourceMappingURL` comment.
NO_COMMENT_IDS: tuple[str, ...] = ("comment-false", "sourcemap-false")

#: The subset that also runs through `src/node/`.  Not the whole catalog: the
#: adapter's job is to pass options through to the same core, so a handful of
#: shapes -- plain, inline, dest-driven, and the failure -- covers what could
#: break there without paying twice for every field of every case.
NODE_PARITY_IDS: tuple[str, ...] = (
    "base-path", "source-root", "dest-is-css-file", "fixture-basic",
    "inline-basic", "inline-charset-utf8", "sourcemap-false", "inline-missing-file",
)


# --------------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def expected():
    return runners.oracle(sourcemaps.ops())


@pytest.fixture(scope="module")
def actual():
    require_core()
    return runners.sandbox(sourcemaps.ops())


@pytest.fixture(scope="module")
def node_actual():
    #: Whichever entry point the tree's shape makes the Node-facing one; see
    #: `layout.face()`.  `options.sourcemap` is asked of it either way.
    if not layout.node_face_available():
        return None
    return runners.node_adapter(sourcemaps.ops())


def _pair(expected, actual, cid: str):
    """Both sides' results, with a missing one reported as itself.

    `Batch.get` synthesises a failing Result for an id it does not hold, which
    would otherwise surface as an ordinary comparison failure and send the reader
    looking at the port's output for a fault that is in the driver.
    """
    assert cid in expected.results, (
        f"harness: the oracle returned no result for {cid}\n"
        f"driver stderr:\n{expected.driver_stderr[-1200:]}"
    )
    if cid not in actual.results:
        pytest.fail(
            f"{sourcemaps.describe(cid)}\nsrc/core returned no result for this op.\n"
            f"driver stderr:\n{actual.driver_stderr[-1200:]}"
        )
    return expected.get(cid), actual.get(cid)


def _require_compiled(exp, got, cid: str) -> None:
    """Both sides compiled, with the two failures reported as different things.

    Without this, a port that throws leaves `got.value` at None and the row dies
    dereferencing it -- an AttributeError inside the harness, for what is really
    the port declining input State A accepts. The distinction matters when reading
    a scored run: one line is the submission's fault, the other is ours.
    """
    assert exp.ok, (
        f"harness: State A cannot compile {cid}: "
        f"{(exp.error or {}).get('name')}: {(exp.error or {}).get('message', '')[:400]}"
    )
    assert got.ok, (
        f"{sourcemaps.describe(cid)}\nsrc/core could not compile it:\n"
        f"  {(got.error or {}).get('name')}: {(got.error or {}).get('message', '')[:600]}"
    )


def _map_of(result, cid: str, side: str):
    assert result.ok, (
        f"{sourcemaps.describe(cid)}\n{side} failed to compile it: "
        f"{(result.error or {}).get('name')}: {(result.error or {}).get('message', '')[:500]}"
    )
    return result.value.get("sourcemap")


def _url_from(css: str) -> str | None:
    """The `sourceMappingURL` the CSS advertises, or None if it has no comment."""
    marker = "sourceMappingURL="
    if marker not in css:
        return None
    return css.split(marker, 1)[1].split(" */", 1)[0]


def _decode_inline(url: str) -> dict:
    payload = url.split("base64,", 1)[1]
    return json.loads(base64.b64decode(payload).decode("utf8"))


# ------------------------------------------------------------------ the CSS side

@pytest.mark.parametrize("cid", sourcemaps.compiled_ids())
def test_css_matches_state_a(cid, expected, actual):
    """The stylesheet, including whatever trailing comment the map produced.

    Compared before any field of the map is: the comment is what a browser and
    every bundler actually follow, and for an inline map it carries the entire
    payload, so this row alone would catch a wrong `charset=utf-8;` or a missing
    `.map` suffix.

    Collected over the cases State A compiles; `inline-missing-file` produces no
    CSS to compare, and is graded on how it fails instead.
    """
    exp, got = _pair(expected, actual, cid)
    assert exp.ok, f"harness: State A cannot compile {cid}"
    assert got.ok, (
        f"{sourcemaps.describe(cid)}\nsrc/core could not compile it:\n"
        f"  {(got.error or {}).get('name')}: {(got.error or {}).get('message', '')[:600]}"
    )
    assert got.value.get("css") == exp.value.get("css"), (
        f"{sourcemaps.describe(cid)}\nthe CSS differs:\n"
        f"  state-a:  {(exp.value.get('css') or '')[-300:]!r}\n"
        f"  src/core: {(got.value.get('css') or '')[-300:]!r}"
    )


@pytest.mark.parametrize("cid", sourcemaps.compiled_ids())
def test_comment_presence_matches_state_a(cid, expected, actual):
    """Whether a `sourceMappingURL` comment is emitted at all.

    Split from the CSS row so that `comment: false` and
    `inline-overrides-comment-false` -- the two halves of one condition -- are
    each their own row.  A port that got the condition backwards fails one of
    them regardless of what its map contains.
    """
    exp, got = _pair(expected, actual, cid)
    _require_compiled(exp, got, cid)
    want = _url_from(exp.value.get("css") or "")
    have = _url_from(got.value.get("css") or "")
    assert (want is None) == (have is None), (
        f"{sourcemaps.describe(cid)}\n"
        f"  state-a:  {'no comment' if want is None else 'comment emitted'}\n"
        f"  src/core: {'no comment' if have is None else 'comment emitted'}"
    )


# ------------------------------------------------------------------ the map side

@pytest.mark.parametrize("cid", sourcemaps.compiled_ids())
def test_map_presence_matches_state_a(cid, expected, actual):
    """`renderer.sourcemap` is set exactly when State A sets it.

    `sourcemap: false` must leave it unset: the Renderer picks the plain Compiler
    and there is no map to expose.  Without this row a port that always built one
    would pass every other row in the dimension.
    """
    exp, got = _pair(expected, actual, cid)
    _require_compiled(exp, got, cid)
    want, have = exp.value.get("sourcemap"), got.value.get("sourcemap")
    assert (want is None) == (have is None), (
        f"{sourcemaps.describe(cid)}\n"
        f"  state-a:  {'no map' if want is None else 'map present'}\n"
        f"  src/core: {'no map' if have is None else 'map present'}"
    )


@pytest.mark.parametrize("cid", sourcemaps.mapped_ids())
@pytest.mark.parametrize("field", MAP_FIELDS)
def test_map_field_matches_state_a(cid, field, expected, actual):
    """One row per (case, field), absence included.

    See the module docstring: a key State A omits must stay omitted, because the
    map is a serialized artifact and an extra key changes what a consumer reads.
    """
    exp, got = _pair(expected, actual, cid)
    want_map = _map_of(exp, cid, "state-a")
    have_map = _map_of(got, cid, "src/core")
    assert want_map is not None, f"harness: {cid} produced no map in State A"
    assert have_map is not None, (
        f"{sourcemaps.describe(cid)}\nsrc/core exposed no `renderer.sourcemap`."
    )
    want, have = want_map.get(field), have_map.get(field)
    if field == "mappings":
        assert have == want, (
            f"{sourcemaps.describe(cid)}\nthe VLQ mappings differ. These are "
            "relative deltas, so the first wrong segment invalidates the rest:\n"
            f"  state-a:  {want!r}\n"
            f"  src/core: {have!r}"
        )
        return
    assert have == want, (
        f"{sourcemaps.describe(cid)}\nmap field `{field}` differs:\n"
        f"  state-a:  {want!r}\n"
        f"  src/core: {have!r}"
    )


# --------------------------------------------------------------- inline payload

@pytest.mark.parametrize("cid", sourcemaps.inline_ids())
def test_inline_uri_prefix_matches_state_a(cid, expected, actual):
    """The data URI's media type and parameters, before the payload.

    `@charset 'utf-8'` in the stylesheet adds `charset=utf-8;` here and nothing
    else does -- an interaction between two features that look unrelated, which
    is why `inline-charset-utf8` and `inline-charset-other` are a pair.
    """
    exp, got = _pair(expected, actual, cid)
    _require_compiled(exp, got, cid)
    want = _url_from(exp.value.get("css") or "") or ""
    have = _url_from(got.value.get("css") or "") or ""
    want_prefix = want.split("base64,", 1)[0] + "base64,"
    have_prefix = have.split("base64,", 1)[0] + "base64," if "base64," in have else have[:80]
    assert have_prefix == want_prefix, (
        f"{sourcemaps.describe(cid)}\nthe data URI prefix differs:\n"
        f"  state-a:  {want_prefix!r}\n"
        f"  src/core: {have_prefix!r}"
    )


@pytest.mark.parametrize("cid", sourcemaps.inline_ids())
def test_inline_payload_decodes_to_the_same_map(cid, expected, actual):
    """The embedded base64, decoded and compared as JSON.

    Not redundant with the CSS row: that one would fail on any difference at all,
    including base64 line-wrapping or key order, and report a 4 KB blob. This
    reports which field of the decoded map is wrong.
    """
    exp, got = _pair(expected, actual, cid)
    _require_compiled(exp, got, cid)
    want_url = _url_from(exp.value.get("css") or "") or ""
    have_url = _url_from(got.value.get("css") or "") or ""
    assert "base64," in have_url, (
        f"{sourcemaps.describe(cid)}\nsrc/core emitted no base64 data URI:\n"
        f"  {have_url[:200]!r}"
    )
    want, have = _decode_inline(want_url), _decode_inline(have_url)
    differing = sorted(
        k for k in set(want) | set(have) if want.get(k) != have.get(k)
    )
    assert not differing, (
        f"{sourcemaps.describe(cid)}\nthe decoded payload differs in {differing}:\n"
        + "\n".join(
            f"  {k}:\n    state-a:  {str(want.get(k))[:200]}\n    src/core: {str(have.get(k))[:200]}"
            for k in differing[:4]
        )
    )


@pytest.mark.parametrize("cid", sourcemaps.inline_ids())
def test_inline_payload_equals_the_exposed_map(cid, expected, actual):
    """The embedded copy and `renderer.sourcemap` must be the same map.

    Upstream builds the URI from the same generator it exposes, so they cannot
    disagree. A port that assembled the inline JSON separately -- easy to do when
    the base64 step is bolted on afterwards -- could pass both of the rows above
    while shipping a stylesheet whose embedded map contradicts its API.
    """
    exp, got = _pair(expected, actual, cid)
    _require_compiled(exp, got, cid)
    have_url = _url_from(got.value.get("css") or "") or ""
    assert "base64," in have_url, f"{sourcemaps.describe(cid)}\nno base64 payload to compare"
    embedded = _decode_inline(have_url)
    exposed = got.value.get("sourcemap")
    assert exposed is not None, (
        f"{sourcemaps.describe(cid)}\nsrc/core embedded a map but exposed none on "
        "`renderer.sourcemap`."
    )
    assert embedded == exposed, (
        f"{sourcemaps.describe(cid)}\nthe embedded map and the exposed one differ:\n"
        f"  embedded keys: {sorted(embedded)}\n  exposed keys:  {sorted(exposed)}"
    )


# ---------------------------------------------------------------- the failure

@pytest.mark.parametrize("prop", ["name", "message"])
def test_missing_source_file_failure_matches_state_a(prop, expected, actual):
    """`inline` + a filename that does not resolve.

    `sourcesContent` reads `node.filename` with no guard, so a successful compile
    becomes a failure -- and the message is Stylus-formatted, caret diagram and
    all, over an underlying ENOENT. A port that skipped the unreadable file would
    produce CSS where State A produces an error; one that let a bare IO error
    escape would produce the wrong message.
    """
    cid = "inline-missing-file"
    exp, got = _pair(expected, actual, cid)
    assert not exp.ok, f"harness: State A now compiles {cid}"
    assert not got.ok, (
        f"{sourcemaps.describe(cid)}\nsrc/core compiled this successfully; State A "
        f"fails it with {(exp.error or {}).get('name')}."
    )
    assert (got.error or {}).get(prop) == (exp.error or {}).get(prop), (
        f"{sourcemaps.describe(cid)}\nthe error's `.{prop}` differs:\n"
        f"  state-a:  {(exp.error or {}).get(prop)!r}\n"
        f"  src/core: {(got.error or {}).get(prop)!r}"
    )


# ------------------------------------------------------------- node adapter parity

@pytest.mark.parametrize("cid", NODE_PARITY_IDS)
def test_node_adapter_produces_the_same_css(cid, expected, node_actual):
    """§1.5 lists `options.sourcemap` among the options the adapter keeps.

    The adapter drives the same core, so this is not a second implementation to
    check -- it is a check that nothing is lost in the handoff, which is where a
    port that resolves `dest` in the wrong layer would show up.
    """
    assert node_actual is not None, (
        f"src/node/index.js does not exist at {layout.NODE_ENTRY}; §1.5 requires it."
    )
    if node_actual.load_error:
        pytest.fail(
            "src/node/index.js could not be imported: "
            f"{node_actual.load_error.get('name')}: "
            f"{node_actual.load_error.get('message', '')[:400]}"
        )
    exp, got = expected.get(cid), node_actual.get(cid)
    assert got is not None, f"src/node returned no result for {cid}"
    if sourcemaps.by_id(cid).expect_failure:
        assert not got.ok, (
            f"{sourcemaps.describe(cid)}\nsrc/node compiled input State A rejects."
        )
        assert (got.error or {}).get("message") == (exp.error or {}).get("message"), (
            f"{sourcemaps.describe(cid)}\nthe adapter's error message differs:\n"
            f"  state-a:  {(exp.error or {}).get('message', '')[:400]!r}\n"
            f"  src/node: {(got.error or {}).get('message', '')[:400]!r}"
        )
        return
    assert exp.ok, f"harness: State A cannot compile {cid}: {exp.error}"
    assert got.ok, (
        f"{sourcemaps.describe(cid)}\nsrc/node could not compile it:\n"
        f"  {(got.error or {}).get('name')}: {(got.error or {}).get('message', '')[:500]}"
    )
    assert got.value.get("css") == exp.value.get("css"), (
        f"{sourcemaps.describe(cid)}\nthe adapter's CSS differs from State A's:\n"
        f"  state-a:  {(exp.value.get('css') or '')[-300:]!r}\n"
        f"  src/node: {(got.value.get('css') or '')[-300:]!r}"
    )


@pytest.mark.parametrize("cid", [c for c in NODE_PARITY_IDS
                                if not sourcemaps.by_id(c).expect_failure])
def test_node_adapter_exposes_the_same_map(cid, expected, node_actual):
    """Collected over the parity cases State A compiles.

    `inline-missing-file` is in `NODE_PARITY_IDS` for the row above, which asserts
    the adapter reproduces State A's error rather than compiling it; there is no
    map on that path for this row to expose, so it is not asked.
    """
    assert node_actual is not None, f"src/node/index.js does not exist at {layout.NODE_ENTRY}"
    if node_actual.load_error:
        pytest.fail(f"src/node/index.js could not be imported: {node_actual.load_error}")
    exp, got = expected.get(cid), node_actual.get(cid)
    assert got is not None, f"src/node returned no result for {cid}"
    assert exp.ok, f"harness: State A cannot compile {cid}: {exp.error}"
    assert got.ok, (
        f"{sourcemaps.describe(cid)}\nsrc/node could not compile it:\n"
        f"  {(got.error or {}).get('name')}: {(got.error or {}).get('message', '')[:500]}"
    )
    want, have = exp.value.get("sourcemap"), got.value.get("sourcemap")
    assert have == want, (
        f"{sourcemaps.describe(cid)}\nthe adapter's map differs from State A's:\n"
        f"  state-a:  {json.dumps(want)[:400]}\n"
        f"  src/node: {json.dumps(have)[:400]}"
    )


# ------------------------------------------------------ harness self-checks
#
# Oracle-only. They earn no credit -- pytest.ini deselects the `harness` marker
# from the graded run -- but a failure voids the run, because each is a premise
# the rows above depend on.

@pytest.mark.harness
def test_oracle_answered_every_case(expected):
    missing = [c.id for c in sourcemaps.cases() if c.id not in expected.results]
    assert not missing, (
        f"the oracle returned no result for {missing}\n"
        f"driver stderr:\n{expected.driver_stderr[-2000:]}"
    )


@pytest.mark.harness
def test_declared_mapped_cases_really_produce_maps(expected):
    """`mapped_ids()` drives the field rows, so it must still be accurate.

    If a case stopped producing a map, its seven field rows would assert against
    nothing and fail every submission for the catalog's mistake.
    """
    wrong = [
        cid for cid in sourcemaps.mapped_ids()
        if not expected.get(cid).ok or expected.get(cid).value.get("sourcemap") is None
    ]
    assert not wrong, f"declared as mapped but State A produces no map: {wrong}"


@pytest.mark.harness
def test_declared_negatives_really_have_no_map(expected):
    """The converse, for `sourcemap: false`."""
    r = expected.get("sourcemap-false")
    assert r.ok, f"harness: State A cannot compile sourcemap-false: {r.error}"
    assert r.value.get("sourcemap") is None, (
        "State A now exposes a map for `sourcemap: false`, so "
        "test_map_presence_matches_state_a is asserting the wrong direction."
    )


@pytest.mark.harness
def test_declared_inline_cases_really_embed_base64(expected):
    wrong = []
    for cid in sourcemaps.inline_ids():
        url = _url_from(expected.get(cid).value.get("css") or "")
        if not url or "base64," not in url:
            wrong.append(cid)
    assert not wrong, (
        f"declared inline but State A emits no base64 data URI: {wrong}. "
        "The payload rows would have nothing to decode."
    )


@pytest.mark.harness
def test_declared_no_comment_cases_really_have_none(expected):
    for cid in NO_COMMENT_IDS:
        css = expected.get(cid).value.get("css") or ""
        assert "sourceMappingURL" not in css, (
            f"{cid} is declared as emitting no sourceMappingURL, but State A's CSS "
            f"contains one: {css[-120:]!r}"
        )


@pytest.mark.harness
def test_charset_pair_really_differs(expected):
    """`@charset 'utf-8'` must change the URI and `iso-8859-1` must not.

    Both cases would pass on a port that never emitted the parameter if the pair
    were not asserted to differ in State A first.
    """
    utf8 = _url_from(expected.get("inline-charset-utf8").value.get("css") or "") or ""
    other = _url_from(expected.get("inline-charset-other").value.get("css") or "") or ""
    assert "charset=utf-8;" in utf8, (
        f"State A no longer adds charset=utf-8 for an @charset 'utf-8' rule: {utf8[:120]!r}"
    )
    assert "charset=utf-8;" not in other, (
        f"State A adds charset=utf-8 for @charset 'iso-8859-1', which the pair "
        f"assumes it does not: {other[:120]!r}"
    )


@pytest.mark.harness
def test_dest_really_outranks_base_path(expected):
    """The two options must genuinely disagree in State A.

    `dest-outranks-base-path` sets both to different directories. If `sources`
    came out the same either way, the row would be measuring nothing.
    """
    both = expected.get("dest-outranks-base-path").value["sourcemap"]["sources"]
    base_only = expected.get("base-path").value["sourcemap"]["sources"]
    assert both != base_only, (
        "`dest` and `basePath` now produce the same `sources`, so the row cannot "
        f"tell which one won: {both!r}"
    )


@pytest.mark.harness
def test_content_from_disk_case_really_differs_from_its_source(expected):
    """The premise of `inline-content-is-from-disk`.

    The case only measures anything if State A's `sourcesContent` is the file's
    bytes rather than the string compiled. If upstream ever started using the
    in-memory source, this row says so instead of the suite quietly asserting a
    behaviour that no longer exists.
    """
    cid = "inline-content-is-from-disk"
    content = expected.get(cid).value["sourcemap"]["sourcesContent"][0]
    compiled = sourcemaps.by_id(cid).source
    assert content != compiled, (
        "State A's sourcesContent now matches the compiled string, so this case no "
        "longer distinguishes a port that fills the field from its input."
    )


@pytest.mark.harness
def test_multi_source_case_really_has_several_sources(expected):
    sources = expected.get("fixture-imports").value["sourcemap"]["sources"]
    assert len(sources) >= 3, (
        f"fixture-imports was chosen for having several sources; it now has {sources!r}. "
        "The ordering and sourcesContent-alignment rows need more than one."
    )


@pytest.mark.harness
def test_source_content_aligns_with_sources(expected):
    """`sourcesContent[i]` describes `sources[i]`, which the field rows rely on."""
    m = expected.get("fixture-imports-inline").value["sourcemap"]
    assert len(m["sourcesContent"]) == len(m["sources"]), (
        f"State A's map has {len(m['sources'])} sources but "
        f"{len(m['sourcesContent'])} contents, so comparing the arrays elementwise "
        "no longer means what this suite assumes."
    )


@pytest.mark.harness
def test_shorthand_and_empty_object_agree_in_state_a(expected):
    """`sourcemap: true` and `sourcemap: {}` must be the same thing upstream.

    The two cases exist as a pair. If State A ever distinguished them, comparing
    each against its own expectation would still pass while the pair's point --
    that the shorthand is exactly the defaulted object -- had been lost.

    Compared on the option-derived fields and the emitted URL, not on the whole
    map: the two cases compile deliberately different text, because
    `test_every_source_is_distinct` requires it, so `mappings` and `names` answer
    to the selector rather than to the option. Everything the shorthand could
    plausibly get wrong -- `file`, `sources`, `sourceRoot`, whether
    `sourcesContent` is filled, the URL -- is source-independent and is compared.
    """
    a, b = expected.get("true-shorthand").value, expected.get("empty-object").value
    option_derived = ("version", "file", "sources", "sourceRoot", "sourcesContent")
    differing = [f for f in option_derived if a["sourcemap"].get(f) != b["sourcemap"].get(f)]
    assert not differing, (
        "`sourcemap: true` and `sourcemap: {}` no longer agree in State A on "
        f"{differing}, so the shorthand is not the defaulted object:\n"
        + "\n".join(
            f"  {f}:\n    true: {a['sourcemap'].get(f)!r}\n    {{}}:   {b['sourcemap'].get(f)!r}"
            for f in differing
        )
    )
    assert _url_from(a["css"] or "") == _url_from(b["css"] or ""), (
        "the shorthand and the empty object now advertise different "
        f"sourceMappingURLs: {_url_from(a['css'] or '')!r} vs {_url_from(b['css'] or '')!r}"
    )


@pytest.mark.harness
def test_no_case_depends_on_the_default_base_path():
    """Every case pins `basePath` or `dest`, or has no filename.

    A default `basePath` of '.' resolves against the cwd, which differs between
    the oracle process, the node adapter and the realm -- so such a case would
    compare three answers and fail every submission for the harness's choice of
    working directory. See the module docstring in harness/sourcemaps.py.
    """
    offenders = []
    for c in sourcemaps.cases():
        if not c.sourcemap or c.expect_no_map:
            continue                      # no map, so normalizePath never runs
        if not c.filename:
            continue                      # both sides of relative() are cwd-relative
        if c.dest:
            continue
        if isinstance(c.sourcemap, dict) and c.sourcemap.get("basePath"):
            continue
        offenders.append(c.id)
    assert not offenders, (
        f"these cases rely on basePath defaulting to '.': {offenders}. "
        "Pin an absolute virtual basePath or dest."
    )


@pytest.mark.harness
def test_every_source_is_distinct():
    """Two cases with identical source share a parse and cross their filenames.

    `Parser.cache` is static and keyed on `sha1(source + prefix)`, and the cached
    clone is stamped with whatever `nodes.filename` was current. Cases that
    compile a fixture are exempt: they name the file they read, so a shared parse
    carries the right filename anyway.
    """
    seen: dict[str, str] = {}
    clashes = []
    for c in sourcemaps.cases():
        if c.source_file:
            continue
        if c.source in seen:
            clashes.append(f"{c.id} and {seen[c.source]}")
        seen[c.source] = c.id
    assert not clashes, (
        "these cases share source text, so the second inherits the first's "
        f"filename from the static parse cache: {clashes}"
    )


@pytest.mark.harness
def test_declared_node_parity_ids_exist():
    unknown = [cid for cid in NODE_PARITY_IDS if cid not in sourcemaps.ids()]
    assert not unknown, f"NODE_PARITY_IDS names cases that do not exist: {unknown}"


@pytest.mark.harness
def test_no_case_leaks_a_host_path():
    """No expectation may name a real directory.

    Every path in this catalog is virtual, so a real one would mean a case was
    written against the build host and would fail on any other machine.
    """
    bad = []
    for c in sourcemaps.cases():
        for field in (c.filename, c.dest, c.source_file, json.dumps(c.sourcemap)):
            text = str(field)
            if text.startswith("/") and not text.startswith(layout.VPROJ) \
                    and not text.startswith(layout.VRUNTIME) and text != "/":
                bad.append(f"{c.id}: {text}")
    assert not bad, f"non-virtual absolute paths in the catalog: {bad}"
