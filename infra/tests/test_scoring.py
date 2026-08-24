"""The ladder, tested at each rung.

These are the numbers that get published, so the properties that matter are the
ones that would be embarrassing to get wrong: a cheating submission scores zero, a
broken harness does not score anything at all, and an honest partial migration is
told how much of the suite it passed even though the stage pays it nothing.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from swerefactor.config import ScoringPolicy
from swerefactor.report import render
from swerefactor.result import Check, StageResult, Unit, pooled, rate
from swerefactor.scoring import Verdict, grade

POLICY = ScoringPolicy()


def audit(**gates: str) -> StageResult:
    """``audit(no_shim="pass", deps_gone="fail")``."""
    res = StageResult(stage="audit", task="t", status="ok")
    for gate, verdict in gates.items():
        res.add(Check(id=gate, verdict=verdict, summary=verdict, required=True))
    return res


def behavioural(*modules: tuple[str, float, float],
               checks: int = 10) -> StageResult:
    """Each module as ``(id, weight, rate)``, expanded into `checks` checks.

    Ten by default, because most tests here are about which rule fired and not
    about a figure.  `checks` is a parameter because payment cannot tell one near
    miss from another -- every rate below 1.0 is paid the same zero -- so the tests
    that distinguish a tree one check short from a tree that failed most of the
    suite do it on the measured rate, and ten checks cannot express 0.9995.
    """
    res = StageResult(stage="behavioural", task="t", status="ok")
    for mid, weight, module_rate in modules:
        res.units.append(Unit(id=mid, title=mid, weight=weight, status="ok"))
        passing = round(module_rate * checks)
        for i in range(checks):
            res.add(Check(id=f"{mid}/c{i}", unit=mid,
                          verdict="pass" if i < passing else "fail",
                          summary=""))
    return res


def verification(survived: int, broke: int = 0, errored: int = 0) -> StageResult:
    res = StageResult(stage="verification", task="t", status="ok")
    n = 0
    for verdict, count in (("pass", survived), ("fail", broke), ("error", errored)):
        for _ in range(count):
            n += 1
            res.add(Check(id=f"adv{n}", verdict=verdict, summary="",
                          metadata={"kind": "round"}))
    return res


# --------------------------------------------------------------------------- #
# stage 1 is a gate
# --------------------------------------------------------------------------- #


def test_failed_gate_scores_zero_and_stops_the_ladder():
    v = grade("t", POLICY, audit(shim="fail"), behavioural(("m", 1, 1.0)),
              verification(6))
    assert v.score == 0
    assert v.audit_gate == "fail"
    assert v.blocked_by == "audit-gate"
    # The later stages did run here -- the point is that their results are not
    # allowed to buy anything back.
    assert v.behavioural_points == 0
    assert v.verification_points == 0


def test_an_uncredited_reading_reports_the_measurement_not_the_policy():
    """The dict has to carry the rate, because the payment cannot carry anything.

    Stage 2 pays all or nothing, so `points` here is 0.00 for a suite that passed
    0.95 of its weighted checks and 0.00 for one that passed none.  A reading built
    from the payment is therefore the same discarding this dict exists to stop, one
    level down: it would file both as the same run.

    `parser` at 0.9 rather than 0.0 is the whole point of the fixture -- a rate
    strictly between the two ends is what tells a recomputed measurement from a
    field read off a verdict where every number is 0.
    """
    from swerefactor.report import render

    func = behavioural(("build", 0.5, 1.0), ("parser", 0.5, 0.9))
    v = grade("t", POLICY, audit(honest="fail"), func)

    seen = v.uncredited["behavioural"]
    # The measurement, recomputed from the module rows: (1.0 + 0.9) / 2.
    assert seen["rate"] == 0.95
    # And the award, which is a zero twice over -- the gate refused it, and stage 2
    # would not have paid it either.  Asserted beside the rate, because the pair is
    # what makes the rate worth recording.
    assert seen["points"] == 0.0
    assert seen["complete"] is False
    assert v.behavioural_points == 0, "the credited score must stay at zero"

    text = render(v)
    assert "rate 0.9500 measured, not credited" in text


def test_an_uncredited_reading_says_when_stage_2_also_stopped_itself():
    """The second stop, which only a verdict read from disk can carry.

    A submission can fail stage 1 *and* be stopped by stage 2 on stage 2's own
    account -- a module that did not pass every scored check being the way.
    Reporting only "stage 1 failed, so this earns nothing" invites the inference that
    fixing the honesty problem turns this into 40 points, when two separate gates are
    shut with two separate fixes.

    Which is why `behavioural-incomplete` is not on the report's exclusion list: it is
    a stop stage 2 owns, unlike the audit ids, which the line above the table has
    already printed.  The verdict is built the way one read from disk arrives: by
    putting the id in the uncredited dict, which is what `Verdict.from_dict` does
    when it reads one.
    """
    from swerefactor.report import render

    func = behavioural(("build", 0.5, 0.0), ("parser", 0.5, 1.0))
    v = grade("t", POLICY, audit(honest="fail"), func)

    assert v.blocked_by == "audit-gate", "the gate still owns the verdict"
    # Stage 2's own stop, announced as one, and named rather than left to the id.
    assert v.uncredited["behavioural"]["blocked_by"] == "behavioural-incomplete"
    fresh = render(v)
    assert "stage 2 also stopped on its own account" in fresh
    assert "did not pass every scored check" in fresh
    assert "behavioural-incomplete" not in fresh, (
        "the id reached the reader instead of a sentence")

    # And the gate's own stop is not repeated here as though stage 2 had set it.
    v.uncredited["behavioural"]["blocked_by"] = "audit-gate"
    assert "stage 2 also stopped on its own account" not in render(v)


def test_a_behavioural_stage_that_ran_is_not_reported_as_not_run():
    """A failed gate stops the ladder; it does not unrun what already ran.

    The stages are separately runnable, which is how a task is authored and how a
    failing gate is investigated, so a behavioural result can be on disk when the
    gate fails.  ``grade()`` returned before reading it and the report then said
    NOT RUN about a stage that had measured every check it has -- discarding the
    only evidence of what the submission *does*, which is a different question
    from whether it is honest and has a different fix.
    """
    from swerefactor.report import render

    v = grade("t", POLICY, audit(shim="fail"), behavioural(("m", 1, 0.8)))
    assert v.score == 0, "the gate still decides the score"
    assert v.behavioural_points == 0, "nothing may be earned past a failed gate"
    assert v.behavioural_rate == 0, "the credited rate must not carry a reading"

    seen = v.uncredited["behavioural"]
    assert seen["rate"] == pytest.approx(0.8)
    # `would_be_points` is what the stage would have been PAID, which is a zero: a
    # module at 0.8 did not pass every scored check.  Asserted because it is the
    # field whose zero could be read as "nothing ran", and the two above are what
    # make it readable as an award instead -- 0.8 and 0.05 are the same 0.00 points,
    # and the rate is where the difference lives.
    assert seen["would_be_points"] == 0.0
    assert seen["complete"] is False
    assert [m["id"] for m in seen["modules"]] == ["m"]

    text = render(v)
    assert "NOT RUN" not in text.split("stage 3")[0], (
        "stage 2 ran; the report must not say otherwise")
    assert "not credited" in text
    assert "0.8000" in text

    # The gate's own note is printed under that table.  Whether a later stage
    # ran is the operator's business; what the gate decides is that nothing it
    # measured can be credited.
    note = next(n for n in v.notes if "audit gate failed" in n)
    assert "no further stage was run" not in note
    assert "no later stage can earn anything" in note


def test_a_behavioural_stage_that_is_absent_still_says_not_run_and_why():
    """The other half: absent is absent, and the reason belongs on the line.

    Stage 3's NOT RUN line has always named its reason.  Stage 2's did not, so a
    reader of a gate failure got "NOT RUN" with no indication of whether the
    stage was skipped by policy or had fallen over.
    """
    from swerefactor.report import render

    v = grade("t", POLICY, audit(shim="fail"), None)
    assert not v.uncredited
    line = next(l for l in render(v).splitlines() if "stage 2" in l)
    assert "NOT RUN" in line
    assert "the audit gate failed" in line


def test_reading_an_uncreditable_stage_cannot_change_a_score(monkeypatch):
    """The reading is taken after the score is decided, so it must not raise.

    A crash here would turn a decided 0 into a harness error -- a worse outcome
    than the missing line it was added to fix.
    """
    from swerefactor import scoring

    def explode(*_a, **_k):
        raise RuntimeError("a stage result the scorer cannot parse")

    monkeypatch.setattr(scoring, "grade_behavioural", explode)
    v = scoring.grade("t", POLICY, audit(shim="fail"), behavioural(("m", 1, 1.0)))
    assert v.score == 0
    assert v.audit_gate == "fail"
    assert v.valid, "a failed reading must not invalidate a decided gate"
    assert "could not be read" in v.uncredited["behavioural"]["note"]


def test_an_uncredited_reading_survives_the_json_round_trip():
    """`score` writes the JSON and the report is re-rendered from it elsewhere."""
    from swerefactor.scoring import Verdict

    v = grade("t", POLICY, audit(shim="fail"), behavioural(("m", 1, 0.8)))
    back = Verdict.from_dict(v.to_dict())
    assert back.uncredited["behavioural"]["rate"] == pytest.approx(0.8)


def test_the_uncredited_note_names_which_figure_is_which():
    """The note carries a rate and an award, and each has to be labelled as one.

    It said "measured 0.00 of its points at a 0.9676 pass rate" on a real fw02
    verdict, and that sentence has two problems at once: it calls an award a
    measurement, and its own numbers do not reconcile in any reading a checker could
    find -- which is a bad property in a note that exists precisely so a zero can be
    attributed.  Under an all-or-nothing stage the numbers *cannot* reconcile, since
    the rate does not price the payment, so the fix is to say which is which.

    Asserted on the pair rather than on arithmetic between them: a rate strictly
    inside (0, 1) beside a paid 0.00, with each labelled.
    """
    v = grade("t", POLICY, audit(shim="fail"),
              behavioural(("a", 0.5, 0.99), ("b", 0.5, 1.0), checks=100))
    note = next(n for n in v.notes if "stage behavioural ran" in n)

    seen = v.uncredited["behavioural"]
    rate, paid = seen["rate"], seen["points"]
    assert 0.0 < rate < 1.0, "the fixture has to measure something to report"
    assert paid == 0.0

    assert f"{rate:.4f} weighted pass rate" in note
    assert f"worth {paid:.2f} points" in note
    # And the award is never called a measurement, which is the whole finding.
    assert f"measured {paid:.2f} of its points" not in note


def test_a_stage_that_measures_no_rate_keeps_the_one_figure_note():
    """Stage 3 has no rate, so its note carries a figure and nothing to label.

    Stage 2 states both because they no longer determine each other; stage 3 counts
    survivors, where the count and the payment are the same fact and a second clause
    would be a distinction that is not there.  Kept as a test because the wording is
    chosen per stage in one expression, so the two branches are one edit apart.
    """
    v = grade("t", POLICY, audit(shim="fail"), behavioural(("m", 1, 1.0)),
              verification(6))
    note = next(n for n in v.notes if "stage verification ran" in n)
    assert "measured 60.00 of its points" in note
    assert "pass rate" not in note


# --------------------------------------------------------------------------- #
# a stage that ran must not be reported NOT RUN -- the other branch's tests
# --------------------------------------------------------------------------- #
# Same finding as the `uncredited` tests above, measured from the other end and
# asserted on the other field, so both are kept.  The header wording is unified
# on "RAN, NOT COUNTED": these arrived saying "RAN, NOT SCORED", which no report
# site emits, and two spellings for one state in one report is exactly the drift
# `Verdict.uncredited`'s docstring warns about.

def test_a_stage_that_ran_is_not_reported_as_not_run():
    """A discarded stage result is a third case, and the report must say which.

    Measured on a real fw06 submission: stage 2 passed every scored check with zero
    failures, stage 1 failed one required gate, and report.txt printed
    "stage 2 behavioural modules ..... NOT RUN" beside a behavioural.json holding
    9/9 modules ok.  The score was right and the sentence was false -- and it was
    the one sentence that mattered, because "behaviourally perfect, structurally
    rejected" is the whole finding about that tree.

    Asserted through the RENDERED text, not the field: a note that is written and
    never printed reads identically to no note at all.
    """
    v = grade("t", POLICY, audit(shim="fail"), behavioural(("m", 1, 1.0)),
              verification(6))
    text = render(v)

    assert "NOT RUN" not in text.split("stage 2")[1].split("stage 3")[0]
    assert "RAN, NOT COUNTED" in text
    assert "9 check(s)" not in text          # the summary is stage 2's own count
    assert "10 check(s), 0 failed" in text
    assert "pass rate 1.0000 over its scored checks" in text
    # And the arithmetic is untouched: the fix is reporting-only.
    assert v.score == 0
    assert "score = 0.00" in text


def test_an_absent_stage_is_still_reported_as_not_run():
    """The distinction only works if the other side still reads NOT RUN."""
    v = grade("t", POLICY, audit(shim="fail"), None, None)
    text = render(v)
    assert "stage 2  behavioural modules ............. NOT RUN" in text
    assert "RAN, NOT COUNTED" not in text
    assert v.unscored_stages == {}


def test_an_unscored_stage_that_did_not_complete_says_so():
    broken = behavioural(("m", 1, 1.0))
    broken.status = "error"
    v = grade("t", POLICY, audit(shim="fail"), broken)
    assert "did not complete" in v.unscored_stages["behavioural"]
    assert "status=error" in render(v)
    assert v.score == 0


def test_undecided_gate_is_invalid_rather_than_zero():
    """A verifier that could not decide has learned nothing about the submission.

    Scoring that as a cheating submission would turn every model outage into a
    published zero, so it is marked invalid and asks to be re-run.
    """
    v = grade("t", POLICY, audit(shim="error"), behavioural(("m", 1, 1.0)))
    assert not v.valid
    assert v.harness_error
    assert v.audit_gate == "not-run"
    assert v.blocked_by == "audit-undecided"


def test_a_decided_failure_outranks_an_undecided_gate():
    """One undecided gate does not turn a caught cheat into a harness outage.

    The gate is a conjunction, so a required check that failed on grounded
    evidence has settled it -- no re-run of some *other* undecided check can make
    the conjunction true.  Reporting "the grader could not decide" over the top of
    that hides the finding and asks for a re-run with nothing to compute.  This is
    the pf01 State A control: three gates failed unanimously, the reviews split on
    a fourth, and the report has to say the tree was not migrated.
    """
    v = grade("t", POLICY,
              audit(posix_layer_retired="fail",
                        wasi_platform_layer_is_real="fail",
                        no_host_escape="error",
                        engine_untouched="pass"),
              behavioural(("m", 1, 1.0)))
    assert v.score == 0
    assert v.audit_gate == "fail"
    assert v.blocked_by == "audit-gate"
    # A zero that describes the submission is a valid result; only a run that
    # learned nothing is invalid.
    assert not v.harness_error
    # The failures are what the report shows, and harbor counts this list as
    # stage1_failures -- an undecided check must not be in it.
    assert [f["id"] for f in v.audit_failures] == [
        "posix_layer_retired", "wasi_platform_layer_is_real"]
    # The undecided gate is still on the record, just not in charge.
    assert v.metadata["audit_undecided"] == ["no_host_escape"]
    assert any("could not be decided" in n for n in v.notes)


def test_a_decided_failure_outranks_an_undecided_gate_on_a_split_review():
    """The same property from the other branch's control, which found it first.

    Two branches wrote this test against different tasks' State A trees, and the
    fixtures are worth keeping apart: this one has four required gates with one
    undecided, and it pins `audit_failures` as a *set*, so it stays true if
    the gate order ever changes.  The pf01 fixture above pins the order.

    The untouched State A found this: its reviews split on one gate while the
    others failed with grounded evidence, and the run was published as "not a
    valid result, re-run" rather than as the zero it plainly was.  A re-run
    cannot lift a gate that another required check already failed on, so the
    decided failure is the one that gets reported.
    """
    v = grade("t", POLICY, audit(js_left="fail", no_interpreter="fail",
                                     one_implementation="error"),
              behavioural(("m", 1, 1.0)))
    assert v.score == 0
    assert v.audit_gate == "fail"
    assert v.blocked_by == "audit-gate"
    assert not v.harness_error
    assert any("could not be decided" in n for n in v.notes)
    assert {f["id"] for f in v.audit_failures} == {"js_left", "no_interpreter"}


def test_undecided_still_blocks_when_it_is_the_only_obstacle():
    """With every other required gate passing, the undecided one decides nothing.

    This is the case the invalid verdict exists for: the gate might have passed,
    so publishing a zero would turn a model outage into a verdict.
    """
    v = grade("t", POLICY, audit(js_left="pass", no_interpreter="pass",
                                     one_implementation="error"),
              behavioural(("m", 1, 1.0)))
    assert not v.valid
    assert v.audit_gate == "not-run"
    assert v.blocked_by == "audit-undecided"


def test_a_decided_failure_outranks_an_undecided_check():
    """One undecided gate must not turn a real failure into a harness error.

    fw02's first run: two required gates failed on grounded evidence and a third
    was undecided, and the run published valid=false / audit-undecided --
    charging the submission's failure to the harness and asking for a re-run
    that could only reach the same verdict.
    """
    v = grade("t", POLICY, audit(shim="fail", awareness="error"),
              behavioural(("m", 1, 1.0)))
    assert v.score == 0
    assert v.valid
    assert not v.harness_error
    assert v.audit_gate == "fail"
    assert v.blocked_by == "audit-gate"
    assert [f["id"] for f in v.audit_failures] == ["shim"]
    # The undecided one is still reported -- it is just not what blocked.  Two
    # branches wrote this note and only one wording survived the merge: it says
    # "could not be decided", not "undecided", so the assertion is on the wording
    # that shipped.  The property is unchanged -- the undecided check is named to
    # the reader -- and the id is also pinned structurally below, where no
    # rewording can reach it.
    assert any("could not be decided" in n and "awareness" in n for n in v.notes)
    assert v.metadata["audit_undecided"] == ["awareness"]


def test_missing_audit_result_is_not_a_pass():
    v = grade("t", POLICY, None, behavioural(("m", 1, 1.0)))
    assert v.score == 0
    assert not v.valid


def test_advisory_gate_does_not_block():
    res = audit(shim="pass")
    res.add(Check(id="style", verdict="fail", summary="", required=False))
    v = grade("t", POLICY, res, behavioural(("m", 1, 1.0)), verification(6))
    assert v.audit_gate == "pass"
    assert v.score == 100


def test_a_reduced_quorum_reaches_the_report():
    """One surviving review still decides the gate, but says so.

    ``audit.py`` keeps ``status=ok`` when some reviews die and one comes
    back, and notes the shortfall on the stage.  A stage's own notes are read
    only on the error branch, so before this the report could not tell a gate
    decided by three reviews from one decided by the last review standing --
    they rendered identically.  The vote is unchanged; only the record grows.
    """
    res = audit(shim="pass")
    res.metadata.update({"samples_requested": 3, "samples_usable": 1})
    v = grade("t", POLICY, res, behavioural(("m", 1, 1.0)), verification(6))
    assert v.audit_gate == "pass"
    assert v.score == 100
    assert any("reduced quorum" in n and "1 of 3" in n for n in v.notes)


def test_a_reduced_quorum_is_recorded_on_a_failing_gate_too():
    """The shortfall is about the run, so a zero carries it as well as a pass."""
    res = audit(shim="fail")
    res.metadata.update({"samples_requested": 3, "samples_usable": 2})
    v = grade("t", POLICY, res, behavioural(("m", 1, 1.0)))
    assert v.score == 0
    assert v.audit_gate == "fail"
    assert any("reduced quorum" in n for n in v.notes)


def test_a_full_quorum_says_nothing():
    """The note has to be absent on a healthy run or it means nothing on a sick one."""
    res = audit(shim="pass")
    res.metadata.update({"samples_requested": 3, "samples_usable": 3})
    v = grade("t", POLICY, res, behavioural(("m", 1, 1.0)), verification(6))
    assert not any("quorum" in n for n in v.notes)


def test_a_quorum_of_zero_is_an_error_not_a_note():
    """No usable review is a harness error, and audit.py already sets that.

    Grading it as a note on a passing gate would publish a verdict nobody voted
    on, so the guard is ``0 < usable``, and the stage arrives with status=error.
    """
    res = StageResult(stage="audit", task="t", status="error")
    res.metadata.update({"samples_requested": 3, "samples_usable": 0})
    res.note("no review completed; the audit gate is undecided")
    v = grade("t", POLICY, res, behavioural(("m", 1, 1.0)))
    assert not v.valid
    assert v.blocked_by == "audit-error"
    assert "no review completed" in v.harness_error
    assert not any("quorum" in n for n in v.notes)


# --------------------------------------------------------------------------- #
# stage 2 is worth 40 and pays all of them or none, and stage 3 is asked only of a
# submission that took all 40
# --------------------------------------------------------------------------- #


def test_perfect_behavioural_pays_forty():
    v = grade("t", POLICY, audit(shim="pass"), behavioural(("m", 1, 1.0)))
    assert v.behavioural_points == pytest.approx(40.0)


def test_full_marks_is_decided_per_module_and_not_by_a_float():
    """No arithmetic stands between a perfect suite and its 40 points.

    The property this guards is that a submission which passed everything is never
    refused its points by a rounding error: payment is decided by reading `complete`
    on each module row -- a bool set by counting failed checks -- so no float
    comparison can go a bit under.  The weighted rate is computed and published
    beside it, and is asserted with `==` rather than `approx` because it is the
    figure a reader checks the rows against, and it holds bitwise: every scored
    check passing makes `earned` and `total` the same accumulation, and the
    weighted combine divides a sum by itself.

    Three weight shapes, including weights that do not sum to 1 and a suite whose
    weights sum to 0.93 the way lang04's do -- if full marks were unreachable on any
    of them, stage 3 would never run for anyone on that task.
    """
    for mods in (
        (("m", 1.0, 1.0),),
        (("a", 0.3, 1.0), ("b", 0.7, 1.0)),
        (("a", 0.25, 1.0), ("b", 0.63, 1.0), ("c", 0.05, 1.0)),
    ):
        v = grade("t", POLICY, audit(shim="pass"), behavioural(*mods),
                  verification(6))
        assert all(m.complete for m in v.modules), mods
        assert v.behavioural_rate == 1.0, mods
        assert v.behavioural_points == 40.0, mods
        assert v.blocked_by == "", mods
        assert v.verification_points == pytest.approx(60.0), mods


def test_one_failed_check_in_two_thousand_scores_nothing():
    """The sharpest form of the rule, and the reason it is worth stating plainly.

    1999 of 2000 checks is a migration that works, and it scores zero.  That is the
    rule doing what it says rather than a defect: the task is a whole-repository port,
    the suite is the specification, and a port that fails one case of it has not
    finished.  Six models spending an hour each looking for a second failure buys
    nothing once stage 2 has found the first.

    So the measured rate is the only place the near miss is visible, and the test
    pins it there -- 0.9995 reported beside 0.00 paid, with the failing module named
    and counted -- because a bare zero cannot be told from a tree that never built.
    """
    v = grade("t", POLICY, audit(shim="pass"),
              behavioural(("m", 1.0, 0.9995), checks=2000), verification(6))
    assert v.behavioural_points == 0.0
    assert v.blocked_by == "behavioural-incomplete"
    assert v.verification_points == 0
    assert v.score == 0.0
    assert "verification" not in v.stages_run
    # The rate survives the zero, and the note carries the count behind it.
    assert v.behavioural_rate == pytest.approx(0.9995)
    note = " ".join(v.notes)
    assert "did not pass every scored check" in note
    assert "m (1999/2000)" in note
    assert "0.9995 of their weighted checks, which is reported and not paid" in note


def test_a_lower_rate_scores_the_same_nothing_and_still_says_what_it_passed():
    # 995 of 1000 checks, ten times as many failures as the case above, and the same
    # zero.  Kept as a separate case precisely because the two are indistinguishable
    # at the payment and must not be at the reading: a reader comparing them sees one
    # stop, one score, and two rates.  That is the whole argument for publishing a
    # rate the formula does not use.
    v = grade("t", POLICY, audit(shim="pass"),
              behavioural(("m", 1, 0.995), checks=1000), verification(6))
    assert v.behavioural_points == 0.0
    assert v.score == 0.0
    assert v.verification_points == 0
    assert v.blocked_by == "behavioural-incomplete"
    assert "verification" not in v.stages_run
    # Same stop, same score, and a rate that is not the other case's.
    assert v.behavioural_rate == pytest.approx(0.995)
    assert "m (995/1000)" in " ".join(v.notes)


def test_modules_are_weighted_not_pooled():
    """Suite sizes differ by three orders of magnitude between these tasks.

    Pooling every check would let one module with 18,000 cases decide the stage and
    leave a nine-case build module unable to matter, however heavily it was weighted.
    So each module is rated on its own and the rates are combined by weight.

    The denominators have to differ for the fixture to say which rule ran: at equal
    sizes the weighted mean and the pooled fraction are the same arithmetic, and a
    test built on two ten-check modules cannot tell them apart however plainly it
    describes the difference.  1000 against 10 is the situation the docstring names,
    and the two rules read 0.75 and 0.5049 on it.

    The rows carry the other half of the argument, which is why they are asserted
    here: each module keeps its own rate, so a perfect module is not dragged down by
    a broken sibling and a broken one is not carried by a perfect one.
    """
    res = behavioural(("big", 0.5, 0.5), checks=1000)
    res.units.append(Unit(id="small", title="small", weight=0.5, status="ok"))
    for i in range(10):
        res.add(Check(id=f"small/c{i}", unit="small", verdict="pass", summary=""))
    v = grade("t", POLICY, audit(shim="pass"), res)

    # Weighted: (0.5 + 1.0) / 2.  Pooled: 510 of 1010 checks, which is 0.5049 -- the
    # ten passing cases all but erased by the module beside them.
    assert v.behavioural_rate == pytest.approx(0.75)
    passed, total = pooled(res.checks)
    assert (passed, total) == (510.0, 1010.0)
    assert v.behavioural_rate != pytest.approx(passed / total, abs=1e-3)

    by_id = {m.id: m for m in v.modules}
    assert by_id["big"].rate == pytest.approx(0.5)
    assert by_id["small"].rate == 1.0, "the small module keeps its own rate"
    assert by_id["small"].complete and not by_id["big"].complete
    # And the stage still pays nothing, because one module is short.  The rate above
    # is the reading; it is not a price.
    assert v.behavioural_points == 0.0
    assert v.blocked_by == "behavioural-incomplete"


def test_the_rate_reaches_the_report_beside_the_zero_it_did_not_earn():
    """A stage line reading only "0.00 points" cannot be told from a dead suite.

    Which is the reporting problem an all-or-nothing stage creates: 0.9925 of the
    weighted checks passing and nothing passing at all are the same award, so the
    header carries the rate next to the payment, and the module table carries the
    per-module rate next to a `done` marker naming which row is short.  A reader can
    then check the stage total against the rows it came from, and see that the zero
    is a rule rather than a collapse.
    """
    v = grade("t", POLICY, audit(shim="pass"),
              behavioural(("behaviour", 3.0, 0.99), ("build", 1.0, 1.0),
                         checks=1000))
    lines = render(v).splitlines()

    line = next(ln for ln in lines if "stage 2" in ln)
    assert "0.00 points" in line                                    # paid
    assert "0.9925 of weighted checks passed" in line               # measured

    behaviour = next(ln for ln in lines if ln.strip().startswith("behaviour"))
    assert "0.9900" in behaviour and " NO " in behaviour
    build = next(ln for ln in lines if ln.strip().startswith("build"))
    assert "1.0000" in build and " ok " in build


def test_a_module_that_overran_its_budget_is_charged_and_the_stage_publishes():
    """A weighted module that timed out scores zero and keeps its weight.

    A timeout is charged, and the reason is that the module was told its deadline.
    The budget is the task author's own declaration in `suite.toml`; the reference
    tree meets it on the same machine; and `behavioural.py` hands the module
    `SRB_MODULE_DEADLINE`, so a module that overruns has been told what it was
    overrunning.  Exceeding it is then a fact about the tree under test, of the same
    kind as a failed check -- lang02's `streaming` stalled 8 of 183 cases past 300s
    each, feeding 64KB byte-at-a-time through `deflate()`, against a reference
    recorded `ok` on the same input.  A quadratic implementation of a linear
    contract is a finding.

    Paying the surviving module's marks and reporting 0.5 with `valid` true would
    claim a measurement of whatever `ran-out` was for that nobody took, and refusing
    the whole run would be right only if a module could not be told its own
    deadline -- which is the case where a killed runner and a hung submission are
    indistinguishable, and `docs/SCHEMA.md`'s "a submission is only zeroed by
    evidence about the submission" makes refusal the honest reading.

    So: zero for the module, weight retained, stage published, run valid.  The
    distinction that survives is between a timeout and every other non-ok status --
    a signal, an OOM kill, a module that wrote nothing -- which say something about
    the machine and are still referred back for a re-run.
    """
    res = behavioural(("built", 0.5, 1.0))
    res.units.append(Unit(id="ran-out", title="ran-out", weight=0.5,
                          status="timeout"))
    v = grade("t", POLICY, audit(shim="pass"), res)

    assert v.valid, "a budget overrun is a measurement, not a harness failure"
    assert not v.harness_error
    # The stage stops on the ladder's own reading -- a module that did not pass
    # every scored check -- rather than on anything the module itself declared.
    assert v.blocked_by == "behavioural-incomplete"
    # Half the weight at 1.0 and half at 0.0.  The zero is charged, not excused,
    # and not spread over the surviving module either.
    overran = next(m for m in v.modules if m.id == "ran-out")
    assert overran.rate == 0.0
    assert overran.weight == pytest.approx(0.5)
    built = next(m for m in v.modules if m.id == "built")
    assert built.rate == pytest.approx(1.0)
    assert v.behavioural_rate == pytest.approx(0.5)
    # And the reason is stated, because a zero a reader cannot account for sends
    # them to the module's source looking for a bug that is in the submission.
    assert any("budget" in n for n in v.notes), v.notes


def test_a_module_killed_by_a_signal_costs_its_weight_and_publishes():
    """A module that died is a zero, not a verdict about the run's legitimacy.

    A SIGKILL is nearly always the OOM killer, and nothing about the tree under test
    follows from it -- but voiding the stage over that throws away every module that
    *did* measure the submission, and the tree does not get a second grading.  So the
    choice is not "score it right or score it wrong", it is "score the modules that
    ran, or score nothing at all".

    A dead module therefore behaves exactly as an empty one: rate 0.0, its own weight
    charged, the stage valid and publishable.  The machine's fingerprint stays in the
    notes for whoever reads the artifact afterwards, which is where a fact about the
    runner belongs -- it is not a fact about the submission, and it cannot silently
    take the other modules' measurements down with it.
    """
    res = behavioural(("built", 0.5, 1.0))
    res.units.append(Unit(id="killed", title="killed", weight=0.5, status="error"))
    v = grade("t", POLICY, audit(shim="pass"), res)

    assert v.valid, "a dead module does not make the stage unpublishable"
    assert v.blocked_by == "behavioural-incomplete"
    assert not v.harness_error
    # Charged, weight kept: a denominator that dropped it would make a module that
    # dies cheaper than one that fails.
    killed = next(m for m in v.modules if m.id == "killed")
    assert killed.rate == 0.0
    assert killed.weight == pytest.approx(0.5)
    # The module that did run keeps its measurement, and now keeps its credit too.
    built = next(m for m in v.modules if m.id == "built")
    assert built.rate == pytest.approx(1.0)
    assert v.behavioural_rate == pytest.approx(0.5)
    # Said in words, because "0.00 over 900 checks" and "the module never wrote a
    # result" are the same number and different facts.
    assert any("did not finish" in n for n in v.notes), v.notes
    assert any("killed" in n for n in v.notes), v.notes


def test_a_module_that_overran_costs_its_own_weight_and_nothing_more():
    """A timeout is an ordinary zero: charged, weighted, and publishable.

    A timeout charges its own weight and has no power over the other rows, and two
    things are why.  It is the status most often set by the machine rather than by
    the tree -- a loaded host, a slow toolchain fetch -- and since a weighted module
    that never finished costs the whole stage anyway, the rate is all that is left to
    tell a submission that overran one module from one that failed everywhere, which
    is the reading a re-run is decided on.  And downstream modules do not in fact go
    unpunished when a build fails: their checks report `error`, they are already
    counted, and each already scores zero on its own evidence, so charging them for
    the overrun as well would charge for that twice.
    """
    res = behavioural(("behaviour", 0.5, 1.0))
    res.units.append(Unit(id="build", title="build", weight=0.5, status="timeout"))
    v = grade("t", POLICY, audit(shim="pass"), res)

    assert v.valid
    # Half the suite measured 1.0 and is paid for it; the overrun costs its own 0.5.
    assert v.behavioural_rate == pytest.approx(0.5)
    build = next(m for m in v.modules if m.id == "build")
    assert build.rate == 0.0
    assert build.weight == pytest.approx(0.5), "the weight is kept, not redistributed"
    # And the overrun is still stated, because "scored 0.00" and "never finished"
    # are the same number and different facts, and only one of them is about the
    # submission.
    assert any("exceeded the budget" in n for n in v.notes), v.notes


def test_a_module_the_stage_clock_never_reached_is_charged_and_the_stage_publishes():
    """The stage clock's own version of the rule above, one level up.

    A module's budget is enforced by the runner, which can warn it.  The *stage*
    timeout is enforced from outside by `docker kill`, so nothing can be warned, and
    a suite that wrote its result once at the end would have no result at all: the
    end never comes, and every module that had already finished is lost with the one
    that had not.

    The suite therefore seeds a row per declared module and checkpoints after each
    one, which lets the artifact distinguish the two: `structure` carries what it
    measured and `walk` carries `unreached`.  The unreached module is charged --
    zero, weight kept -- because a denominator assembled from whoever finished would
    make hanging module 2 cheaper than failing it, and cost nothing for modules
    3..N.
    """
    res = behavioural(("structure", 0.5, 1.0))
    res.units.append(Unit(id="walk", title="walk", weight=0.5, status="unreached"))
    v = grade("t", POLICY, audit(shim="pass"), res)

    assert v.valid, "a stage that measured something publishes what it measured"
    assert not v.harness_error
    assert v.blocked_by == "behavioural-incomplete"
    walk = next(m for m in v.modules if m.id == "walk")
    assert walk.rate == 0.0
    assert walk.weight == pytest.approx(0.5), "the weight is kept, not redistributed"
    assert v.behavioural_rate == pytest.approx(0.5)
    # Said in words, and worded as an absence rather than as a measurement: an
    # overrun points at the module named, an unreached one points at the modules
    # before it, which are where the clock went.
    note = next((n for n in v.notes if "before" in n and "reached" in n), "")
    assert note, v.notes
    assert "walk" in note


def test_a_stage_that_reached_no_module_at_all_still_publishes_zero():
    """Every row `unreached`: a zero, not a referral.

    Nothing in the artifact separates a tree that ate the clock from an image that hung
    on startup or a loaded machine, so the zero cannot name its own cause.  It is still
    the right number: a referral reads as "we do not know" while costing exactly what a
    zero costs, and the tree does not come back for a second grading -- so the honest
    options are a published zero or a blank, and a blank is worse, dropping out of
    every aggregate silently instead of appearing as the zero it already is.

    The cause stays where a cause belongs, in the note, and it does not decide whether
    the number exists.
    """
    res = behavioural()
    for mid in ("build", "walk"):
        res.units.append(Unit(id=mid, title=mid, weight=0.5, status="unreached"))
    v = grade("t", POLICY, audit(shim="pass"), res)

    assert v.valid, "an all-unreached stage publishes a zero rather than a blank"
    # Named for what it is, not for how it happened: the stage stops because no
    # module passed its scored checks, and the cause stays in the note below.
    assert v.blocked_by == "behavioural-incomplete"
    assert not v.harness_error
    assert v.behavioural_rate == 0.0
    # Both rows charged, both weights kept -- the table is readable, and the reason
    # is written down rather than encoded in the stage's validity.
    assert [m.rate for m in v.modules] == [0.0, 0.0]
    assert all(m.weight == pytest.approx(0.5) for m in v.modules)
    assert any("did not finish" in n or "reached" in n for n in v.notes), v.notes


def test_seeded_rows_do_not_make_an_unmeasured_stage_look_measured():
    """`measured` counts modules that reported, not rows in the table.

    The seeding is what makes this necessary: a stage killed before its first module
    finished now carries a full table, so anything asking "did stage 2 measure
    anything" by row count reads N measurements off a stage that took none.  Both
    readers of that question are checked here, because they are two expressions of it
    -- `Verdict.uncounted` sets the flag the arithmetic sentence keys on and the
    report prints the line -- and a disagreement puts a module table under "measured
    nothing" or the reverse.
    """
    from swerefactor.report import render
    from swerefactor.scoring import _reported

    assert _reported([{"id": "a", "status": "ok"},
                      {"id": "b", "status": "unreached"}]) == 1
    assert _reported([{"id": "a", "status": "unreached"}]) == 0
    # A row with no status at all is a module that ran: this also reads verdicts
    # written before `unreached` existed.
    assert _reported([{"id": "a"}]) == 1

    res = behavioural()
    for mid in ("build", "walk"):
        res.units.append(Unit(id=mid, title=mid, weight=0.5, status="unreached"))
    v = grade("t", POLICY, audit(shim="fail"), res)
    seen = v.uncounted.get("behavioural") or {}
    assert seen.get("ran") is True
    assert seen.get("measured") is False, (
        "two unreached rows are not two measurements")
    text = render(v)
    line = next(l for l in text.splitlines() if "stage 2" in l)
    # The claim under test is that no *rate* is reported, not that the word
    # "measured" is absent -- this branch's own sentence is "has not measured the
    # submission", which is the thing being asserted rather than a violation of it.
    assert "NO RESULT" in line, line
    assert "rate" not in line, line


def test_a_modules_own_notes_reach_the_verdict_and_the_report():
    """A module explaining its own skips is the reason its rate is readable.

    ``behavioural.py`` folds a module's notes into ``unit.metadata["notes"]``,
    and they stopped there: five lang04 modules wrote one each on the untouched
    State A -- structure declining 7 of 17 structural cases, provenance
    declining all 6 gates -- and none appeared in ``report.txt``.  A reader
    comparing structure's rate against the catalogue had nothing to explain the
    gap, which is what driver.py's note says it exists to prevent, "in the
    report as well as the log".
    """
    from swerefactor.report import render

    res = behavioural(("structure", 0.5, 1.0), ("built", 0.5, 1.0))
    for unit in res.units:
        if unit.id == "structure":
            unit.metadata["notes"] = [
                "7 of 17 structural case(s) are not applicable to a javascript "
                "tree and were not asked: install-modes, workspace-members"
            ]
    v = grade("t", POLICY, audit(shim="pass"), res)
    structure = next(m for m in v.modules if m.id == "structure")
    assert structure.notes and "not applicable" in structure.notes[0]
    # The scorer's own note is a different field and stays empty here: this
    # module ran fine, so there is no zero to explain.
    assert structure.note == ""
    assert "notes" in structure.to_dict()

    text = render(v)
    assert "install-modes" in text
    # And the module it belongs to is identifiable from the report.
    assert text.index("structure") < text.index("install-modes")


def test_a_module_with_nothing_to_say_adds_no_note_lines():
    """The negative control: absent notes must print nothing at all."""
    from swerefactor.report import render

    v = grade("t", POLICY, audit(shim="pass"), behavioural(("m", 1, 1.0)))
    assert all(m.notes == [] for m in v.modules)
    assert "note:" not in render(v)


def test_a_module_note_survives_being_a_bare_string():
    """Defensive only: the field is a list by contract, and one task wrote one."""
    res = behavioural(("m", 1, 1.0))
    res.units[0].metadata["notes"] = "a single note, not in a list"
    v = grade("t", POLICY, audit(shim="pass"), res)
    assert v.modules[0].notes == ["a single note, not in a list"]


def test_a_module_that_skipped_everything_scores_zero_and_keeps_its_weight():
    """An unanswerable module is not a cheap module.

    Six skips pool as 0/6 rather than 0/0, so the module takes the ordinary scoring
    path and its zero is an ordinary rate; the empty-denominator branch is only for a
    module that recorded nothing whatsoever.  The 0.5 below is the point -- the reason
    an all-skip module cannot be cheap does not depend on which branch describes it.
    """
    res = behavioural(("built", 0.5, 1.0))
    res.units.append(Unit(id="inapplicable", title="inapplicable", weight=0.5,
                          status="ok"))
    for i in range(6):
        res.add(Check(id=f"inapplicable/c{i}", unit="inapplicable",
                      verdict="skip", summary="not applicable to this tree"))
    v = grade("t", POLICY, audit(shim="pass"), res)
    # Half the weight scored zero, so the stage pays half -- the skips did not
    # quietly drop out of the denominator and leave a perfect run.
    assert v.behavioural_rate == pytest.approx(0.5)
    # And the module row says the six were charged rather than absent.  An empty
    # denominator reads "produced no scored checks", which of a module scored 0/6
    # would be false in the one word that matters.
    note = next(m.note for m in v.modules if m.id == "inapplicable")
    assert "6 of 6 scored check(s) did not run and are charged 0" in note
    assert "absence of an artifact to measure" in note
    row = next(m for m in v.modules if m.id == "inapplicable")
    # `scored_weight` is the denominator, and 6 rather than 0 is the whole change:
    # the six checks are IN the fraction.  Asserted beside `skipped` so the row says
    # both things at once -- six scored, six of them declined.
    assert (row.passed, row.scored_weight, row.skipped) == (0, 6.0, 6), row


def test_an_all_skip_module_is_inert_only_at_weight_zero():
    """The shape a task uses on purpose: lang04's provenance on a JS tree.

    All six provenance gates skip when the tree is still JavaScript, and the
    module carries weight 0.00 so the skips cost nothing.  That is the only
    reason a wholesale skip is safe, which is worth pinning: an author who
    builds an applicability-skip module and leaves the weight nonzero gets the
    silent penalty the test above measures.
    """
    res = behavioural(("built", 0.93, 1.0))
    res.units.append(Unit(id="provenance", title="provenance", weight=0.0,
                          status="ok"))
    for i in range(6):
        res.add(Check(id=f"provenance/c{i}", unit="provenance", verdict="skip",
                      summary="no release artifact to attribute yet"))
    v = grade("t", POLICY, audit(shim="pass"), res)
    assert v.behavioural_points == pytest.approx(40.0)


def test_the_note_separates_a_wholesale_skip_from_a_silent_module():
    """Both score zero; only one of them is a design statement.

    An operator acting on the zero needs to know whether the module declined
    every case on purpose or recorded nothing at all, so the note distinguishes
    them rather than saying "no scored checks" for both.
    """
    res = behavioural(("built", 0.5, 1.0))
    res.units.append(Unit(id="silent", title="silent", weight=0.5, status="ok"))
    v = grade("t", POLICY, audit(shim="pass"), res)
    note = next(m.note for m in v.modules if m.id == "silent")
    # Asserted as the distinguishing parenthetical plus the absence of the other
    # one: an equality would break on any rewording of the clause the two notes
    # share, and the property under test is that the two shapes are told apart.
    assert "it recorded no checks at all" in note
    # And not the wholesale-skip module's clause, which is what this note would
    # carry if one string were describing both shapes.
    assert "declined as inapplicable" not in note


def test_a_failing_module_zeroes_the_stage_by_the_ladder_and_not_by_a_flag():
    """A `required` written into the input must not reach the arithmetic.

    `build` failing arrives here from two directions a suite could try: the module
    carrying `required` in its metadata, and its checks carrying it too.  Both are
    set here deliberately, because both are natural to write and neither may have
    any effect on a behavioural score -- `required` decides an audit gate and
    nothing else.

    The stage does score zero, and the point of the test is which rule produced it:
    the ladder's, reading a module that did not pass every scored check, and not a
    clamp reading a flag.  The two are told apart by the rate, which is 0.8 rather
    than 0.0 -- a clamp would have taken the other 0.8 of the suite with it.
    """
    res = behavioural(("build", 0.2, 0.0), ("behaviour", 0.8, 1.0))
    for unit in res.units:
        if unit.id == "build":
            unit.metadata["required"] = True
    for check in res.checks:
        if check.unit == "build":
            check.required = True
    v = grade("t", POLICY, audit(shim="pass"), res)

    assert v.behavioural_points == 0.0
    assert v.behavioural_rate == pytest.approx(0.8), (
        "a clamp would have taken the whole suite, not `build`'s share of it")
    assert v.blocked_by == "behavioural-incomplete"
    # And the module's own row still shows what it measured, unclamped.
    build = next(m for m in v.modules if m.id == "build")
    assert build.rate == 0.0 and build.weight == pytest.approx(0.2)
    assert "required" not in build.note


# --------------------------------------------------------------------------- #
# stage 3 is worth 60: ten points per adversary that found nothing
# --------------------------------------------------------------------------- #


def test_ten_points_per_survivor():
    v = grade("t", POLICY, audit(shim="pass"), behavioural(("m", 1, 1.0)),
              verification(survived=4, broke=2))
    assert v.verification_points == pytest.approx(40.0)
    assert v.adversaries_survived == 4
    # 40 from stage 2 and 40 from four survivors.  The two halves coincide here on
    # purpose: it is the one place in the ladder where the same figure can arrive
    # from either stage, so a scorer that double-counted one of them would still
    # produce a plausible total, and the survivor count above is what separates them.
    assert v.score == pytest.approx(80.0)


def test_a_round_that_errored_pays_nothing_and_is_not_a_break():
    v = grade("t", POLICY, audit(shim="pass"), behavioural(("m", 1, 1.0)),
              verification(survived=5, errored=1))
    assert v.verification_points == pytest.approx(50.0)
    assert not v.verification_breaks       # an outage is not a defect found
    assert v.notes or v.metadata          # but it is reported somewhere
    # And the 50 is not a total.  `verification.py` raises the STAGE status only
    # when every round errored, so a five-of-six outage arrives here as a clean
    # `status: ok` result -- ten points the sixth adversary never got to contest,
    # simply missing from the field `flatten` puts beside `reward`.  The measured
    # points stay on the verdict because they were measured; `valid` is what says
    # the sum is not final.
    assert not v.valid
    assert v.blocked_by == "verification-incomplete"
    assert "1 of 6" in v.harness_error


def test_a_declared_stage_three_with_no_result_is_a_re_run():
    """The ladder reached stage 3, the task asked for one, and no file appeared.

    Worth 60 of the 100 points, so this is the largest silent shortfall in the
    ladder -- the majority of the score, and more than stage 2 can pay -- and the one
    shape that has to be told apart from a task with no stage 3 at all: `cmd_score`
    and `harbor.main` both compute "declared but absent", and both hand `grade` a
    `None` that would otherwise publish 40.00 as a full result.
    """
    v = grade("t", POLICY, audit(shim="pass"), behavioural(("m", 1, 1.0)),
              None, declared=("audit", "behavioural", "verification"))

    assert not v.valid
    assert v.blocked_by == "verification-missing"
    assert "60" in v.harness_error
    assert v.metadata.get("verification_missing") is True
    # The 40 it did measure is still there to be read; it is just not a total.
    assert v.behavioural_points == pytest.approx(40.0)


def test_an_undeclared_stage_three_is_not_a_re_run():
    """The other side of `declared`, and the reason it is a parameter.

    Grading stages 1 and 2 by hand is a normal thing to do -- most of this file
    does it -- and a task may legitimately have no verification stage.  Neither is
    an outage, so neither may invalidate a verdict.
    """
    v = grade("t", POLICY, audit(shim="pass"), behavioural(("m", 1, 1.0)))

    assert v.valid and not v.blocked_by
    assert v.score == pytest.approx(40.0)


def test_a_stage_three_that_did_not_run_is_never_a_published_zero():
    """Stage status error: the whole stage failed, so nothing was contested.
    """
    res = verification(0)
    res.status = "error"
    res.metadata["error"] = "the gateway refused every request"
    v = grade("t", POLICY, audit(shim="pass"), behavioural(("m", 1, 1.0)), res,
              declared=("audit", "behavioural", "verification"))

    assert not v.valid
    assert v.blocked_by == "verification-error"
    assert "gateway refused" in v.harness_error
    assert v.verification_points == 0.0


def test_every_adversary_breaking_through_leaves_only_behavioural_points():
    v = grade("t", POLICY, audit(shim="pass"), behavioural(("m", 1, 1.0)),
              verification(survived=0, broke=6))
    assert v.score == pytest.approx(40.0)
    assert len(v.verification_breaks) == 6


def test_full_marks_is_one_hundred():
    v = grade("t", POLICY, audit(shim="pass"), behavioural(("m", 1, 1.0)),
              verification(6))
    assert v.score == pytest.approx(100.0)
    assert v.to_dict()["reward"] == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# properties that are easy to get backwards
# --------------------------------------------------------------------------- #


def test_an_empty_check_pool_rates_zero():
    """Nothing measured is not the same as everything passing.

    The opposite convention would pay full marks to a module whose test runner
    crashed before collecting anything, which is a reward for breaking it.
    """
    assert rate([]) == 0.0


def test_skips_stay_in_the_denominator_and_score_zero():
    """A skip is charged: 1 of 2, not 1 of 1.

    The reverse of what this asserted, and the reason is that the two situations a
    skip covered are not one situation.  Where the *reference* has no answer the
    check is not a question.  What is left is a question some other submission
    answered -- so letting the skip leave paid this submission for the gap it
    created, and it made a task's denominator differ per model, which stops two
    cells' module rows from being comparable at all.
    """
    checks = [Check(id="a", verdict="pass", summary=""),
              Check(id="b", verdict="skip", summary="")]
    assert rate(checks) == pytest.approx(0.5)
    # And the pair, not only the ratio: the skip must be IN the denominator rather
    # than out of both sides, which a ratio alone cannot distinguish (1/1 and 1/2
    # differ, but 0/0 and a dropped check would not).
    assert pooled(checks) == (1.0, 2.0)


def test_a_module_that_skipped_every_check_rates_zero_over_its_real_size():
    """All-skip is 0/N, not 0/0 -- the guard in `rate` is no longer what gets there.

    Worth its own test because the two arrive at the same 0.0 by different routes,
    and only one of them keeps the denominator a fact about the task: an empty pool
    says nothing about how many questions there were.
    """
    checks = [Check(id=f"c{i}", verdict="skip", summary="") for i in range(6)]
    assert pooled(checks) == (0.0, 6.0)
    assert rate(checks) == 0.0


def test_unknown_verdict_reads_as_error_not_pass():
    check = Check.from_dict({"id": "x", "verdict": "probably-fine",
                             "summary": ""})
    assert check.verdict == "error"
    assert not check.ok


# --------------------------------------------------------------------------- #
# weight 0: how a task records an observation without charging for it
#
# Seven fw tasks score State A at full marks while still recording that State A has
# not been ported -- "gin is still linked", "the wire still says X-Powered-By:
# Express", "the manifest declares neither react nor react-dom".  Those checks fail,
# deliberately, and must not cost a point, because whether a port happened is stage
# 1's question and stage 2 only supplies the evidence.
#
# Weight 0 is inert without qualification: it does not matter whether the check or the
# module carrying it is also marked required.
#
# A weight-0 check is dropped at collection (`behavioural.py:_collect`), so in a fresh
# run it is not in the result at all.  The scorer still has to handle one, because a
# re-grade reads `behavioural.json` files that carry thousands of them.
# --------------------------------------------------------------------------- #


def test_a_weight_zero_check_cannot_move_its_modules_rate():
    """The scorer's half of the rule, which a re-grade still depends on.

    Checks inside a module are equally weighted now, so `pooled` cannot express
    "counts for less" -- weight is one bit, and a weight-0 row is excluded from both
    halves of the fraction rather than added to the denominator with a zero on top.
    The two arrive at the same rate here, which is why this test did not change when
    the arithmetic underneath it did.
    """
    checks = [Check(id="real", verdict="pass", summary=""),
              Check(id="observation", verdict="fail", summary="", weight=0.0)]
    assert rate(checks) == pytest.approx(1.0)
    # `judged` is about the verdict, weight is about the denominator, and since
    # skips are charged those are no longer two ways to leave the fraction: weight
    # is the only one.  Asserted anyway, because it is what makes the line above a
    # statement about weight -- both checks here reached a verdict, so the 1.0 can
    # only come from the weight-0 one being excluded.
    assert all(c.judged for c in checks)


def test_a_weight_zero_module_cannot_move_the_stage():
    """fw02's `closure` rates 0.125 on State A and the stage still pays all 40.

    Which is the whole reason a task may declare a module at weight 0: under an
    all-or-nothing stage, a scored module that fails one case costs the submission
    every point, so an observational module carrying weight would make its
    observations fatal.  Weight 0 keeps them observations.

    Here the rate is 0.1 rather than fw02's 0.125 only because the helper expands a
    module to ten checks; fw02's was one pass of eight.
    """
    res = behavioural(("behaviour", 1.0, 1.0), ("closure", 0.0, 0.1))
    v = grade("t", POLICY, audit(shim="pass"), res)
    assert v.behavioural_rate == pytest.approx(1.0)
    assert v.behavioural_points == pytest.approx(40.0)
    assert v.blocked_by == "", "a weight-0 module's failures cannot stop the stage"
    # The failures are still reported against the module that found them.
    closure = next(m for m in v.modules if m.id == "closure")
    assert closure.failed == 9 and closure.rate == pytest.approx(0.1)


def test_a_failing_weight_zero_check_is_free_at_the_module_level_too():
    """The check level: nothing a single check can do zeroes its module.

    Setting weight=0 to make an observation free, and required=True out of habit
    because it looked important, must not silently cost a task a module.  A weight-0
    check is dropped, and `required` on a check does not reach the behavioural scorer
    at all.
    """
    res = behavioural(("behaviour", 1.0, 1.0))
    res.add(Check(id="observation", unit="behaviour", verdict="fail", summary="",
                  weight=0.0, required=True))
    v = grade("t", POLICY, audit(shim="pass"), res)
    behaviour = next(m for m in v.modules if m.id == "behaviour")
    assert behaviour.rate == pytest.approx(1.0), (
        "a weight-0 check, required or not, is outside the fraction")
    assert "required" not in behaviour.note
    assert v.behavioural_points == pytest.approx(40.0)


def test_a_reporting_module_pays_full_marks():
    """A module that only reports: weight 0, all checks weight 0, fw07's shape."""
    res = behavioural(("behaviour", 1.0, 1.0))
    res.units.append(Unit(id="provenance", title="reports only", weight=0.0,
                          status="ok"))
    for i in range(3):
        res.add(Check(id=f"provenance/obs{i}", unit="provenance", verdict="pass",
                      summary="an observation", weight=0.0))
    v = grade("t", POLICY, audit(shim="pass"), res)

    assert v.valid and v.behavioural_points == pytest.approx(40.0)
    prov = next(m for m in v.modules if m.id == "provenance")
    assert prov.scored_weight == 0.0 and prov.rate == 0.0
    assert "reports only, by design" in prov.note


