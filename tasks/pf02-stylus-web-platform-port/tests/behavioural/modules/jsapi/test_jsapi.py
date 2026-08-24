"""`require('stylus')`, asked the questions a plugin author asks.

§1.5 keeps `src/node/` as the API existing Node users already depend on, and this
suite compares that API against State A's answer for every question in
`harness/jsapi.py`.  Three things are worth saying about how the rows are shaped.

**The synchronous contract is a row, not an assertion.**  `render()` returns a
String upstream, and the drivers on both sides report `wasPromise` rather than
awaiting quietly.  So a port whose `render()` went async produces the right CSS
and still fails, which is the point: the CSS was never the part at risk.

**`define()` gets the most rows because it is the part most likely to be lost.**
The corpus dimension would pass on a port that handed plugin functions plain JS
numbers instead of `Unit` nodes -- no corpus case defines a function.  Every
ecosystem plugin would break.  The cases here make the defined function report
what it actually received, so the row fails on the node type.

**Failures are compared, not just detected.**  A port that threw a bare `Error`
where upstream throws a positioned `ParseError` breaks every editor integration
that reads `.lineno`.  Each failing case therefore checks the name, the message,
and the position separately, so a partial answer scores partially.
"""
from __future__ import annotations

import re

import pytest

from harness import jsapi, layout, runners
from harness.cli import _css_escape
from srbstylus import permitted_skip


# --------------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def expected():
    """State A's answers.  One process for the whole catalog."""
    return runners.oracle(jsapi.ops())


@pytest.fixture(scope="module")
def actual():
    """The submission's, or None when there is no Node-facing API to ask.

    Which entry point that is comes from `layout.face()`, not from here: §1.5's
    `src/node/index.js` in a ported tree, the package's own CommonJS entry in one
    that has not been ported.  Every row below is `render`, `deps`, `convertCSS` or
    the surface they hang off, which is the same question of either.
    """
    if not layout.node_face_available():
        return None
    return runners.node_adapter(jsapi.ops())


def _pair(expected, actual, cid: str):
    """The oracle's result and the submission's, both required to be present.

    Missing `src/node/index.js` fails every row rather than skipping them: §1.5
    makes the adapter part of the deliverable, so its absence is the failure this
    dimension exists to catch.  Skipping would score a submission that deleted the
    Node API above one that kept it and got a detail wrong.
    """
    exp = expected.get(cid)
    assert actual is not None, (
        f"src/node/index.js does not exist at {layout.NODE_ENTRY}. §1.5 keeps the "
        "Node-facing API on that entry point; without it nothing in this dimension "
        "can be asked."
    )
    if actual.load_error:
        pytest.fail(
            "src/node/index.js could not be imported, so no row in this dimension "
            f"can run:\n  {actual.load_error.get('name')}: "
            f"{actual.load_error.get('message', '')[:600]}"
        )
    return exp, actual.get(cid)


def _describe(cid: str) -> str:
    return f"{cid}: {jsapi.describe()[cid]}"


def _trim(s, n: int = 400) -> str:
    s = "" if s is None else str(s)
    return s if len(s) <= n else f"{s[:n]}... (+{len(s) - n} bytes)"


#: `linenos` and `firebug` annotate every file they compile, and one of those files
#: is the built-in `.styl` library.  The oracle's copy is at `/runtime/index.styl`;
#: the submission's is wherever it put it, and instruction.md §1.4 only says to keep
#: the `.styl` sources somewhere under `src/core/` and point `runtimeRoot` at them
#: -- neither the directory nor the filename is pinned.  So that one annotation is
#: collapsed on both sides.  The project's own paths are left exact: they come from
#: the fixture mount, they are identical on both sides, and requiring them is the
#: whole point of the row.
_RUNTIME_LINENO = re.compile(r"/\* line (\d+) : (?!" + re.escape(layout.VPROJ) + r"/)\S* \*/")
_RUNTIME_FIREBUG = re.compile(
    r"filename\{font-family:file(?:\\.)*(?!" + re.escape(_css_escape(layout.VPROJ)) + r")[^}]*\}"
)


def _collapse_runtime(text: str | None) -> str:
    if not text:
        return "" if text is None else text
    out = _RUNTIME_LINENO.sub(r"/* line \1 : <runtime> */", text)
    return _RUNTIME_FIREBUG.sub("filename{font-family:<runtime>}", out)


