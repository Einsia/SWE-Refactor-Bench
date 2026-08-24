"""`bin/stylus` as a black box: same argv on both sides, same everything out.

This is the only dimension that runs the submission as a process rather than
loading its modules.  It is also the one the port is least tempted to touch, which
is exactly why it is checked: `bin/stylus` is the published release artifact, so a
port that quietly changed an exit code or moved usage text from stderr to stdout
has broken installed users while every corpus row still passes.

What each case compares, and what it deliberately does not:

  * **exit status** -- always.
  * **stdout** -- as text, after the scratch directory and both implementation
    trees are rewritten to fixed tokens (`harness/cli.py _revirt`).
  * **files written** -- as a set of names, then one row per file for its contents.
  * **stderr** -- reduced to the Stylus diagnostic for a failing case, because
    upstream fails by throwing and V8 wraps the diagnosis in a preamble naming the
    absolute path of `bin/stylus` and the Node version.  For a case that exits 0
    while writing to stderr -- `--help` -- the full text is compared, since there
    is no crash framing to strip.

Three measurements shaped the catalog more than any design choice:

  * multi-file stdout is racy, so no row compares it;
  * the `compiled`/`generated` progress notices are racy *even for one input*, so
    those cases compare them as a sorted set;
  * `--line-numbers` and `--firebug` annotate the built-in `.styl` library's own
    path, which instruction.md 1.4 does not pin, so that one path is collapsed
    while the project's file paths stay exact.

`writes`, `exit_code` and `explains` are declared on each case rather than read from
the oracle at collection time, so the row count is fixed independently of the run --
`test_declared_writes_match_state_a` is what keeps all three declarations honest.

Nothing here skips.  Every row is collected off those declarations, so the states a
skip would have to cover -- State A not writing a file the catalog names, writing one
it does not, producing no error line for a case declared failing, or carrying no
Stylus explanation -- are either impossible or a catalog that needs re-measuring, and
they say so as an assertion.  The reason to care: a skip is charged 0 rather than
leaving the denominator, so a row excused on every submission would cost each of them
the same check for a question none of them was asked.
"""
from __future__ import annotations

import pytest

from harness import cli as C


# --------------------------------------------------------------------- fixtures


@pytest.fixture(scope="module")
def expected():
    """State A's answers.  Computed here, at verify time, from the pristine copy."""
    return C.run_all(C.CASES, C.oracle_bin(), "oracle")


@pytest.fixture(scope="module")
def actual():
    """The submission's answers, or a marker that there is no CLI to run."""
    if not C.submission_bin().exists():
        return None
    return C.run_all(C.CASES, C.submission_bin(), "submission")


def _trim(text: str | None, limit: int = 700) -> str:
    if text is None:
        return "<none>"
    if not text.strip():
        return "<empty>"
    one = text.strip()
    return one if len(one) <= limit else f"{one[:limit]}... ({len(one)} chars)"


def _got(actual, cid: str) -> C.CliResult:
    """The submission's result, or a failed row if `bin/stylus` is not there.

    An assertion and not a skip: instruction.md 1.6 requires the executable to
    still exist, so its absence is a behavioural failure in every row that needed
    it.  Excusing them would score a submission that deleted the CLI above one
    that kept it and got a flag wrong.
    """
    assert actual is not None, (
        f"{cid}: {C.submission_bin()} does not exist. instruction.md 1.6 requires "
        f"bin/stylus to remain an executable client of src/node/."
    )
    res = actual.get(cid)
    assert not res.timed_out, (
        f"{cid}: bin/stylus did not exit within {C.TIMEOUT_S}s\n"
        f"  invocation: {C.describe()[cid]}"
    )
    return res


def _diag_or_full(res: C.CliResult, case: C.CliCase) -> str:
    """What of stderr is comparable for this case.

    A failing case is compared on its diagnostic alone; anything that exits 0 and
    still wrote to stderr gets compared in full.  See the module docstring.
    """
    return C.diagnostic(res.stderr) if case.fails else res.stderr


# ------------------------------------------------------------------ exit status


@pytest.mark.parametrize("cid", C.IDS)
def test_exit_status_matches_state_a(cid, expected, actual):
    """The status an installed user's shell sees."""
    case = C.by_id(cid)
    exp, got = expected.get(cid), _got(actual, cid)
    assert not exp.timed_out, f"oracle timed out on {cid}, which should not happen"
    assert got.code == exp.code, (
        f"{cid}: exit status differs\n"
        f"  invocation: {C.describe()[cid]}\n"
        f"  state-a:    {exp.code}\n"
        f"  submission: {got.code}\n"
        f"  its stderr: {_trim(got.stderr, 400)}"
    )


# ----------------------------------------------------------------------- stdout


