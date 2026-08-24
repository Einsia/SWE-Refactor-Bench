"""Diagnostics, decomposed so a failure names which part is wrong.

Compared against the sandbox: errors are raised, positioned and formatted inside
core, and `test_jsapi.py` already covers the adapter's eight failing cases as
opaque message strings. This suite takes the message apart instead -- header,
gutter, caret, body, stack -- so a port whose diagnostics are structurally right
but off by one column fails on the caret and reports as passing the other four.

The decomposition mirrors how `formatException` builds the string, including the
detail that the caret is *appended to its source line* rather than placed after
the block, so it arrives interleaved:

       10|   p-9: 9px
       11|   width unit(1px, 3)
    ---------------^
       12|   q-1: 1px

Because of that, comparing the caret text and the gutter text separately is not
sufficient: a port could render both correctly and attach the caret to the wrong
line. `test_caret_attaches_to_the_reported_line` is the row for the interleaving.

See `harness/errors.py` for what each case measures and why the catalog is shaped
the way it is.
"""
from __future__ import annotations

import re

import pytest

from harness import errors, layout, runners
from srbstylus import require_core

#: A caret line is dashes then a marker, and nothing else can look like one: every
#: gutter line begins with three spaces and a line number.
CARET_RE = re.compile(r"^-+\^$")

#: `'   ' + pad + lineno + '| ' + text`. The number is what the caret row needs.
GUTTER_RE = re.compile(r"^\s+(\d+)\| ")

#: The `:line:column` that `formatException` appends to the filename in the
#: header. Matched at the end rather than split off it: a header with no position
#: has to fail with that as the reason, not compare the gutter against whatever a
#: split of the wrong shape happened to leave behind.
HEADER_POS_RE = re.compile(r":(\d+):(\d+)$")

#: A `stylusStack` frame, which is how the body and the stack are told apart.
FRAME_PREFIX = "    at "


@pytest.fixture(scope="module")
def expected():
    return runners.oracle(errors.ops(), mounts=errors.oracle_mounts())


@pytest.fixture(scope="module")
def actual():
    require_core()
    return runners.sandbox(errors.ops(), files=errors.sandbox_files())


class Parts:
    """One formatted diagnostic, split the way `formatException` assembled it."""

    __slots__ = ("header", "gutter", "caret", "body", "stack", "block")

    def __init__(self, message: str):
        lines = (message or "").split("\n")
        self.header = lines[0] if lines else ""
        # The source window runs to the blank line that `'\n\n'` introduces.
        i, block = 1, []
        while i < len(lines) and lines[i] != "":
            block.append(lines[i])
            i += 1
        rest = lines[i + 1:]
        # `formatException` ends with a newline, and another after the stack.
        while rest and rest[-1] == "":
            rest.pop()
        self.block = tuple(block)
        self.caret = tuple(l for l in block if CARET_RE.match(l))
        self.gutter = tuple(l for l in block if not CARET_RE.match(l))
        self.stack = tuple(l for l in rest if l.startswith(FRAME_PREFIX))
        self.body = tuple(l for l in rest if not l.startswith(FRAME_PREFIX))

    def get(self, part: str) -> object:
        return getattr(self, part)


def _pair(expected, actual, cid: str):
    assert cid in expected.results, (
        f"harness: the oracle returned no result for {cid}\n"
        f"driver stderr:\n{expected.driver_stderr[-1200:]}"
    )
    if cid not in actual.results:
        pytest.fail(
            f"{errors.describe(cid)}\nsrc/core returned no result for this op.\n"
            f"driver stderr:\n{actual.driver_stderr[-1200:]}"
        )
    return expected.get(cid), actual.get(cid)