#: Cases State A answers with a CSS string.  Split from the failing ones because
#: the two need different assertions, and a case cannot be in both.
def _ok_css_ids(batch) -> list[str]:
    return [c.id for c in jsapi.cases()
            if c.kind in ("render", "renderTopLevel", "renderCallback")]


#: The cases whose input State A cannot compile.  Declared, not read off the
#: oracle: the row count must not depend on the run, and
#: `test_declared_failures_still_fail` asserts the declaration is still true.
#:
#: Two things are collected over its complement rather than over every case --
#: `test_render_returns_a_string_not_a_promise` and
#: `test_callback_fires_synchronously`.  Both ask what the *return value* is, and a
#: call that throws has none: there is no String to type-check and no callback to
#: not `permitted_skip` on these.  A skip is charged 0 rather than left out of the
#: denominator, so exempting 8 rows -- 7 render ids and `error-in-callback` -- would
#: charge every submission for checks none of them could answer.  Filtering here
#: deletes the question instead of exempting it, so the module's size is the number
#: of questions it can put.
#:
#: These cases are not ungraded.  Every one is asked, by name, whether it fails at
#: all (`test_render_matches_state_a` asserts `not got.ok`) and then how: the error's
#: `.name`, its message, and its `.lineno`/`.column`/`.filename` positions, which is
#: four more rows each than the two removed here.
FAILING_IDS: tuple[str, ...] = (
    "error-parse", "error-bad-extend", "error-mixin-arity", "error-unit-arg",
    "error-missing-import", "error-filename-reported", "error-in-callback",
    "define-throwing-function",
)


# ------------------------------------------------------------------ the API shape

def test_adapter_imports(actual):
    assert actual is not None, (
        f"src/node/index.js does not exist at {layout.NODE_ENTRY}; §1.5 requires it."
    )
    assert actual.load_error is None, (
        "src/node/index.js failed to import:\n"
        f"  {(actual.load_error or {}).get('name')}: "
        f"{(actual.load_error or {}).get('message', '')[:800]}"
    )


def test_version_matches_state_a(expected, actual):
    exp, got = _pair(expected, actual, "version")
    assert exp.ok, "harness: the oracle could not read stylus.version"
    assert got.ok, f"src/node has no readable .version: {(got.error or {}).get('message')}"
    assert got.value.get("version") == exp.value["version"], (
        f"stylus.version is {got.value.get('version')!r}; State A says "
        f"{exp.value['version']!r}. §3 pins the published version."
    )


def test_module_is_callable(expected, actual):
    """`module.exports = render`, so `stylus(str)` works and `.render` hangs off it."""
    exp, got = _pair(expected, actual, "callable")
    assert exp.value["callable"] is True, "harness: State A's export stopped being callable"
    assert got.ok, f"the callable probe failed: {(got.error or {}).get('message')}"
    assert got.value.get("callable") is True, (
        "src/node's default export is not a function. Upstream sets "
        "`module.exports = render`, so `stylus('...')` is the documented way to "
        "make a renderer and every existing call site uses it."
    )


def test_middleware_still_exported(expected, actual):
    exp, got = _pair(expected, actual, "middleware-present")
    assert exp.value["present"] is True, "harness: State A stopped exporting middleware"
    assert got.ok, f"the middleware probe failed: {(got.error or {}).get('message')}"
    assert got.value.get("present") is True, (
        "src/node does not export a `middleware` function. It is Node-only by "
        "nature -- it serves files off a filesystem -- which is exactly why §1.5 "
        "keeps it on the Node side rather than dropping it."
    )


#: The exported names, checked one row each so a submission that kept eleven of
#: fourteen scores eleven.  Read off the oracle at collection time would make the
#: row count depend on the run; this list is the declaration, and
#: `test_declared_exports_match_state_a` keeps it honest.
DECLARED_EXPORTS: tuple[str, ...] = (
    "Compiler", "Evaluator", "Normalizer", "Parser", "Visitor",
    "convertCSS", "functions", "middleware", "nodes", "render",
    "resolver", "url", "utils", "version",
)


