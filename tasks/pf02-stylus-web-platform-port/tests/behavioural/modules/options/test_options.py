"""Every render option upstream honours, one row at a time.

The corpus sweep compiles each case with the options upstream's own `run.js`
assigns by filename, which covers `compress`, `include css`, `prefix` and
`hoist atrules` and nothing else.  The rest of the option surface -- `linenos`,
`firebug`, `indent spaces`, `globals`, `imports`, the sourcemap variants -- is
never exercised there, so a port could drop it entirely and lose no corpus case.

Each row below compiles a real corpus file twice, once through State A and once
through `src/core`, and compares the bytes.  Three rows carry `revirtCss` because
the option writes the source path into the stylesheet: the oracle's absolute path
is mapped back to the virtual one first, so the row fails on behaviour rather than
on the prefix.  That mapping is also why `/runtime` is mounted -- see
`runners.default_mounts()`.
"""
from __future__ import annotations

import pytest

from harness import layout, optionmatrix as OM, runners
from srbstylus import permitted_skip, require_core


@pytest.fixture(scope="module")
def expected():
    return runners.oracle(OM.ops())


@pytest.fixture(scope="module")
def actual():
    require_core()
    return runners.sandbox(OM.ops())


def _trim(css: str | None, limit: int = 500) -> str:
    if css is None:
        return "<none>"
    one = " | ".join(line.strip() for line in css.strip().splitlines())
    return one if len(one) <= limit else f"{one[:limit]}... ({len(one)} chars)"


def _compiled(actual, cid: str):
    """The submission's result for `cid`, or a failure if it did not produce one.

    Deliberately an assertion and not a `permitted_skip`. A skip is neutral in both
    the numerator and the denominator, so excusing the checks below when the
    submission cannot compile would *raise* the score of a port that emits nothing
    over one that emits something wrong. `permitted_skip` is for the oracle failing
    to state an expectation -- never for the submission failing to meet one.
    """
    got = actual.get(cid)
    assert got.ok, (
        f"{cid}: src/core raised, so this option's behaviour could not be checked\n"
        f"  case: {OM.describe()[cid]}\n"
        f"  {(got.error or {}).get('name')}: {_trim((got.error or {}).get('message'))}"
    )
    return got


# ------------------------------------------------------------------ the sweep


def test_core_loaded_for_option_matrix(actual):
    """Nothing below can pass if the graph did not link."""
    assert actual.load_error is None, (
        "src/core failed to load in the web realm:\n"
        f"{actual.load_error.get('name')}: {actual.load_error.get('message')}"
    )


@pytest.mark.parametrize("cid", OM.case_ids())
def test_option_matches_state_a(cid, expected, actual):
    exp = expected.get(cid)
    if not exp.ok:
        permitted_skip(
            f"State A could not compile option case {cid}: {(exp.error or {}).get('message')}"
        )

    got = actual.get(cid)
    desc = OM.describe()[cid]
    assert got.ok, (
        f"{cid}: src/core raised where State A produced CSS\n"
        f"  case: {desc}\n"
        f"  {(got.error or {}).get('name')}: {(got.error or {}).get('message')}"
    )
    assert got.css == exp.css, (
        f"{cid}: output differs from State A\n"
        f"  case:     {desc}\n"
        f"  state-a:  {_trim(exp.css)}\n"
        f"  src/core: {_trim(got.css)}"
    )


# ------------------------------------------------- what each option should do
#
# The byte comparison above already decides these rows.  These checks exist for
# the failure message: "your linenos output contains no line comments at all" is
# a different diagnosis from "bytes differ at offset 400", and points at the
# option that was not implemented rather than at the case that used it.