def _both_failed(exp, got, cid: str):
    """Every case in this catalog fails in State A; the port must fail it too.

    A port that *compiles* malformed input has a bug this dimension exists to
    catch, and it is worth naming separately from a wrong diagnostic -- the second
    is a formatting slip, the first means the error was never raised.
    """
    assert not exp.ok, (
        f"harness: State A now compiles {cid}, so it has no diagnostic to compare.\n"
        f"css: {((exp.value or {}).get('css') or '')[:200]!r}"
    )
    assert not got.ok, (
        f"{errors.describe(cid)}\nsrc/core compiled this successfully. State A rejects it "
        f"with {(exp.error or {}).get('name')}:\n"
        f"  {((exp.error or {}).get('message') or '').splitlines()[:1]}"
    )
    return exp.error or {}, got.error or {}


def _parts(err: dict) -> Parts:
    return Parts(err.get("message") or "")


# ------------------------------------------------------------------ whole message

@pytest.mark.parametrize("cid", errors.ids())
def test_message_matches_state_a(cid, expected, actual):
    """The complete formatted diagnostic, byte for byte.

    The strictest row in the dimension, and the one a correct port passes without
    any of the others being needed. The decomposed rows exist for the ports that
    fail this one, to say which part went wrong.
    """
    exp, got = _pair(expected, actual, cid)
    exp_err, got_err = _both_failed(exp, got, cid)
    want, have = exp_err.get("message") or "", got_err.get("message") or ""
    if want == have:
        return
    wl, hl = want.split("\n"), have.split("\n")
    first = next((i for i in range(max(len(wl), len(hl)))
                  if (wl[i] if i < len(wl) else None) != (hl[i] if i < len(hl) else None)), 0)
    pytest.fail(
        f"{errors.describe(cid)}\nthe diagnostic differs, first at line {first + 1}:\n"
        f"  state-a:  {(wl[first] if first < len(wl) else '<no such line>')!r}\n"
        f"  src/core: {(hl[first] if first < len(hl) else '<no such line>')!r}\n"
        f"\nfull state-a message:\n{want}\nfull src/core message:\n{have}"
    )


@pytest.mark.parametrize("cid", errors.ids())
def test_error_name_matches_state_a(cid, expected, actual):
    """`ParseError` for syntax, and whatever the thrower used for evaluation.

    Upstream's `ParseError` and `SyntaxError` are its own classes with `name` set
    in the constructor, while evaluation failures surface as plain `Error` or the
    `TypeError` a built-in raised. Tooling switches on this, so a port that
    collapsed everything into one class changes behaviour a consumer can see.
    """
    exp, got = _pair(expected, actual, cid)
    exp_err, got_err = _both_failed(exp, got, cid)
    assert got_err.get("name") == exp_err.get("name"), (
        f"{errors.describe(cid)}\nthe error's `.name` differs:\n"
        f"  state-a:  {exp_err.get('name')!r}\n"
        f"  src/core: {got_err.get('name')!r}"
    )


# ------------------------------------------------------------------ decomposition

@pytest.mark.parametrize("part", errors.PART_NAMES)
@pytest.mark.parametrize("cid", errors.ids())
def test_diagnostic_part_matches_state_a(cid, part, expected, actual):
    """One row per (case, part) of the formatted message.

    Parametrised over every part for every case, including the parts a case does
    not declare -- those assert emptiness on both sides, which is a real check: a
    port that attached a `stylusStack` to a parse error, or drew a caret for an
    unpositioned one, would be adding output upstream does not produce.

    An absent part is only meaningfully absent from a diagnostic that exists,
    though, so those rows first require a source window. Otherwise a port that
    throws a bare `TypeError` passes fifteen of them -- the ones for the cases with
    no stack -- by virtue of having produced nothing at all. The declared-present
    rows need no such gate: they compare real text and a stub fails them outright.
    """
    exp, got = _pair(expected, actual, cid)
    exp_err, got_err = _both_failed(exp, got, cid)
    want, have = _parts(exp_err).get(part), _parts(got_err).get(part)
    declared = part in errors.by_id(cid).parts
    assert bool(want) == declared, (
        f"harness: {cid} declares parts={list(errors.by_id(cid).parts)}, but State A's "
        f"`{part}` is {'empty' if not want else 'non-empty'}. The catalog is stale."
    )
    if not declared:
        assert _parts(got_err).gutter, (
            f"{errors.describe(cid)}\nsrc/core's error is not a formatted Stylus "
            f"diagnostic -- it has no source window -- so its `{part}` is empty for "
            f"the wrong reason:\n"
            f"  {got_err.get('name')}: {(got_err.get('message') or '')[:300]}"
        )
    if isinstance(want, tuple):
        assert have == want, (
            f"{errors.describe(cid)}\nthe `{part}` of the diagnostic differs:\n"
            f"  state-a:\n" + "".join(f"    {l!r}\n" for l in want)
            + f"  src/core:\n" + "".join(f"    {l!r}\n" for l in have)
        )
    else:
        assert have == want, (
            f"{errors.describe(cid)}\nthe `{part}` differs:\n"
            f"  state-a:  {want!r}\n  src/core: {have!r}"
        )