def test_a_module_that_died_costs_its_weight_and_the_rest_is_still_measured():
    """A crashed module is an absence priced at its own weight.

    status != ok means the zero is an absence rather than a finding, so publishing 0.0
    for it names a cause the artifact cannot support -- but the other modules DID
    measure the submission, and voiding the stage throws their measurements away too,
    leaving the reader no number at all where 0.90 of the suite had a real one.

    So the absence is priced instead: 0.10 of the weight is lost and 0.90 of it is
    still measured and reported, with the cause in a note.  The stage pays nothing,
    because a module that did not finish did not pass every scored check either --
    but that is the ladder reading the rate, and the rate is still the reader's.
    """
    res = behavioural(("behaviour", 1.0, 1.0))
    res.units.append(Unit(id="build", title="build both", weight=0.10,
                          status="error", summary="the reference did not compile"))
    v = grade("t", POLICY, audit(shim="pass"), res)

    assert v.valid
    assert not v.harness_error
    # `behavioural` gives `behaviour` weight 1.0, so the two normalise to 1/1.1 and
    # 0.1/1.1 -- deliberately not written as a rounded 0.909, so a change in the
    # normalisation shows up here as an arithmetic failure rather than a near-miss.
    assert v.behavioural_rate == pytest.approx(1.0 / 1.1)
    # The stage pays nothing and stage 3 is not offered, and the reason named is the
    # ladder's rather than a module clamp's: `build` did not pass every scored check,
    # which is the same sentence a submission gets for one failing assertion.  The id
    # is asserted rather than waved past, because "some blocked_by is set" is how a
    # clamp inside a module would hide.
    assert v.behavioural_points == 0.0
    assert v.blocked_by == "behavioural-incomplete"
    assert any("did not finish" in n for n in v.notes), v.notes
    assert any("build" in n for n in v.notes), v.notes