@pytest.mark.parametrize("cid", [c.id for c in C.CASES if not c.fails])
def test_stdout_matches_state_a(cid, expected, actual):
    """Byte-for-byte, for every case State A completes.

    Only successful cases: a failing one prints nothing to stdout, so comparing it
    would be a row every broken CLI passes.  Their diagnostics are compared below
    instead.
    """
    case = C.by_id(cid)
    exp, got = expected.get(cid), _got(actual, cid)
    assert got.stdout == exp.stdout, (
        f"{cid}: stdout differs\n"
        f"  invocation: {C.describe()[cid]}\n"
        f"  state-a:    {_trim(exp.stdout)}\n"
        f"  submission: {_trim(got.stdout)}"
    )


# ------------------------------------------------------------------ files written


@pytest.mark.parametrize("cid", [c.id for c in C.CASES if c.writes])
def test_files_written_match_state_a(cid, expected, actual):
    """The *names*, as a set -- right content in the wrong place is still wrong.

    Restricted to cases State A writes something in, so no row here is passed by a
    CLI that writes nothing.  That a *successful* case wrote nothing extra is
    checked separately, below.
    """
    exp, got = expected.get(cid), _got(actual, cid)
    assert set(got.produced) == set(exp.produced), (
        f"{cid}: different files on disk afterwards\n"
        f"  invocation: {C.describe()[cid]}\n"
        f"  state-a:    {sorted(exp.produced) or '<none>'}\n"
        f"  submission: {sorted(got.produced) or '<none>'}\n"
        f"  missing:    {sorted(set(exp.produced) - set(got.produced)) or '<none>'}\n"
        f"  unexpected: {sorted(set(got.produced) - set(exp.produced)) or '<none>'}"
    )


@pytest.mark.parametrize(
    "cid,name",
    [(c.id, n) for c in C.CASES for n in c.writes],
    ids=[f"{c.id}::{n}" for c in C.CASES for n in c.writes],
)
def test_written_file_content_matches_state_a(cid, name, expected, actual):
    """One row per file State A writes, so a wrong `.map` is not hidden by a right `.css`."""
    exp, got = expected.get(cid), _got(actual, cid)
    assert name in exp.produced, (
        f"harness: the catalog declares {cid} writes {name}, and State A here did "
        f"not -- it wrote {sorted(exp.produced) or '<none>'}. Re-measure the catalog; "
        f"this row is collected off that declaration."
    )
    assert name in got.produced, (
        f"{cid}: {name} was not written\n"
        f"  invocation: {C.describe()[cid]}\n"
        f"  on disk:    {sorted(got.produced) or '<none>'}"
    )
    assert got.produced[name] == exp.produced[name], (
        f"{cid}: {name} differs from State A\n"
        f"  invocation: {C.describe()[cid]}\n"
        f"  state-a:    {_trim(exp.produced[name])}\n"
        f"  submission: {_trim(got.produced[name])}"
    )


@pytest.mark.parametrize("cid", [c.id for c in C.CASES if not c.fails and not c.writes])
def test_nothing_is_written_when_state_a_writes_nothing(cid, expected, actual):
    """A `--print` or stdin case must not leave files behind.

    The mirror of the row above, and the reason `--print` cases are not simply left
    unchecked: a port that helpfully wrote `in.css` as well as printing it would
    surprise every script that pipes the output.
    """
    exp, got = expected.get(cid), _got(actual, cid)
    assert not exp.produced, (
        f"harness: the catalog declares {cid} writes nothing, and State A here wrote "
        f"{sorted(exp.produced)}. Re-measure the catalog; this row is collected off "
        f"that declaration."
    )
    assert not got.produced, (
        f"{cid}: files were written where State A wrote none\n"
        f"  invocation: {C.describe()[cid]}\n"
        f"  unexpected: {sorted(got.produced)}"
    )


# ------------------------------------------------------------------------ stderr


@pytest.mark.parametrize("cid", [c.id for c in C.CASES if c.fails])
def test_failure_diagnostic_matches_state_a(cid, expected, actual):
    """The error line: type, file, line, column.

    The V8 preamble and the stack are dropped -- see `harness/cli.diagnostic`.  What
    is left is the sentence a user reads to find their mistake, and it has to be
    the same sentence.
    """
    exp, got = expected.get(cid), _got(actual, cid)
    want = C.diagnostic(exp.stderr)
    assert want, (
        f"harness: the catalog declares {cid} exits {C.by_id(cid).exit_code}, and "
        f"State A here produced no recognisable error line. Re-measure the catalog.\n"
        f"  stderr: {_trim(exp.stderr, 300)}"
    )
    assert C.diagnostic(got.stderr) == want, (
        f"{cid}: the diagnostic differs\n"
        f"  invocation: {C.describe()[cid]}\n"
        f"  state-a:    {want}\n"
        f"  submission: {C.diagnostic(got.stderr) or '<none found>'}\n"
        f"  raw stderr: {_trim(got.stderr, 500)}"
    )