@pytest.mark.parametrize("cid", errors.ids_with("caret"))
def test_caret_attaches_to_the_reported_line(cid, expected, actual):
    """The caret's place *within* the window, not just its shape.

    `formatException` appends the caret to the line whose number equals `lineno`,
    so it lands in the middle of the block for an error mid-file and at the end for
    one at `eos`. Comparing the caret text and the gutter text as separate lists
    loses that: a port could emit the right dashes against the wrong line and pass
    both. This row compares the index and checks the line above it really is the
    reported one.
    """
    exp, got = _pair(expected, actual, cid)
    exp_err, got_err = _both_failed(exp, got, cid)
    ep, gp = _parts(exp_err), _parts(got_err)
    want_i = [i for i, l in enumerate(ep.block) if CARET_RE.match(l)]
    have_i = [i for i, l in enumerate(gp.block) if CARET_RE.match(l)]
    assert len(want_i) == 1, f"harness: State A's {cid} has {len(want_i)} caret lines"
    assert have_i == want_i, (
        f"{errors.describe(cid)}\nthe caret sits at a different place in the window:\n"
        f"  state-a:  index {want_i}\n  src/core: index {have_i}\n"
        f"\nstate-a window:\n" + "\n".join(ep.block)
        + f"\n\nsrc/core window:\n" + "\n".join(gp.block)
    )
    above = gp.block[have_i[0] - 1] if have_i[0] else ""
    m = GUTTER_RE.match(above)
    assert m, (
        f"{errors.describe(cid)}\nthe line the caret points at is not a numbered source "
        f"line: {above!r}"
    )
    pos = HEADER_POS_RE.search(gp.header)
    assert pos, (
        f"{errors.describe(cid)}\nthe header does not end in `:line:column`, so there is "
        f"no reported line for the caret to agree with: {gp.header!r}"
    )
    assert m.group(1) == pos.group(1), (
        f"{errors.describe(cid)}\nthe caret is attached to line {m.group(1)} but the "
        f"header reports line {pos.group(1)}."
    )


# --------------------------------------------------------------- error properties
#
# The message embeds `file:line:col`, so these are not implied by the rows above:
# a port can format the header correctly and still leave the properties unset,
# which is what `test_jsapi.py` found upstream itself does for parse errors.