def test_a_module_at_rate_zero_is_charged_only_its_own_weight():
    """The rule the whole reading rests on, on the case that shows it most plainly.

    Status ok, ten real checks, ten failures, and the module carrying 0.9 of the
    suite: the module ran and the submission failed it.  That is a measurement, so
    the zero is published, and it stays this module's zero rather than the stage's --
    `style` measured 1.0 and keeps its 0.1 of the rate, which is the difference
    between charging a failed module and charging the suite for it.

    The stage pays nothing either way here.  What the rule buys is the reading: 0.1
    rather than 0.0 tells an operator that a tenth of the suite passed, and that is
    the number a later attempt is compared against.
    """
    res = behavioural(("behaviour", 0.9, 0.0), ("style", 0.1, 1.0))
    v = grade("t", POLICY, audit(shim="pass"), res)

    assert v.valid, "a module that ran and failed has measured the submission"
    # 0.1, not 0.0: `style` keeps its 0.1 of the weight, which is the difference
    # between charging a failed module and charging the whole suite for it.
    assert v.behavioural_rate == pytest.approx(0.1)
    assert "scored zero" not in " ".join(v.notes)
    # And stage 3 is not offered, on the stage's own terms rather than any one
    # module's: a suite this far from complete has already shown the port is
    # unfinished, and six adversaries looking for a seventh defect add nothing.
    assert v.behavioural_points == 0.0
    assert v.blocked_by == "behavioural-incomplete"