@pytest.mark.parametrize("name", DECLARED_EXPORTS)
def test_export_present(name, expected, actual):
    exp, got = _pair(expected, actual, "api-surface")
    assert name in exp.value["keys"], f"harness: State A no longer exports {name!r}"
    assert got.ok, f"the api-surface probe failed: {(got.error or {}).get('message')}"
    assert name in got.value.get("keys", []), (
        f"src/node does not export {name!r}. State A does, and §1.5 keeps the "
        f"public surface. Present: {sorted(got.value.get('keys', []))}"
    )


# --------------------------------------------------------------- render() itself

@pytest.mark.parametrize("cid", [c.id for c in jsapi.cases()
                                if c.kind in ("render", "renderTopLevel", "renderCallback")])
def test_render_matches_state_a(cid, expected, actual):
    """Same call, same CSS -- or the same failure, for the cases that fail."""
    exp, got = _pair(expected, actual, cid)

    if not exp.ok:
        # A failing case is checked by the three failure tests below; here it only
        # has to fail on both sides.  Passing where State A refused is a real
        # divergence, so it is an assertion rather than a skip.
        assert not got.ok, (
            f"{_describe(cid)}\nsrc/node compiled this; State A refuses it with "
            f"{(exp.error or {}).get('name')}: "
            f"{_trim((exp.error or {}).get('message'), 200)}\n"
            f"src/node produced: {_trim(got.css, 200)}"
        )
        return

    assert got.ok, (
        f"{_describe(cid)}\nsrc/node raised where State A succeeded:\n"
        f"  {(got.error or {}).get('name')}: {_trim((got.error or {}).get('message'))}"
    )
    # Only the debug-annotation cases go through the collapse, and only their
    # built-in-library annotation is affected; every other byte is compared exact.
    want, have = (exp.css, got.css)
    if jsapi.by_id(cid).revirt:
        want, have = _collapse_runtime(want), _collapse_runtime(have)
    assert have == want, (
        f"{_describe(cid)}\n"
        f"  state-a:  {_trim(want)}\n"
        f"  src/node: {_trim(have)}"
    )


@pytest.mark.parametrize("cid", [c.id for c in jsapi.cases()
                                if c.kind == "render" and c.id not in FAILING_IDS])
def test_render_returns_a_string_not_a_promise(cid, expected, actual):
    """§1.5, one row per case rather than one row for the sweep.

    A port that made `render()` async breaks every synchronous call site, and it
    breaks them whatever the CSS says.  Per-case because a port might return a
    String on the easy path and a Promise the moment an import has to be read --
    which is precisely the shape the async core makes tempting.

    Collected over the cases State A compiles.  A call that throws returns nothing
    to type-check, so the seven `FAILING_IDS` of this kind are not asked here; they
    are asked whether they throw, and with which name, message and position.
    """
    exp, got = _pair(expected, actual, cid)
    assert exp.value.get("wasPromise") is False, (
        f"harness: State A's render() returned a thenable for {cid}"
    )
    assert got.ok, (
        f"{_describe(cid)}\n`renderer.render()` did not return at all, so it did "
        f"not return a String:\n{_trim(got.error)}"
    )
    assert got.value.get("wasPromise") is False, (
        f"{_describe(cid)}\n`renderer.render()` returned a Promise. State A returns "
        "a String and §1.5 keeps that: `var css = stylus(str).render()` is the "
        "documented synchronous API, and a Promise breaks it at every call site "
        "even though the CSS inside is right."
    )


@pytest.mark.parametrize("cid", [c.id for c in jsapi.cases() if c.kind == "renderTopLevel"])
def test_top_level_render_returns_a_string(cid, expected, actual):
    """`stylus.render(str, opts)` is its own export and carries the same promise."""
    exp, got = _pair(expected, actual, cid)
    assert exp.ok and exp.value.get("wasPromise") is False, (
        f"harness: State A's stylus.render() misbehaved on {cid}"
    )
    assert got.ok, (
        f"{_describe(cid)}\n`stylus.render(str, opts)` raised where State A "
        f"returned a String:\n{_trim(got.error)}"
    )
    assert got.value.get("wasPromise") is False, (
        f"{_describe(cid)}\n`stylus.render(str, opts)` returned a Promise; State A "
        "returns a String."
    )


@pytest.mark.parametrize("cid", [c.id for c in jsapi.cases()
                                if c.kind == "renderCallback" and c.id not in FAILING_IDS])