@pytest.mark.parametrize("prop", ["lineno", "column", "filename", "stylusStack"])
@pytest.mark.parametrize("cid", errors.ids())
def test_error_property_matches_state_a(cid, prop, expected, actual):
    """`.lineno`, `.column`, `.filename`, `.stylusStack` -- present *or* absent.

    Absence is compared too, and deliberately: upstream's parse errors carry none
    of these even though the message says `file:3:1`, because `Renderer#render`
    formats from `parser.lexer` without ever assigning to the error. A port that
    helpfully populated them would diverge from what tooling reads today.

    But absence only means something against a diagnostic that reports a position,
    so the row first requires the port to have produced one. Without that
    precondition a port that throws a bare `TypeError` -- carrying none of these
    properties because it carries nothing -- collects every row where State A also
    leaves the property unset, which for this catalog is most of them. Stated as an
    assertion rather than a skip: a submission that produced no diagnostic has
    failed this check, not sidestepped it.
    """
    exp, got = _pair(expected, actual, cid)
    exp_err, got_err = _both_failed(exp, got, cid)
    assert _parts(got_err).gutter, (
        f"{errors.describe(cid)}\nsrc/core's error is not a formatted Stylus "
        f"diagnostic -- it has no source window -- so there is no position for "
        f"`.{prop}` to agree or disagree with:\n"
        f"  {got_err.get('name')}: {(got_err.get('message') or '')[:300]}"
    )
    want, have = exp_err.get(prop), got_err.get(prop)
    assert have == want, (
        f"{errors.describe(cid)}\nthe error's `.{prop}` differs "
        f"({'State A leaves it unset' if want is None else 'State A sets it'}):\n"
        f"  state-a:  {want!r}\n  src/core: {have!r}"
    )


# -------------------------------------------------------------------- the two reads

def test_import_error_window_shows_the_imported_file(expected, actual):
    """The caret diagram must be drawn against the imported file's own text.

    `Evaluator#visit` re-reads `err.filename` off disk to set `err.input`, so the
    window shows lines the compiled entry file never contained. The imported
    fixture's first line is deliberately distinctive: if a port left `err.input`
    unset, `formatException` falls back to the entry source and the window becomes
    a plausible-looking excerpt of the wrong file.
    """
    cid = "import-error-shows-imported-source"
    exp, got = _pair(expected, actual, cid)
    exp_err, got_err = _both_failed(exp, got, cid)
    marker = "a distinctive first line nothing else has"
    assert any(marker in l for l in _parts(exp_err).gutter), (
        f"harness: State A's window for {cid} no longer shows the imported file, so "
        "this row is not measuring the re-read."
    )
    assert any(marker in l for l in _parts(got_err).gutter), (
        f"{errors.describe(cid)}\nthe window does not show the imported file's text, so "
        "`err.input` was not re-read through the platform's readFile:\n"
        + "\n".join(_parts(got_err).block)
    )


def test_unreadable_filename_still_produces_a_full_diagnostic(expected, actual):
    """The read failure must be swallowed, not propagated.

    The compiled source is supplied inline while `filename` names nothing on disk,
    so upstream's `try { err.input = readFileSync(...) } catch {}` leaves `input`
    unset and the diagnostic falls back to the compiled string -- complete, with
    the original error intact. A port that let the failure escape reports a file
    error instead of the real one, which is the single most likely way to get this
    wrong when both reads go through one `platform.readFile` helper.
    """
    cid = "unreadable-file-falls-back"
    exp, got = _pair(expected, actual, cid)
    exp_err, got_err = _both_failed(exp, got, cid)
    want_body, have_body = _parts(exp_err).body, _parts(got_err).body
    assert have_body == want_body, (
        f"{errors.describe(cid)}\nthe diagnostic's body differs. If it names a file or "
        "ENOENT, the swallowed read is escaping:\n"
        f"  state-a:  {want_body}\n  src/core: {have_body}"
    )
    assert _parts(got_err).gutter, (
        f"{errors.describe(cid)}\nthe diagnostic has no source window at all, so the "
        "fallback to the compiled string did not happen."
    )