def test_a_dead_reporting_module_is_said_out_loud_and_paid_in_full():
    """Weight 0 and unrequired: the one dead module that cannot move the number.

    Its observations are missing from the report, so the run is not identical to
    a clean one and a note has to say why -- but the score is untouched, and
    calling this a re-run would make every crashed reporting module cost a whole
    evaluation.
    """
    res = behavioural(("behaviour", 1.0, 1.0))
    res.units.append(Unit(id="provenance", title="reports only", weight=0.0,
                          status="error", summary="the collector crashed"))
    v = grade("t", POLICY, audit(shim="pass"), res)

    assert v.valid and v.behavioural_points == pytest.approx(40.0)
    assert not v.blocked_by
    assert "provenance" in " ".join(v.notes)


def test_a_weight_zero_module_at_rate_zero_costs_nothing():
    """Weight 0 is inert on its own, with no second condition attached.

    Weight is the only mechanism that says how much a module matters.  A second
    declaration beside it -- costing nothing at weight 0, and all 40 points if the
    same module were also marked required -- would differ by two tokens in suite.toml
    and by the whole score in the result, and it is a natural thing to write for a
    module whose output explains the score.

    Note what separates this from a reporting module: the helper gives every check
    the default weight, so this module scored ten checks and failed ten.  Its rate of
    0.0 is a real measurement -- it just has no weight to spend it through.
    """
    res = behavioural(("behaviour", 1.0, 1.0), ("closure", 0.0, 0.0))
    v = grade("t", POLICY, audit(shim="pass"), res)
    assert v.behavioural_points == pytest.approx(40.0)
    assert not v.blocked_by
    closure = next(m for m in v.modules if m.id == "closure")
    assert closure.rate == 0.0 and closure.failed == 10