@pytest.mark.parametrize("cid", [c.id for c in C.CASES if c.fails and c.explains])
def test_failure_explanation_matches_state_a(cid, expected, actual):
    """And the source excerpt and reason under it, where Stylus wrote one.

    `expected ")", got "outdent"` is the half of a `ParseError` that says what to
    do about it.  Collected over the failing cases that have one: `explains=False`
    marks the two whose error is a bare Node object -- an ENOENT, printed by
    `util.inspect` -- where there is no Stylus text to reproduce and the error line
    `test_failure_diagnostic_matches_state_a` compares is the whole contract.
    """
    exp, got = expected.get(cid), _got(actual, cid)
    want = C.message(exp.stderr)
    assert want, f"harness: State A's error for {cid} carries no Stylus explanation"
    assert C.message(got.stderr) == want, (
        f"{cid}: the explanation under the diagnostic differs\n"
        f"  invocation: {C.describe()[cid]}\n"
        f"  state-a:\n    " + want.replace("\n", "\n    ") + "\n"
        f"  submission:\n    " + (C.message(got.stderr) or "<none found>").replace("\n", "\n    ")
    )


@pytest.mark.parametrize("cid", [c.id for c in C.CASES if not c.fails])
def test_stderr_matches_state_a_on_success(cid, expected, actual):
    """Which stream the output went to.

    `--help` writes usage to stderr and exits 0; `--version` writes to stdout.  That
    asymmetry is easy to tidy up by accident and impossible for a user to notice
    until a script that greps one of them stops working.  Successful cases have no
    crash framing, so this compares the text as it stands.
    """
    exp, got = expected.get(cid), _got(actual, cid)
    assert got.stderr == exp.stderr, (
        f"{cid}: stderr differs on a successful run\n"
        f"  invocation: {C.describe()[cid]}\n"
        f"  state-a:    {_trim(exp.stderr)}\n"
        f"  submission: {_trim(got.stderr)}"
    )


# --------------------------------------------------------------- self-checks
#
# These consult only the oracle.  They earn no credit -- a submission cannot
# influence them, so paying for them would be a floor every submission collects --
# but a failure voids the run, because each one is a premise the rows above rely on.


@pytest.mark.harness
@pytest.mark.parametrize("cid", C.IDS)
def test_declared_writes_match_state_a(cid, expected):
    """The catalog's `writes=`/`exit_code=` are still what State A does.

    The declarations exist to keep the row count independent of the run.  That is
    only safe while they are true: a case that started failing, or stopped writing
    its `.map`, would otherwise quietly drop its rows.  Checked against the oracle
    on every run so the two cannot drift.
    """
    case, exp = C.by_id(cid), expected.get(cid)
    assert exp.code == case.exit_code, (
        f"{cid}: the catalog declares exit_code={case.exit_code} but State A here "
        f"returned {exp.code}. Re-measure the catalog.\n"
        f"  stderr: {_trim(exp.stderr, 300)}"
    )
    assert set(exp.produced) == set(case.writes), (
        f"{cid}: the catalog declares writes={sorted(case.writes)} but State A here "
        f"wrote {sorted(exp.produced)}. Re-measure the catalog."
    )
    if case.fails:
        assert bool(C.message(exp.stderr)) == case.explains, (
            f"{cid}: the catalog declares explains={case.explains} but State A here "
            f"{'wrote' if C.message(exp.stderr) else 'wrote no'} Stylus explanation "
            f"under its error line. Re-measure the catalog -- "
            f"`test_failure_explanation_matches_state_a` is collected off this.\n"
            f"  stderr: {_trim(exp.stderr, 300)}"
        )


@pytest.mark.harness
def test_oracle_ran_every_case(expected):
    """No case timed out or went missing on the oracle side."""
    missing = [c.id for c in C.CASES if c.id not in expected]
    assert not missing, f"the oracle produced no result for: {missing}"
    stuck = [c.id for c in C.CASES if expected.get(c.id).timed_out]
    assert not stuck, f"the oracle timed out on: {stuck} -- these cases are unusable"


@pytest.mark.harness
def test_cases_that_should_fail_do_fail_in_state_a(expected):
    """Every `error-` case really is an error, and no other case is.

    A case named for a failure that State A happily compiles would make the
    diagnostic rows above vacuous, and a case that fails by accident would silently
    stop testing whatever it was written for.
    """
    named = {c.id for c in C.CASES if c.id.startswith("error-")}
    failing = {c.id for c in C.CASES if expected.get(c.id).code != 0}
    assert named == failing, (
        "the `error-` prefix and actual failure disagree:\n"
        f"  named error- but succeed: {sorted(named - failing)}\n"
        f"  fail but not named:       {sorted(failing - named)}"
    )