def test_nested_import_keeps_the_innermost_filename(expected, actual):
    """`if (err.filename) throw err` -- the first frame to claim it wins.

    Three files sit between the entry and the frame that claims the name, and each
    one's `visit` sees the error on its way out. The claimant is the built-in `.styl`
    library: the failing call is `darken`, which is defined in `index.styl` rather
    than in JavaScript, so the innermost `.styl` frame is upstream's own library and
    `deep-c.styl` is merely the outermost file that could have overwritten it. What
    the row measures is that none of the three did. A port that assigned
    unconditionally would report the entry file and still pass every single-file
    case in this catalog.

    Stated as "not one of the outer files" rather than as the library's own path.
    The property is the declining, and writing it that way keeps the premise true if
    the fixture is ever pointed at a different built-in.
    """
    cid = "nested-import-error-keeps-innermost"
    exp, got = _pair(expected, actual, cid)
    exp_err, got_err = _both_failed(exp, got, cid)
    want = exp_err.get("filename")
    outer = [errors.by_id(cid).entry, f"{errors.E}/mid-c.styl", f"{errors.E}/deep-c.styl"]
    assert want and want not in outer, (
        f"harness: State A blames {want!r}, which is one of the files the error passes "
        f"through on its way out ({outer}). This row exists to measure that each of "
        "them declines to overwrite a filename already set, and there is nothing left "
        "of that to measure if the outermost one wins."
    )
    assert got_err.get("filename") == want, (
        f"{errors.describe(cid)}\nthe error is attributed to the wrong file:\n"
        f"  state-a:  {want!r}\n  src/core: {got_err.get('filename')!r}"
    )


# ------------------------------------------------------------------ window shape

@pytest.mark.parametrize("cid", errors.context_ids())
def test_window_line_count_matches_state_a(cid, expected, actual):
    """How many source lines the window holds.

    Implied by the gutter row, but separated because it is the one number that
    distinguishes the asymmetric slice from a symmetric one, and a failure here
    reads as "your window is the wrong size" rather than as a wall of text.
    """
    exp, got = _pair(expected, actual, cid)
    exp_err, got_err = _both_failed(exp, got, cid)
    want, have = len(_parts(exp_err).gutter), len(_parts(got_err).gutter)
    assert have == want, (
        f"{errors.describe(cid)}\nthe source window holds {have} lines, State A's holds "
        f"{want}:\n" + "\n".join(_parts(got_err).block)
    )


def test_context_option_does_not_change_the_window(expected, actual):
    """Setting `context` must be inert, because it is inert upstream.

    `Renderer#render`'s catch builds a fresh options object and copies only
    input/filename/lineno/column into it, so `formatException` never sees the
    renderer's `context`. Measured three ways in `harness/errors.py`: both public
    routes are inert and a direct call is not. A port that wired the option
    through would narrow every diagnostic for any user who has it set -- an
    improvement, and a behaviour change.

    Compared within the submission rather than against State A, so it holds
    whatever the port's window size is: the claim is that the option changes
    nothing, not that the window is any particular width.
    """
    plain = "window-mid-file"
    with_opt = "context-option-is-inert"
    _, got_plain = _pair(expected, actual, plain)
    _, got_opt = _pair(expected, actual, with_opt)
    for cid, r in ((plain, got_plain), (with_opt, got_opt)):
        assert not r.ok, f"{errors.describe(cid)}\nsrc/core compiled input State A rejects."
    plain_err, opt_err = got_plain.error or {}, got_opt.error or {}
    n_plain, n_opt = len(_parts(plain_err).gutter), len(_parts(opt_err).gutter)
    assert n_plain and n_opt, (
        "neither diagnostic has a source window, so this row cannot compare them:\n"
        f"  {plain}: {(plain_err.get('message') or '')[:300]!r}\n"
        f"  {with_opt}: {(opt_err.get('message') or '')[:300]!r}"
    )
    assert n_opt == n_plain, (
        f"`context: 2` changed the window from {n_plain} lines to {n_opt}. Upstream "
        "never passes the option through to `formatException`, so setting it must "
        "have no effect -- see the note on `context-option-is-inert`."
    )


# ---------------------------------------------------------------------- the stack