# --------------------------------------------------------------------------- #
# a stage's own notes have to survive the ok path
#
# A stage that ran fine and recorded something must not render identically to one
# with nothing to say.  These assert the note reaches the verdict, because "it is
# written to the result file" is already true while nothing displays it, and every
# reader of `result.notes` is easy to write as an error branch.
# --------------------------------------------------------------------------- #


def test_a_passing_stage_carries_its_own_notes():
    ig = audit(shim="pass")
    ig.notes.append("one review sample was retried after a gateway timeout")
    fn = behavioural(("m", 1, 1.0))
    fn.notes.append("the tls module reused a cached certificate")
    v = grade("t", POLICY, ig, fn)
    joined = " | ".join(v.notes)
    assert "stage 1: one review sample was retried after a gateway timeout" in joined
    assert "stage 2: the tls module reused a cached certificate" in joined
    # Carrying a note must not move the arithmetic.
    assert v.score == 40.0


def test_the_note_says_which_stage_spoke():
    """The notes section is shared, so an unattributed note is ambiguous.

    Both stages emit the same sentence here: without the prefix the reader
    cannot tell one degraded stage from two.
    """
    ig, fn = audit(shim="pass"), behavioural(("m", 1, 1.0))
    ig.notes.append("a sample was retried")
    fn.notes.append("a sample was retried")
    v = grade("t", POLICY, ig, fn)
    assert "stage 1: a sample was retried" in v.notes
    assert "stage 2: a sample was retried" in v.notes