def test_linenos_annotates_every_rule(actual):
    """§2.3: `/* line N : path */` before each rule, naming the file it came from."""
    got = _compiled(actual, "linenos")
    assert "/* line " in (got.css or ""), (
        "linenos produced no line comments. The option annotates each rule with its "
        f"source line and file; output was:\n  {_trim(got.css)}"
    )
    assert f"{layout.VPROJ}/test/cases/dumb.styl" in (got.css or ""), (
        "linenos annotations do not name the source file. The path must be the one "
        f"the host gave as the filename ({layout.VPROJ}/test/cases/dumb.styl); "
        f"output was:\n  {_trim(got.css)}"
    )


def test_firebug_emits_debug_info_media_block(actual):
    """`@media -stylus-debug-info`, with the path escaped for a font-family value."""
    got = _compiled(actual, "firebug")
    css = got.css or ""
    assert "-stylus-debug-info" in css, (
        "firebug produced no debug-info media blocks. The option wraps each rule's "
        f"origin in `@media -stylus-debug-info`; output was:\n  {_trim(css)}"
    )
    assert "file\\:\\/\\/" in css, (
        "firebug emitted the source path unescaped. Upstream escapes `/` and `:` so "
        f"the path survives as a font-family value; output was:\n  {_trim(css)}"
    )


def test_builtin_library_is_annotated_at_its_runtime_path(actual):
    """The debug options name the built-in library, and must name it where it lives.

    §1.4: the port loads the library from `platform.runtimeRoot`, so its
    annotations have to say so. A submission that inlined the library into a
    string would have no path to report here, and one that hard-coded upstream's
    `lib/functions` would report a directory that does not exist in a web host.
    """
    got = _compiled(actual, "linenos")
    assert layout.VRUNTIME in (got.css or ""), (
        f"no linenos annotation names anything under platform.runtimeRoot "
        f"({layout.VRUNTIME}). The built-in .styl library is compiled before the "
        f"stylesheet and upstream annotates it too; output was:\n  {_trim(got.css)}"
    )


def test_compress_removes_optional_whitespace(actual):
    got = _compiled(actual, "compress")
    css = (got.css or "").strip()
    assert "\n" not in css, (
        f"compress left newlines in the output:\n  {_trim(css)}"
    )


def test_sourcemap_comment_is_appended(actual):
    got = _compiled(actual, "sourcemap")
    assert "sourceMappingURL" in (got.css or ""), (
        "the sourcemap option added no sourceMappingURL comment; output was:\n"
        f"  {_trim(got.css)}"
    )


def test_inline_sourcemap_carries_the_whole_map(actual):
    """`inline: true` base64s the map into the comment instead of naming a file."""
    got = _compiled(actual, "sourcemap-inline")
    css = got.css or ""
    assert "data:application/json;base64," in css, (
        "the inline sourcemap option did not embed the map as a data URI. Upstream "
        "base64-encodes the whole map into the comment; output was:\n"
        f"  {_trim(css)}"
    )


def test_globals_are_visible_without_an_import(actual, expected):
    """A value injected through `globals` resolves like any other variable."""
    exp, got = expected.get("globals"), actual.get("globals")
    if not exp.ok:
        permitted_skip("oracle could not compile the globals case")
    assert got.ok, f"src/core raised on the globals case: {(got.error or {}).get('message')}"
    assert got.css == exp.css, (
        "the globals option did not inject the variable\n"
        f"  state-a:  {_trim(exp.css)}\n"
        f"  src/core: {_trim(got.css)}"
    )


def test_imports_are_prepended_in_order(actual, expected):
    """`imports` compiles its files before the source, in the order given."""
    for cid in ("imports-one", "imports-two"):
        exp, got = expected.get(cid), actual.get(cid)
        if not exp.ok:
            permitted_skip(f"oracle could not compile {cid}")
        assert got.ok, f"{cid}: src/core raised: {(got.error or {}).get('message')}"
        assert got.css == exp.css, (
            f"{cid}: auto-imported files did not land as State A places them\n"
            f"  state-a:  {_trim(exp.css)}\n"
            f"  src/core: {_trim(got.css)}"
        )


# ------------------------------------------------------- the matrix as a whole