@pytest.mark.parametrize("cid", errors.stack_ids())
def test_stack_frame_count_matches_state_a(cid, expected, actual):
    """How many frames `stylusStack` renders.

    `eval-deep-mixin-stack` has four and a top-level failure has one, so a port
    that pushed or popped its call stack at the wrong moment shows up here even
    when each individual frame is formatted correctly.
    """
    exp, got = _pair(expected, actual, cid)
    exp_err, got_err = _both_failed(exp, got, cid)
    want, have = _parts(exp_err).stack, _parts(got_err).stack
    assert len(have) == len(want), (
        f"{errors.describe(cid)}\nthe stack has {len(have)} frames, State A's has "
        f"{len(want)}:\n  state-a:\n" + "".join(f"    {l}\n" for l in want)
        + "  src/core:\n" + "".join(f"    {l}\n" for l in have)
    )


@pytest.mark.parametrize("cid", errors.stack_ids())
def test_stack_property_matches_the_message_tail(cid, expected, actual):
    """`.stylusStack` and the tail of the message must agree.

    `formatException` appends the property to the text, so they cannot disagree
    upstream. A port that built the message from one source and the property from
    another -- easy when the stack is rendered lazily -- would pass the message row
    and the property row separately while shipping two different answers.
    """
    _, got = _pair(expected, actual, cid)
    assert not got.ok, f"{errors.describe(cid)}\nsrc/core compiled input State A rejects."
    got_err = got.error or {}
    prop = got_err.get("stylusStack")
    assert prop is not None, (
        f"{errors.describe(cid)}\nsrc/core's error carries no `.stylusStack`, but its "
        "message is expected to end with the rendered frames."
    )
    from_message = "\n".join(_parts(got_err).stack)
    assert from_message == prop.rstrip("\n"), (
        f"{errors.describe(cid)}\n`.stylusStack` and the message's frames differ:\n"
        f"  property: {prop!r}\n  message:  {from_message!r}"
    )


# ------------------------------------------------------- harness self-checks
#
# Oracle-only, so they earn no credit -- but each is a premise the rows above
# depend on, and a failure voids the run rather than scoring it.

@pytest.mark.harness
def test_oracle_answered_every_case(expected):
    missing = [c.id for c in errors.cases() if c.id not in expected.results]
    assert not missing, (
        f"the oracle returned no result for {missing}\n"
        f"driver stderr:\n{expected.driver_stderr[-2000:]}"
    )


@pytest.mark.harness
def test_every_case_fails_in_state_a(expected):
    """A case that compiles measures nothing, and would score every port for free.

    This is the check that caught twelve cases when the catalog was first written:
    stylus coerces far more than it rejects -- `1px + #fff` compiles to `#fff` and
    an unknown function passes through as literal CSS -- so a plausible-looking
    broken source is very often not broken at all.
    """
    compiled = [c.id for c in errors.cases() if expected.get(c.id).ok]
    assert not compiled, (
        "these cases compile successfully in State A, so they have no diagnostic to "
        f"compare and every submission would pass their rows: {compiled}"
    )


@pytest.mark.harness
def test_declared_parts_match_state_a(expected):
    """`parts` drives the row axis, so it must still describe State A in both
    directions.

    Under-declaring means a part nobody compares; over-declaring means a row that
    asserts against emptiness and fails every submission for the catalog's error.
    """
    wrong = []
    for c in errors.cases():
        p = _parts(expected.get(c.id).error or {})
        present = tuple(name for name in errors.PART_NAMES if p.get(name))
        if present != c.parts:
            wrong.append(f"  {c.id}:\n    declared: {c.parts}\n    state-a:  {present}")
    assert not wrong, (
        "`parts` no longer matches State A:\n" + "\n".join(wrong)
    )