def test_an_errored_stage_still_reports_its_note_as_the_cause():
    """The error branch already used notes as the failure text; keep it.

    The fix adds a path, it does not move one, and this is the assertion that
    would catch it having moved.
    """
    ig = StageResult(stage="audit", task="t", status="error")
    ig.notes.append("the audit model returned 503 on every sample")
    v = grade("t", POLICY, ig, behavioural(("m", 1, 1.0)))
    assert v.blocked_by == "audit-error"
    assert "503" in v.harness_error
    # Not carried a second time as an ok-path note.
    assert not any(n.startswith("stage 1:") for n in v.notes)


def test_an_empty_or_blank_note_is_not_carried():
    ig = audit(shim="pass")
    ig.notes.extend(["", "   "])
    v = grade("t", POLICY, ig, behavioural(("m", 1, 1.0)))
    assert not any(n.startswith("stage 1:") for n in v.notes)


def test_a_surviving_verification_stage_carries_its_notes():
    adv = verification(6)
    adv.notes.append("adversary a04 fell back to a smaller context window")
    v = grade("t", POLICY, audit(shim="pass"), behavioural(("m", 1, 1.0)), adv)
    assert "stage 3: adversary a04 fell back to a smaller context window" in v.notes
    assert v.score == 100.0


# --------------------------------------------------------------------------- #
# a stage that ran is not reported as one that did not
# --------------------------------------------------------------------------- #
# The ladder stops at a failed audit gate, so stage 2 is never graded.  But the
# two stages are separate invocations and stage 2 has usually already run by then, so
# reporting "stage 2  behavioural modules ..... NOT RUN" would deny twelve measured
# modules on a submission whose build succeeded and whose 88 cases were captured.
# Telling a reader whether a zero belongs to the submission or to the grader is the
# one thing the report exists for.
#
# `stages_run` is not the record of it.  `harbor.flatten` publishes that list as *two*
# keys -- `stage2_ran` and `stage2_scored` -- so appending "behavioural" to it would
# also publish "stage 2 was scored" next to `stage2_points: 0.0` and `score: 0.0`.
# `stage2_ran` is already `stages_run or unscored_stages`, and stage 3 is recorded the
# same way, through `unscored`.
#
# One state, one spelling: the report renders the points/rate split, the stage's own
# one-line summary, its reason for stopping, its notes and the module table, under
# "RAN, NOT COUNTED" -- the words both stage-3 lines and test_unscored_stages.py use.
# --------------------------------------------------------------------------- #