def test_every_option_row_matches(expected, actual):
    """One check that says the option surface is complete, not merely mostly right."""
    comparable = [c.id for c in OM.CASES if expected.get(c.id).ok]
    if not comparable:
        permitted_skip("oracle compiled none of the option cases")

    wrong = []
    for cid in comparable:
        got = actual.get(cid)
        if not got.ok:
            wrong.append(f"{cid}: raised {(got.error or {}).get('message')!r}")
        elif got.css != expected.get(cid).css:
            wrong.append(f"{cid}: wrong output ({OM.describe()[cid]})")
    assert not wrong, (
        f"{len(wrong)} of {len(comparable)} option rows disagree with State A:\n  "
        + "\n  ".join(wrong[:25])
    )


# ------------------------------------------------------- harness self-checks
#
# Not judgements on the submission: they check that the matrix above can fail.
# An option whose row equals its own baseline would be awarded to a submission
# that ignored the option entirely, which measures nothing.


#: `(option row, baseline row)` pairs whose State A outputs must differ.
_MUST_DIFFER: tuple[tuple[str, str], ...] = (
    ("compress", "plain"),
    ("indent-1", "plain"),
    ("indent-4", "plain"),
    ("indent-8", "plain"),
    ("linenos", "plain"),
    ("firebug", "plain"),
    ("linenos+firebug", "linenos"),
    ("hoist", "no-hoist"),
    ("include-css", "no-include-css"),
    ("prefix", "no-prefix"),
    ("sourcemap", "plain"),
    ("sourcemap-inline", "sourcemap"),
    ("sourcemap-basePath", "sourcemap"),
    ("globals", "plain"),
    ("imports-one", "plain"),
    ("imports-two", "imports-one"),
)


@pytest.mark.harness
@pytest.mark.parametrize("row,baseline", _MUST_DIFFER, ids=[f"{a}-vs-{b}" for a, b in _MUST_DIFFER])
def test_option_changes_state_a_output(row, baseline, expected):
    """The option demonstrably does something to pristine State A's output.

    If it does not, the corresponding row is vacuous and would be handed to a
    submission that never implemented the option.
    """
    a, b = expected.get(row), expected.get(baseline)
    if not a.ok or not b.ok:
        permitted_skip(f"oracle could not compile {row} or {baseline}")
    assert a.css != b.css, (
        f"harness defect: in State A, `{row}` produces byte-identical output to "
        f"`{baseline}`, so the matrix row cannot distinguish a port that honours "
        f"the option from one that ignores it. Row: {OM.describe()[row]}"
    )


@pytest.mark.harness
def test_matrix_rows_are_distinct_or_declared(expected):
    """Rows collide only where a comment says they should.

    `compress-beats-indent` is meant to equal `compress` -- that precedence is the
    assertion. Any other collision means two rows are measuring one thing.
    """
    by_output: dict[str, list[str]] = {}
    for c in OM.CASES:
        r = expected.get(c.id)
        if r.ok and r.css is not None:
            by_output.setdefault(r.css.strip(), []).append(c.id)

    undeclared = [
        ids for ids in by_output.values()
        if len(ids) > 1 and not any(i in OM.INTENDED_COLLISIONS for i in ids)
    ]
    assert not undeclared, (
        "option rows produce identical State A output without being declared as "
        f"intended collisions: {undeclared}. Either the rows are redundant or the "
        "sources do not exercise what they claim to."
    )


# ------------------------------------------------------------------ provenance


def test_option_cases_were_read_through_the_capability_object(actual):
    """The debug options stat and read the source file; that has to go through the VFS.

    §1.2 forbids `fs`. A port that resolved these paths some other way would still
    match the bytes, so the access log is what shows the read happened at all.
    """
    reads = [e for e in actual.access_log if f"{layout.VPROJ}/test/cases" in e]
    assert reads, (
        f"no read under {layout.VPROJ}/test/cases across {len(OM.CASES)} option "
        "cases. The sources were compiled without ever being fetched through "
        "platform, which means the imports and debug annotations cannot be real."
    )