@pytest.mark.harness
def test_every_message_decomposes_cleanly(expected):
    """The parser must account for every line of every diagnostic.

    If `formatException`'s layout ever changed, the decomposition would silently
    put lines in the wrong bucket and the per-part rows would compare the wrong
    things while still looking green.
    """
    bad = []
    for c in errors.cases():
        msg = (expected.get(c.id).error or {}).get("message") or ""
        p = _parts(expected.get(c.id).error or {})
        lines = msg.split("\n")
        # Reassembled in the order `formatException` writes them: the header, the
        # window with its interleaved caret, the blank that `'\n\n'` leaves, the
        # original message, the frames, then the empties the trailing newlines make.
        rebuilt = [p.header, *p.block, "", *p.body, *p.stack]
        rebuilt += [""] * (len(msg) - len(msg.rstrip("\n")))
        if rebuilt != lines:
            first = next((i for i in range(max(len(rebuilt), len(lines)))
                          if (rebuilt[i:i + 1] or [None]) != (lines[i:i + 1] or [None])), 0)
            bad.append(
                f"  {c.id}: line {first + 1} lands in the wrong bucket\n"
                f"    rebuilt: {(rebuilt[first:first + 1] or ['<missing>'])[0]!r}\n"
                f"    actual:  {(lines[first:first + 1] or ['<missing>'])[0]!r}"
            )
    assert not bad, (
        "the decomposition does not round-trip, so the per-part rows are comparing "
        "mis-bucketed text:\n" + "\n".join(bad)
    )


@pytest.mark.harness
def test_the_four_window_shapes_really_differ(expected):
    """The window cases must produce different windows.

    They exist to pin the asymmetric slice and its two clamps. If they all came
    out the same size -- which is what happened when they were parametrised over
    the inert `context` option -- the dimension would report green while measuring
    one shape four times.
    """
    sizes = {}
    for cid in ("window-mid-file", "window-clamps-at-file-start",
                "window-clamps-at-file-end", "window-whole-short-file"):
        sizes[cid] = len(_parts(expected.get(cid).error or {}).gutter)
    assert len(set(sizes.values())) == len(sizes), (
        f"the window cases no longer produce distinct shapes: {sizes}"
    )


@pytest.mark.harness
def test_default_window_is_four_before_and_three_after(expected):
    """The asymmetry itself, stated as a number.

    `slice(lineno - 4, lineno + 4)` excludes its end index. Asserting it here means
    a change to upstream's window is reported as the one-line fact it is, rather
    than as twenty-five diffs.
    """
    p = _parts(expected.get("window-mid-file").error or {})
    nums = [int(m.group(1)) for l in p.gutter if (m := GUTTER_RE.match(l))]
    reported = int((p.header.rsplit(":", 2))[-2])
    before = [n for n in nums if n < reported]
    after = [n for n in nums if n > reported]
    assert (len(before), len(after)) == (4, 3), (
        f"State A's default window is now {len(before)} lines before and "
        f"{len(after)} after, not 4 and 3: {nums} around line {reported}"
    )


@pytest.mark.harness
def test_context_option_is_inert_in_state_a(expected):
    """The premise of `test_context_option_does_not_change_the_window`.

    If upstream ever started honouring the option, that row would be asserting a
    behaviour State A no longer has, and would fail correct ports.
    """
    n_plain = len(_parts(expected.get("window-mid-file").error or {}).gutter)
    n_opt = len(_parts(expected.get("context-option-is-inert").error or {}).gutter)
    assert n_plain == n_opt, (
        f"State A now honours `context`: the window is {n_plain} lines without it "
        f"and {n_opt} with `context: 2`. The inertness row must be retired."
    )


@pytest.mark.harness
def test_import_cases_blame_the_imported_file(expected):
    """Each import case must be attributed to its import, not to the entry.

    These rows measure the re-read and the first-filename-wins rule, both of which
    are invisible if the error is attributed to the entry file.
    """
    wrong = []
    for cid in errors.import_ids():
        fn = (expected.get(cid).error or {}).get("filename")
        if fn is not None and fn.endswith(f"/{cid}.styl"):
            wrong.append(f"  {cid}: blamed on the entry file")
    assert not wrong, (
        "State A now attributes these to the entry file rather than the import:\n"
        + "\n".join(wrong)
    )