def test_a_behavioural_stage_that_ran_is_reported_after_a_gate_fail():
    v = grade("t", POLICY, audit(shim="fail"), behavioural(("m", 1, 0.5)))
    # Reported, and not by way of `stages_run`.  This asserted `"behavioural" in
    # v.stages_run`; `flatten` publishes that list twice, once as `stage2_ran` and
    # once as `stage2_scored`, so appending said the stage was scored beside
    # `stage2_points: 0.0`.  The two claims are separated here: it ran, it was not
    # scored, and both are what the published numbers say.
    assert "behavioural" not in v.stages_run
    from swerefactor.harbor import flatten
    flat = flatten(v)
    assert flat["stage2_ran"] == 1
    assert flat["stage2_scored"] == 0
    assert [m.id for m in v.modules] == ["m"]
    assert v.metadata["behavioural_measured_not_counted"] is True
    # The award, which is a zero for a module at 0.5 -- and a zero here is exactly
    # the shape this test objects to elsewhere, since "0.00 points" is also what a
    # stage that never ran would show.  So the reading that is not a zero is asserted
    # beside it: `behavioural_measured_points` is what the stage would have been paid,
    # and `behavioural_measured_rate` is the evidence it ran and measured something.
    assert v.metadata["behavioural_measured_points"] == 0.0
    assert v.metadata["behavioural_measured_rate"] == pytest.approx(0.5)


def test_reporting_a_ran_stage_awards_nothing():
    """The measured points are shown, never added.

    `behavioural_points` is what stage 2 PAID, published to Harbor as
    `stage2_points` next to `score` and `reward`.  A consumer that sums the stage
    points and checks the total against `score` is doing something reasonable, so
    the measured value travels in metadata and the awarded field stays 0.
    """
    v = grade("t", POLICY, audit(shim="fail"), behavioural(("m", 1, 1.0)))
    assert v.score == 0
    assert v.behavioural_points == 0
    assert v.behavioural_rate == 0
    from swerefactor.harbor import flatten
    flat = flatten(v)
    assert flat["stage2_points"] == 0
    assert flat["stage2_ran"] == 1          # because it did
    assert flat["module_m_rate"] == 1.0     # and this is what it measured


def test_reporting_a_ran_stage_cannot_invalidate_a_policy_zero():
    """A reporting fix must not turn a valid zero into a re-run.

    A failed gate is a legitimate 0 with `valid: true`.  Grading the behavioural
    result into the real verdict would let its error paths set `harness_error`,
    which flips `valid` to false and asks for the whole run again -- strictly
    worse than the reporting bug being fixed.  Here stage 2 declares no modules,
    which is one of those paths.
    """
    empty = StageResult(stage="behavioural", task="t", status="ok")
    v = grade("t", POLICY, audit(shim="fail"), empty)
    assert v.valid
    assert not v.harness_error
    assert v.score == 0
    assert v.blocked_by == "audit-gate"
    assert "could not be read" in " ".join(v.notes)


def test_reporting_a_ran_stage_does_not_move_where_the_ladder_stopped():
    """`blocked_by` names stage 1, and the stage-3 line reads it to say why.

    An incomplete behavioural result sets `blocked_by` to `behavioural-incomplete`.
    Letting that reach the real verdict would have the report explain a run that
    stopped at stage 1 with "stage 2 did not pass every scored check" -- true of the
    submission, and not why this run scored nothing.
    """
    v = grade("t", POLICY, audit(shim="fail"), behavioural(("m", 1, 0.1)))
    assert v.blocked_by == "audit-gate"
    assert v.metadata["behavioural_would_have_blocked"] == "behavioural-incomplete"


def test_the_gate_note_does_not_claim_later_stages_did_not_run():
    v = grade("t", POLICY, audit(shim="fail"), behavioural(("m", 1, 1.0)))
    joined = " ".join(v.notes)
    assert "no further stage was run" not in joined
    # The gate's own wording, which this spelled "no later stage contributes
    # points".  Same claim; `test_the_gate_note_does_not_say_what_it_cannot_know`
    # above pins the live phrasing, and two tests asserting one sentence in two
    # spellings means whichever is rewritten next leaves the other failing.
    assert "no later stage can earn anything" in joined


def test_a_genuinely_absent_behavioural_stage_is_still_absent():
    """The fix must not invent a stage 2 for a run where none happened."""
    v = grade("t", POLICY, audit(shim="fail"), None)
    assert "behavioural" not in v.stages_run
    assert not v.modules
    assert "behavioural_measured_not_counted" not in v.metadata


def test_an_undecided_gate_still_reports_a_clean_stage_two():
    """The case that distinguishes "re-run stage 1" from "re-run everything".

    A model outage invalidates the run, and the operator's next question is which
    stage to repeat.  A clean stage 2 in the report answers it.
    """
    v = grade("t", POLICY, audit(shim="error"), behavioural(("m", 1, 1.0)))
    assert not v.valid                      # unchanged: still asks to be re-run
    assert v.blocked_by == "audit-undecided"
    # Recorded as run and uncredited, not as scored -- see the test above.
    assert "behavioural" not in v.stages_run
    assert v.unscored["behavioural"]["counted"] is False
    assert v.metadata["behavioural_measured_points"] == pytest.approx(40.0)


# --------------------------------------------------------------------------- #
# the report
# --------------------------------------------------------------------------- #