def test_callback_fires_synchronously(cid, expected, actual):
    """`render(fn)` calls back before it returns.

    Upstream's own `bin/stylus` relies on it -- `renderer.render(fn)` with the
    write inside the callback -- and so does every build plugin that returns the
    CSS from the enclosing function.  A port that deferred the callback to a
    microtask produces identical CSS and breaks all of them.

    Collected over the cases State A compiles.  `error-in-callback` reports its
    failure through `err` rather than by throwing, which is contract and is graded
    as such by the failure rows; what it does not have is a successful callback
    whose timing could be compared, so it is not asked here.
    """
    exp, got = _pair(expected, actual, cid)
    assert exp.value.get("sync") is True, f"harness: State A deferred the callback for {cid}"
    assert got.ok, (
        f"{_describe(cid)}\n`render(cb)` reported an error where State A called "
        f"the callback and succeeded:\n{_trim(got.error)}"
    )
    assert got.value.get("sync") is True, (
        f"{_describe(cid)}\nthe callback passed to `render()` had not fired by the "
        "time `render()` returned. State A calls it synchronously."
    )


# ------------------------------------------------------------------ deps() and convertCSS()

@pytest.mark.parametrize("cid", [c.id for c in jsapi.cases() if c.kind == "deps"])
def test_deps_matches_state_a(cid, expected, actual):
    """The dependency list, as a list -- order included.

    Order is part of it because `deps()` exists to be fed to a watcher, and a
    build tool that diffs the list against its previous run sees a reorder as a
    change.  Paths are compared virtualised, so where the verifier unpacked the
    tree does not enter into it.
    """
    exp, got = _pair(expected, actual, cid)
    if not exp.ok:
        permitted_skip(f"State A's deps() failed for {cid}")
    assert got.ok, (
        f"{_describe(cid)}\nsrc/node's deps() raised where State A's returned:\n"
        f"  {(got.error or {}).get('name')}: {_trim((got.error or {}).get('message'))}"
    )
    assert got.value.get("deps") == exp.value["deps"], (
        f"{_describe(cid)}\n"
        f"  state-a:  {exp.value['deps']}\n"
        f"  src/node: {got.value.get('deps')}"
    )


@pytest.mark.parametrize("cid", [c.id for c in jsapi.cases() if c.kind == "convertCSS"])
def test_convert_css_matches_state_a(cid, expected, actual):
    """`stylus.convertCSS` -- the CSS-to-Stylus direction, which has no other suite.

    It is pure string work with no filesystem in it, so a port has no excuse for
    changing it, and it is exactly the kind of secondary export a rewrite drops.
    """
    exp, got = _pair(expected, actual, cid)
    assert exp.ok, f"harness: State A's convertCSS failed on {cid}"
    assert got.ok, (
        f"{_describe(cid)}\nsrc/node's convertCSS raised:\n"
        f"  {(got.error or {}).get('name')}: {_trim((got.error or {}).get('message'))}"
    )
    assert got.value.get("styl") == exp.value["styl"], (
        f"{_describe(cid)}\n"
        f"  state-a:  {_trim(exp.value['styl'])}\n"
        f"  src/node: {_trim(got.value.get('styl'))}"
    )


# ------------------------------------------------------------------- how it fails
#
# `FAILING_IDS` is declared above, with the rest of the collection-time lists,
# because two of the tests before this point are collected over its complement.


@pytest.mark.parametrize("cid", FAILING_IDS)
def test_failure_name_matches_state_a(cid, expected, actual):
    exp, got = _pair(expected, actual, cid)
    assert not exp.ok, f"harness: {cid} compiles in State A"
    assert not got.ok, (
        f"{_describe(cid)}\nsrc/node accepted input State A rejects with "
        f"{(exp.error or {}).get('name')}."
    )
    assert (got.error or {}).get("name") == (exp.error or {}).get("name"), (
        f"{_describe(cid)}\nthe error's `.name` differs, and tools switch on it:\n"
        f"  state-a:  {(exp.error or {}).get('name')!r}\n"
        f"  src/node: {(got.error or {}).get('name')!r}"
    )