@pytest.mark.harness
def test_some_case_has_a_multi_frame_stack(expected):
    """At least one case must render more than one frame.

    Every `stylusStack` could be a single frame -- for a top-level failure that is
    the honest rendering -- and then the frame-count row would check nothing about
    a port's call-stack bookkeeping.
    """
    deep = [cid for cid in errors.stack_ids()
            if len(_parts(expected.get(cid).error or {}).stack) > 1]
    assert deep, (
        "no case renders a multi-frame stylusStack, so frame ordering and the "
        "separator are unmeasured."
    )


@pytest.mark.harness
def test_some_caret_is_not_at_the_end_of_its_window(expected):
    """At least one case must have the caret interleaved.

    Malformed input usually fails at `eos`, which puts the caret on the last line
    of the window -- where a port that always appended it would look correct.
    `test_caret_attaches_to_the_reported_line` needs at least one case where that
    shortcut is wrong.
    """
    interleaved = []
    for c in errors.cases():
        p = _parts(expected.get(c.id).error or {})
        if not p.block:
            continue
        idx = [i for i, l in enumerate(p.block) if CARET_RE.match(l)]
        if idx and idx[0] != len(p.block) - 1:
            interleaved.append(c.id)
    assert interleaved, (
        "every caret sits on the last line of its window, so a port that always "
        "appends the caret would pass the interleaving row."
    )


@pytest.mark.harness
def test_body_messages_are_not_all_identical(expected):
    """The body row must compare more than one string.

    Several cases legitimately share a message -- three built-ins raise the same
    type error -- but if every case did, the body row would be one assertion
    repeated twenty-five times.
    """
    bodies = {"\n".join(_parts(expected.get(c.id).error or {}).body) for c in errors.cases()}
    assert len(bodies) >= 5, (
        f"only {len(bodies)} distinct body messages across {len(errors.cases())} cases: "
        f"{sorted(bodies)}"
    )


@pytest.mark.harness
def test_every_source_is_distinct():
    """Identical sources share a parse, and the second inherits the first's filename.

    `Parser.cache` is static and keyed on `sha1(source + prefix)`. For this
    dimension that would mean inheriting the wrong `header`, which is the first
    thing every row compares.
    """
    seen: dict[str, str] = {}
    clashes = []
    for c in errors.cases():
        if c.source in seen:
            clashes.append(f"{c.id} and {seen[c.source]}")
        seen[c.source] = c.id
    assert not clashes, f"these cases share source text: {clashes}"


@pytest.mark.harness
def test_no_case_leaks_a_host_path():
    """Every path in the catalog is virtual.

    A real one would mean a case was written against the build host, and it would
    reach the comparison through the header.
    """
    bad = []
    for c in errors.cases():
        candidates = [c.entry, *c.files, *c.paths, str(c.settings.get("filename", ""))]
        for text in candidates:
            if text.startswith("/") and not text.startswith(layout.VPROJ):
                bad.append(f"{c.id}: {text}")
    assert not bad, f"non-virtual absolute paths in the catalog: {bad}"


@pytest.mark.harness
def test_the_unreadable_case_names_nothing_on_disk():
    """`unreadable-file-falls-back` depends on its filename being absent.

    If some other case's tree happened to create that path, the read would succeed
    and the case would measure the ordinary import path instead of the swallowed
    failure.
    """
    target = errors.by_id("unreadable-file-falls-back").settings["filename"]
    assert target not in errors.all_files(), (
        f"{target} is materialised by the catalog, so the read this case needs to "
        "fail would succeed."
    )
    on_disk = errors.scratch_root() / target[len(errors.E):].lstrip("/")
    assert not on_disk.exists(), f"{on_disk} exists on disk; the case cannot fail its read."