def test_the_report_shows_the_module_table_it_used_to_deny():
    from swerefactor.report import render
    text = render(grade("t", POLICY, audit(shim="fail"),
                        behavioural(("behaviour", 1, 0.5))))
    assert "stage 2  behavioural modules ............. NOT RUN" not in text
    # "RAN, NOT COUNTED", where this asserted "MEASURED, NOT COUNTED": one state
    # had two spellings across the report and this was the minority one.
    assert "RAN, NOT COUNTED" in text
    assert "behaviour" in text
    # And the score line is still the truth about what was awarded.
    assert "score = 0.00  (stage 1 fail)" in text


def test_the_report_still_says_not_run_when_it_did_not():
    from swerefactor.report import render
    text = render(grade("t", POLICY, audit(shim="fail"), None))
    assert "stage 2  behavioural modules ............. NOT RUN" in text


def test_the_report_does_not_print_measured_points_as_awarded():
    """The header has to distinguish them, or two numbers appear to disagree."""
    from swerefactor.report import render
    text = render(grade("t", POLICY, audit(shim="fail"),
                        behavioural(("m", 1, 1.0))))
    # The figure may appear as a measured total, but the header line must not
    # read like the scored one -- that line is where a reader looks for an award.
    header = [ln for ln in text.splitlines()
              if "stage 2  behavioural modules" in ln][0]
    assert "RAN, NOT COUNTED" in header
    # The scored branch's shape: the dotted leader running straight into the
    # figure.  That is what an awarded stage 2 looks like, and it must not appear.
    assert "............. 40.00 points" not in text
    # The figure still appears, and still says of itself that it was not awarded.
    # This asserted "would have been 40.00 points", which is the other branch's
    # phrasing for the same thing; the words differ, the property does not.
    assert "40.00 points uncredited" in header
    assert "not credited" in header


# --------------------------------------------------------------------------- #
# a stage that ran and did not count
#
# The ladder short-circuits, so a failed gate returns before the behavioural result
# is read.  Under Harbor that matches the world: the skipped stage never launches.
# Driven by hand -- one stage at a time, to get a gate verdict and a behavioural
# number out of the same submission -- the result file is right there, and calling
# it "NOT RUN" is a false statement about a measurement the scorer is holding.
#
# The fix reports it and refuses to bank it.  Both halves need a test: the number
# has to appear, and it must not be reachable by anything that sums the score.
# --------------------------------------------------------------------------- #


def test_a_discarded_stage_is_recorded_rather_than_denied():
    v = grade("t", POLICY, audit(shim="fail"), behavioural(("m", 1, 1.0)))
    assert v.score == 0
    assert v.behavioural_points == 0          # the invariant above still holds
    assert v.uncounted["behavioural"]["ran"] is True
    assert v.uncounted["behavioural"]["points"] == pytest.approx(40.0)
    # ...and it is not claimed as a stage the ladder climbed.
    assert "behavioural" not in v.stages_run


@pytest.mark.parametrize("modules, rate, points", [
    ((("a", 3.0, 1.0), ("b", 1.0, 1.0)), 1.0, 40.0),
    ((("a", 3.0, 1.0), ("b", 1.0, 0.5)), 0.875, 0.0),
])
def test_the_uncounted_figure_is_the_one_the_stage_would_have_earned(
        modules, rate, points):
    """Reported by the real grader, not recomputed -- so it cannot drift from it.

    Both fixtures are needed.  The stage pays all or nothing, so a discarded reading
    and a counted one agree on 0.00 for every submission that is one check short, and
    a test built only on the second row would pass on two zeros no matter which
    number the uncounted dict actually carried.  The first row is the same equality
    where both sides are 40.00, and the rate below is what separates the rows: 0.875
    is three quarters of the weight at 1.0 and a quarter at 0.5.

    The counted side is read off `behavioural_rate` rather than out of metadata,
    because a counted verdict publishes no `behavioural_measured_*` keys -- those
    exist to carry a measurement that was thrown away, and a stage that counted has
    nowhere to throw one.
    """
    res = behavioural(*modules)
    counted = grade("t", POLICY, audit(shim="pass"), res)
    discarded = grade("t", POLICY, audit(shim="fail"), res)
    seen = discarded.uncounted["behavioural"]

    assert seen["points"] == pytest.approx(counted.behavioural_points)
    assert seen["points"] == pytest.approx(points)
    # The measurement, which is the reading the payment cannot express, so the two
    # paths have to agree on it as well -- otherwise the discarded verdict is the
    # only one of the pair a reader can learn anything from.
    assert seen["rate"] == pytest.approx(counted.behavioural_rate, abs=1e-6)
    assert seen["rate"] == pytest.approx(rate)
    assert seen["complete"] is (rate == 1.0)


def test_a_stage_that_really_did_not_run_stays_absent():
    """"NOT RUN" is still the honest rendering when there is no result file."""
    v = grade("t", POLICY, audit(shim="fail"), None)
    assert v.uncounted == {}


def test_a_discarded_zero_says_why_it_is_zero():
    """Otherwise a reported 0.00 cannot be told from a submission that failed.

    A 0.00 built from failing checks *is* a submission that failed, so the case worth
    pinning is the other one: the stage scored zero because its only module never
    finished.  Same number, and the only thing separating it from a genuine failure
    is this note.
    """
    res = behavioural()
    res.units.append(Unit(id="build", title="build", weight=1.0, status="error",
                          summary="the reference did not compile"))
    v = grade("t", POLICY, audit(shim="fail"), res)
    got = v.uncounted["behavioural"]
    assert got["points"] == 0.0
    # Read the raw entry, not `uncounted`.  Stage 2 was scored into a scratch verdict
    # here (the gate above it failed), so its notes land in the entry rather than on
    # `v`, and `uncounted` is a projection that keeps one sentence -- the last, which
    # is `behavioural-incomplete`'s about stage 3's availability, appended after this
    # one.  The reason a reader needs is the module's; it survives in `unscored`.
    raw = v.unscored["behavioural"]
    joined = " | ".join(str(n) for n in raw.get("notes") or [])
    assert "did not finish" in joined, raw.get("notes")
    assert "build" in joined, raw.get("notes")
    # The gate that actually stopped the ladder keeps the top-level field: it is the
    # rung the submission fell at, and stage 2's own shortfall is reported in the
    # notes above rather than claimed as a second stop beside it.
    assert v.blocked_by == "audit-gate"


def test_stage_three_is_uncounted_when_stage_two_did_not_earn_it():
    """The same defect one rung down: "stage 3 not run" where stage 3 ran.

    Six adversaries found nothing and are paid nothing, because stage 2 was one check
    short of the marks that admit a submission to stage 3.  Which makes this the
    largest discarded figure the report can be asked to render -- 60 points measured
    and withheld -- and the one most worth stating plainly.
    """
    # 995 of 1000 rather than a rounder rate: a submission that failed most of its
    # suite also scores zero, and this test has to show that the 60 points were
    # withheld by a stage 2 that nearly passed rather than by one that measured
    # nothing.  Ten checks cannot express the difference.
    v = grade("t", POLICY, audit(shim="pass"),
              behavioural(("m", 1, 0.995), checks=1000), verification(6))
    assert v.score == 0.0
    assert v.behavioural_rate == pytest.approx(0.995)
    assert v.verification_points == 0.0
    assert v.blocked_by == "behavioural-incomplete"
    assert v.uncounted["verification"]["points"] == pytest.approx(60.0)
    assert v.uncounted["verification"]["models_survived"] == 6
    # The rendering, which is the other place a reader meets this.  The verdict
    # fields above and the arithmetic line are written by different code off the
    # same state, so a stage line reading "RAN, NOT COUNTED  60.00 points" above an
    # arithmetic line reading "stage 3 not run" is a report disagreeing with itself.
    # Both are asserted, because agreeing with each other is the property.
    text = render(v)
    stage3 = next(ln for ln in text.splitlines() if "stage 3" in ln)
    assert "RAN, NOT COUNTED" in stage3
    assert "60.00 points" in stage3
    arithmetic = next(ln for ln in text.splitlines() if "score =" in ln)
    assert "stage 3 not counted" in arithmetic
    assert "not run" not in arithmetic


def test_uncounted_survives_the_json_round_trip():
    """The report is re-rendered from score.json, so the field has to be in both."""
    v = grade("t", POLICY, audit(shim="fail"), behavioural(("m", 1, 1.0)),
              verification(6))
    back = Verdict.from_dict(v.to_dict())
    # Assert there is something to carry before comparing: two empty maps are equal,
    # so without this the round trip passes on a build that records nothing at all.
    assert set(v.uncounted) == {"behavioural", "verification"}
    assert v.uncounted["behavioural"]["points"] == pytest.approx(40.0)
    assert back.uncounted == v.uncounted
    assert back.score == 0
    # An old score.json predates the field; empty means "nothing was discarded".
    assert Verdict.from_dict({"task": "t"}).uncounted == {}


def test_the_report_names_the_discarded_number_without_banking_it():
    v = grade("t", POLICY, audit(shim="fail"), behavioural(("m", 1, 1.0)))
    text = render(v)
    stage2 = next(ln for ln in text.splitlines() if "stage 2" in ln)
    assert "NOT COUNTED" in stage2
    assert "NOT RUN" not in stage2
    assert "40.00 points" in stage2               # stated
    assert "reported, not counted" in text        # and disowned
    assert "score = 0.00  (stage 1 fail)" in text
    assert "excluded from that total by policy, not missing from it" in text
    # Stage 3 handed in nothing here, so its "NOT RUN" is the honest rendering and
    # must survive the change -- the fix is about stages that did run.
    stage3 = next(ln for ln in text.splitlines() if "stage 3" in ln)
    assert "NOT RUN (the audit gate failed)" in stage3


def test_policy_rejects_arithmetic_that_does_not_add_up():
    from swerefactor.config import ConfigError
    with pytest.raises(ConfigError):
        # 6 * 4 = 24, not the 55 declared.  The numbers are picked to match no
        # ladder at all, so that a future retune does not read this as stale.
        ScoringPolicy.from_dict(
            {"verification_points": 55, "verification_models": 6,
             "points_per_survived_model": 4}, "test")