@pytest.mark.parametrize("cid", FAILING_IDS)
def test_failure_message_matches_state_a(cid, expected, actual):
    """The whole message, source excerpt and caret included.

    Upstream's message is a formatted block -- filename, position, the offending
    lines, a caret under the column, then the explanation -- and it is what a
    developer reads. Comparing only the last line would let a port drop the
    excerpt and keep the row.
    """
    exp, got = _pair(expected, actual, cid)
    assert not exp.ok, f"harness: {cid} compiles in State A"
    assert not got.ok, f"{_describe(cid)}\nsrc/node accepted input State A rejects."
    assert (got.error or {}).get("message") == (exp.error or {}).get("message"), (
        f"{_describe(cid)}\n"
        f"  state-a:\n{_trim((exp.error or {}).get('message'), 700)}\n"
        f"  src/node:\n{_trim((got.error or {}).get('message'), 700)}"
    )


#: Which position properties each failing case's error actually carries, measured
#: against State A.  Declared per case rather than crossed with every property,
#: because upstream's coverage is genuinely uneven and a cross product would spend
#: 14 of its 32 rows skipping.
#:
#: `utils.formatException` builds a *new* Error and copies a different subset
#: depending on where the failure came from.  A `ParseError` arrives carrying only
#: `name` and `message`; an `@extend` failure keeps `lineno` and `column` but no
#: filename; an evaluation failure keeps all four.  This is not visible from the
#: message text, which embeds `file:line:col` in every case -- reading the message
#: would have suggested the properties were there.
#:
#: An empty tuple is a claim too, and `test_declared_failure_props_match_state_a`
#: checks it in both directions: declared properties must be present, undeclared
#: ones must be absent.
FAILURE_PROPS: dict[str, tuple[str, ...]] = {
    # ParseError -- formatException returns a fresh error with the formatted
    # message and nothing else.  `error-filename-reported` is the sharp one: the
    # filename it sets reaches the message text but never `.filename`.
    "error-parse": (),
    "error-filename-reported": (),
    "error-in-callback": (),
    # @extend resolution: positioned, but no filename.
    "error-bad-extend": ("lineno", "column"),
    # Evaluation: the full set.  `error-missing-import`'s `stylusStack` is the
    # empty string -- it fails at the top level, so there are no frames to join.
    # Empty is still a value, and a port that dropped the property fails the row.
    "error-mixin-arity": ("lineno", "column", "filename", "stylusStack"),
    "error-unit-arg": ("lineno", "column", "filename", "stylusStack"),
    "error-missing-import": ("lineno", "column", "filename", "stylusStack"),
    "define-throwing-function": ("lineno", "column", "filename", "stylusStack"),
}

POSITION_PROPS: tuple[str, ...] = ("lineno", "column", "filename", "stylusStack")

#: The flat (case, property) list the rows below parametrise over.
FAILURE_PROP_PAIRS = tuple(
    pytest.param(cid, prop, id=f"{cid}-{prop}")
    for cid in FAILING_IDS
    for prop in FAILURE_PROPS[cid]
)


@pytest.mark.parametrize("cid,prop", FAILURE_PROP_PAIRS)
def test_failure_position_matches_state_a(cid, prop, expected, actual):
    """`.lineno`, `.column`, `.filename`, `.stylusStack` -- what tooling reads.

    One row per property the case actually has, so a port that gets the line right
    and the column wrong scores the line.  See `FAILURE_PROPS` for why the pairs
    are declared rather than crossed.
    """
    exp, got = _pair(expected, actual, cid)
    assert not exp.ok, f"harness: {cid} compiles in State A"
    want = (exp.error or {}).get(prop)
    assert want is not None, (
        f"harness: State A's error for {cid} no longer carries .{prop}; "
        "FAILURE_PROPS is stale."
    )
    assert not got.ok, f"{_describe(cid)}\nsrc/node accepted input State A rejects."
    assert (got.error or {}).get(prop) == want, (
        f"{_describe(cid)}\nthe error's `.{prop}` differs:\n"
        f"  state-a:  {want!r}\n"
        f"  src/node: {(got.error or {}).get(prop)!r}"
    )


# ------------------------------------------------------------------ harness self-checks
#
# These consult only the oracle.  They earn no credit -- a submission cannot
# influence them, so paying for them would be a floor every submission collects --
# but a failure voids the run, because each one is a premise the rows above rely on.

@pytest.mark.harness
def test_oracle_answered_every_case(expected):
    missing = [c.id for c in jsapi.cases() if c.id not in expected.results]
    assert not missing, (
        f"the oracle returned no result for {len(missing)} case(s): {missing[:10]}\n"
        f"driver stderr:\n{expected.driver_stderr[-2000:]}"
    )