@pytest.mark.harness
def test_flag_pairs_that_should_differ_do_differ(expected):
    """Each flag actually changes State A's output.

    Without this a port could ignore `--compress` entirely and still pass
    `compress-print`, provided its plain output happened to match.  Pairs are
    (with-flag, without-flag) over the same input.
    """
    pairs = [
        ("compress-print", "print-basic", "--compress"),
        ("linenos", "print-nested", "--line-numbers"),
        ("firebug", "print-nested", "--firebug"),
        ("prefix", "print-basic", "--prefix"),
        ("sourcemap-print", "print-basic", "--sourcemap"),
        ("sourcemap-inline", "print-basic", "--sourcemap-inline"),
        ("include-css", "css-import-untouched", "--include-css"),
        ("hoist-atrules", "print-basic", "--hoist-atrules"),
        ("compare-stdin", "stdin-basic", "--compare"),
        ("write-ext", "write-default", "--ext"),
        ("resolve-url-nocheck", "resolve-url", "--resolve-url-nocheck"),
        ("sourcemap-root-kept", "sourcemap-root-clobbered", "--sourcemap-root order"),
    ]
    same = []
    for with_flag, without, label in pairs:
        a, b = expected.get(with_flag), expected.get(without)
        if (a.stdout, sorted(a.produced.items())) == (b.stdout, sorted(b.produced.items())):
            same.append(f"{label}: {with_flag} == {without}")
    assert not same, (
        "these flags made no difference to State A, so the rows testing them are "
        "vacuous:\n  " + "\n  ".join(same)
    )


@pytest.mark.harness
def test_instruction_flags_are_all_exercised():
    """Every flag instruction.md 1.6 promises appears in some case's argv.

    A promise in the brief with no row behind it is a thing the submission is told
    to get right and never asked about.  `--watch` and `--interactive` are the
    documented exceptions: one never exits and the other wants a TTY.
    """
    untestable = {"-w", "--watch", "-i", "--interactive", "-u", "--use"}
    promised = {
        "-c", "--compress", "-o", "--out", "-I", "--include", "-U", "--inline",
        "-m", "--sourcemap", "--sourcemap-inline", "--sourcemap-root",
        "--sourcemap-base", "-p", "--print", "--resolve-url", "--include-css",
        "--hoist-atrules", "--prefix", "--css", "-V", "--version", "-h", "--help",
    } - untestable
    seen = {arg for c in C.CASES for arg in c.argv}
    missing = sorted(promised - seen)
    assert not missing, (
        f"instruction.md 1.6 promises these flags and no case passes them: {missing}"
    )


@pytest.mark.harness
def test_progress_cases_really_are_racy_enough_to_need_sorting(expected):
    """The `progress` flag is only on cases whose stdout is notices, not CSS.

    If a case marked `progress` actually printed CSS, sorting its lines would
    scramble the stylesheet and compare a meaningless permutation -- passing ports
    that emit the right lines in the wrong order.  Notices all start with two
    spaces and a verb; CSS does not.
    """
    wrong = []
    for c in C.CASES:
        if not c.progress:
            continue
        out = expected.get(c.id).stdout
        for line in out.splitlines():
            if line.strip() and not line.split()[0] in ("compiled", "generated", "watching"):
                wrong.append(f"{c.id}: {line!r}")
    assert not wrong, (
        "these cases are marked `progress` -- which sorts their stdout -- but their "
        "stdout is not made of progress notices:\n  " + "\n  ".join(wrong)
    )


@pytest.mark.harness
def test_no_case_leaks_an_absolute_path(expected):
    """Nothing in the oracle's output still names a real directory.

    Every such path differs between the two sides for reasons the port does not
    control, so one that escaped `_revirt` would fail every submission on the spot.
    """
    from harness import layout

    reals = [str(layout.STATE_A), str(layout.REPO), str(layout.WORK)]
    leaks = []
    for c in C.CASES:
        r = expected.get(c.id)
        blob = r.stdout + "\n" + r.stderr + "\n" + "\n".join(r.produced.values())
        for real in reals:
            if real and real != "/" and real in blob:
                leaks.append(f"{c.id}: {real}")
            esc = C._css_escape(real)
            if real and real != "/" and esc in blob:
                leaks.append(f"{c.id}: {real} (css-escaped)")
    assert not leaks, (
        "these outputs still contain a real absolute path, so the comparison would "
        "fail on location rather than behaviour:\n  " + "\n  ".join(sorted(set(leaks)))
    )