@pytest.mark.harness
def test_declared_exports_match_state_a(expected):
    r = expected.get("api-surface")
    assert r.ok, f"the oracle could not read stylus's exports: {r.error}"
    assert sorted(DECLARED_EXPORTS) == sorted(r.value["keys"]), (
        "DECLARED_EXPORTS no longer matches State A's export list, so the "
        "per-export rows are measuring a stale declaration.\n"
        f"  declared: {sorted(DECLARED_EXPORTS)}\n"
        f"  state-a:  {sorted(r.value['keys'])}"
    )


@pytest.mark.harness
def test_declared_failures_still_fail(expected):
    """Every id in FAILING_IDS must be a case State A refuses.

    If upstream started accepting one, three parametrised tests would be asserting
    against an error that no longer exists and would fail every submission for the
    harness's mistake.
    """
    wrong = [cid for cid in FAILING_IDS if expected.get(cid).ok]
    assert not wrong, (
        f"FAILING_IDS names {wrong}, which State A compiles successfully. "
        "The failure rows would be unsatisfiable."
    )


@pytest.mark.harness
def test_declared_failure_props_match_state_a(expected):
    """`FAILURE_PROPS` must still describe State A, in both directions.

    Under-declaring costs coverage silently: a property upstream carries that this
    table omits is a row nobody runs, and no test fails to say so.  Over-declaring
    is worse -- the row would assert against `None` and fail every submission for
    the harness's stale table.  So the check is symmetric.
    """
    wrong = []
    for cid in FAILING_IDS:
        err = expected.get(cid).error or {}
        present = tuple(p for p in POSITION_PROPS if err.get(p) is not None)
        if present != FAILURE_PROPS[cid]:
            wrong.append(f"  {cid}:\n"
                         f"    declared: {FAILURE_PROPS[cid]}\n"
                         f"    state-a:  {present}")
    assert not wrong, (
        "FAILURE_PROPS no longer matches State A, so the position rows are either "
        "under-measuring or unsatisfiable:\n" + "\n".join(wrong)
    )


@pytest.mark.harness
def test_every_failing_case_is_declared():
    """No failing case may be missing from FAILURE_PROPS.

    A `KeyError` at collection time would take down the whole module, which reads
    as an infrastructure fault rather than the one-line omission it is.
    """
    missing = [cid for cid in FAILING_IDS if cid not in FAILURE_PROPS]
    assert not missing, (
        f"FAILING_IDS names {missing}, which FAILURE_PROPS does not describe. "
        "Add the measured tuple (empty if the error carries no position)."
    )


@pytest.mark.harness
def test_some_failure_carries_a_full_position(expected):
    """At least one case must exercise all four properties.

    Every entry in `FAILURE_PROPS` could go empty -- a port that threw bare `Error`
    objects everywhere would make that the honest description -- and then this
    dimension would check nothing about positions while still reporting green.
    The table is measured against State A, so this asserts State A itself still
    produces a fully-positioned error to compare against.
    """
    full = [cid for cid, props in FAILURE_PROPS.items()
            if set(props) == set(POSITION_PROPS)]
    assert full, (
        "no declared failure carries all of "
        f"{POSITION_PROPS}, so the position rows cannot detect a port that drops "
        "error metadata wholesale."
    )


@pytest.mark.harness
def test_no_other_case_fails_in_state_a(expected):
    """The converse: nothing outside FAILING_IDS may fail.

    A case that fails by accident -- a typo in the source, a fixture that moved --
    turns into a row every submission passes by failing too, which measures
    nothing.  This catches it at the harness level instead.
    """
    unexpected = [
        c.id for c in jsapi.cases()
        if c.id not in FAILING_IDS and not expected.get(c.id).ok
    ]
    assert not unexpected, (
        f"{len(unexpected)} case(s) fail in State A without being declared: "
        f"{unexpected}\nEither the case is wrong or it belongs in FAILING_IDS.\n"
        + "\n".join(
            f"  {cid}: {_trim((expected.get(cid).error or {}).get('message'), 200)}"
            for cid in unexpected[:5]
        )
    )


@pytest.mark.harness
def test_every_source_is_distinct():
    """Two cases with identical source share a parse, and the answers cross.

    `Parser.cache` is a static property -- one memo for the whole process -- keyed
    on `sha1(source + prefix)`, and `MemoryCache.set` stamps the cached clone with
    whatever `nodes.filename` was at the time.  So the second op with the same
    source inherits the first's filename, and `set-linenos` annotated
    `<oracle>/stylus` until every source here was made unique.  Informational ops
    (`version`, `callable`, ...) never reach a parser and are exempt.
    """
    seen: dict[tuple[str, str], str] = {}
    clashes = []
    for c in jsapi.cases():
        if c.kind in ("convertCSS", "version", "apiSurface", "callable", "middlewarePresent"):
            continue
        if not c.source.strip():
            continue
        key = (c.source, str(c.settings.get("prefix") or c.options.get("prefix") or ""))
        if key in seen:
            clashes.append((seen[key], c.id))
        seen[key] = c.id
    assert not clashes, (
        f"{len(clashes)} pair(s) of cases share byte-identical source and prefix, "
        f"so they share a cached parse: {clashes}. Give each one a distinct "
        "selector; see the note in harness/jsapi.py."
    )


@pytest.mark.harness
def test_define_probes_actually_observe_node_types(expected):
    """The `define-receives-*` rows have to be able to tell the types apart.

    Each reports `typeof/nodeName/type` for its argument.  If two of them came
    back with the same string, the probe would not distinguish a Unit from an
    Ident and a port that passed plain JS values could satisfy both.  So the
    distinctness of State A's own answers is the premise, and it is checked rather
    than assumed.
    """
    ids = ["define-receives-unit", "define-receives-string",
           "define-receives-ident", "define-receives-rgba"]
    answers = {}
    for cid in ids:
        r = expected.get(cid)
        assert r.ok, f"the oracle could not run {cid}: {r.error}"
        # The reported string is the only interesting part of the CSS.
        answers[cid] = r.css
    assert len(set(answers.values())) == len(ids), (
        "State A reports the same thing for different node types, so these rows "
        f"cannot discriminate: {answers}"
    )
    for cid, css in answers.items():
        assert "undefined/undefined" not in css, (
            f"{cid}: the probe read nothing off its argument in State A "
            f"({css!r}); the row would pass on a port that passed a bare value."
        )


@pytest.mark.harness
def test_include_css_pair_really_differs(expected):
    """`include css` on and off must produce different bytes.

    Otherwise the pair is two rows that any port passes without implementing the
    option at all.
    """
    on = expected.get("set-include-css")
    off = expected.get("set-include-css-off")
    assert on.ok and off.ok, f"the oracle could not run the include-css pair: {on.error or off.error}"
    assert on.css != off.css, (
        "`include css` changes nothing in State A for this source, so neither row "
        f"measures the option:\n  on:  {_trim(on.css, 200)}\n  off: {_trim(off.css, 200)}"
    )


@pytest.mark.harness
def test_instruction_features_are_all_exercised():
    """§1.5 names the JS API features that must keep working; each needs a case."""
    kinds = {c.kind for c in jsapi.cases()}
    have_define = any(c.define_fn or c.define_fn_raw or c.define for c in jsapi.cases())
    have_set = any(c.settings for c in jsapi.cases())
    have_include = any(c.include for c in jsapi.cases())
    have_globals = any("globals" in c.options for c in jsapi.cases())
    missing = []
    if not have_define:
        missing.append(".define")
    if not have_set:
        missing.append(".set")
    if not have_include:
        missing.append(".include")
    if not have_globals:
        missing.append("globals")
    for kind, label in (("deps", ".deps"), ("renderCallback", "callbacks"),
                        ("convertCSS", "convertCSS")):
        if kind not in kinds:
            missing.append(label)
    assert not missing, (
        f"instruction.md §1.5 requires {missing} to keep working, and the "
        "catalog has no case for them."
    )


@pytest.mark.harness
def test_no_case_leaks_a_host_path():
    """Every path in the catalog is virtual, so the job is host-independent."""
    leaks = []
    for c in jsapi.cases():
        blob = repr((c.source, c.options, c.settings, c.include, c.css))
        for real in (str(layout.STATE_A), str(layout.REPO), str(layout.INPUTS)):
            if real and real != "/" and real in blob:
                leaks.append((c.id, real))
    assert not leaks, (
        f"cases name a real host path instead of a virtual one: {leaks}. The two "
        "sides mount different trees, so a real path cannot appear in a job."
    )
