"""A stage that ran must not be reported as never run.

The bug this locks down: `grade()` returns as soon as a required gate fails, and
the only thing that appends to `stages_run` is `grade_behavioural` -- so a stage 2
that ran, produced 2586 checks, and wrote its result file next to the verdict was
rendered as `stage 2 behavioural modules ... NOT RUN`.  Measured on pf02 run 3,
where the submission failed one gate *and* passed 42.07% of stage 2's scored
checks; only the first of those was visible, and the second is the one that
says whether the port works at all.

Two things are asserted here that a narrower test would miss.  The first is that
the report *renders* the distinction -- `report.render` had no test coverage of any
kind, which is how a false line survived in the first place, and asserting the
field alone would not have caught it.  The second is the negative control: a gate
failure with no stage-2 result at all must still say NOT RUN, or the new branch is
just an unconditional relabel that happens to look right on one run.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from swerefactor import report
from swerefactor.config import ScoringPolicy
from swerefactor.scoring import Verdict, grade
from tests.test_scoring import verification, behavioural, audit

POLICY = ScoringPolicy()

# Roughly pf02 run 3's shape: a wide module mostly failing, two clean ones.  The
# weights do not sum to 100 and do not need to; the stage normalises by the weight
# it was given, which is the property the numbers below depend on.
RUN3ISH = (("web-cases", 18.0, 0.0), ("node-cases", 11.0, 1.0),
           ("prepare", 4.0, 1.0))
EXPECT_RATE = (11.0 + 4.0) / 33.0           # 0.4545..., the two clean modules
# Not derived from the rate, because no rate maps to a payment: stage 2 pays for a
# submission that answered every scored check, and `web-cases` answered none.  The
# pair is the point of the fixture -- a stage that measured 0.4545 and was paid
# nothing is the only shape on which "reported, not counted" says anything.
EXPECT_POINTS = 0.0


# --------------------------------------------------------------------------- #
# a failed gate: stage 2 ran
# --------------------------------------------------------------------------- #


def test_failed_gate_still_records_what_stage_two_measured():
    v = grade("t", POLICY, audit(shim="fail"), behavioural(*RUN3ISH))
    u = v.unscored["behavioural"]
    assert u["counted"] is False
    assert u["status"] == "ok"
    assert u["points"] == round(EXPECT_POINTS, 4)
    assert u["rate"] == round(EXPECT_RATE, 6)
    assert {m["id"] for m in u["modules"]} == {"web-cases", "node-cases", "prepare"}


def test_recording_it_does_not_pay_for_it():
    """The whole risk of this feature: that the note becomes a number.

    Everything the score is computed from has to read exactly as it did before,
    which is why this asserts the scored fields and not just the total.
    """
    v = grade("t", POLICY, audit(shim="fail"), behavioural(*RUN3ISH),
              verification(6))
    assert v.score == 0
    assert v.to_dict()["reward"] == 0      # what Harbor reads as the headline
    assert v.behavioural_points == 0
    assert v.behavioural_rate == 0
    assert v.verification_points == 0
    # The module table IS carried, and that is not a payment.
    # `_report_behavioural_only` copies the real `ModuleScore`s onto the verdict, so
    # `flatten` emits true `module_*_rate` keys for a stage that ran -- a stage
    # whose rows were kept inside the dict would flatten to nothing and read as a
    # stage that never ran.  The rows are asserted present here, and the fields
    # above, which are the ones the score is computed from, are what must stay zero.
    assert [m.id for m in v.modules] == ["web-cases", "node-cases", "prepare"]
    from swerefactor.harbor import flatten
    flat = flatten(v)
    assert flat["module_node_cases_rate"] == 1.0    # measured
    assert flat["stage2_points"] == 0.0             # awarded
    assert flat["stage2_scored"] == 0
    assert flat["stage2_ran"] == 1
    assert v.stages_run == ["audit"]
    assert v.blocked_by == "audit-gate"
    assert v.valid            # a cheating submission is a valid zero, not an error


def test_no_stage_two_result_still_reads_not_run():
    """The negative control.

    Without this, the new branch could be an unconditional relabel that happens to
    look right whenever a result exists.
    """
    v = grade("t", POLICY, audit(shim="fail"), None)
    assert "behavioural" not in v.unscored
    assert "NOT RUN" in report.render(v)
    assert "NOT COUNTED" not in report.render(v)


# --------------------------------------------------------------------------- #
# the report is the artifact a person reads, so assert the rendered text
# --------------------------------------------------------------------------- #


def test_the_report_says_ran_not_counted_not_not_run():
    v = grade("t", POLICY, audit(shim="fail"), behavioural(*RUN3ISH))
    text = report.render(v)
    # Matched on the header text, not on "stage 2".  This filtered on the bare
    # phrase and asserted exactly one line, to catch a report printing both a NOT
    # RUN line and a RAN, NOT COUNTED line for the same stage.  The arithmetic block
    # now also names stage 2 -- "stage 2 ran: those measurements are excluded from
    # that total by policy" -- which is a second mention and not a second verdict, so
    # the count is pinned on the header instead and the NOT RUN check below is what
    # actually holds the property.
    stage2 = [ln for ln in text.splitlines() if "stage 2  behavioural modules" in ln]
    assert len(stage2) == 1
    assert "RAN, NOT COUNTED" in stage2[0]
    assert "NOT RUN" not in text.split("stage 3")[0]
    assert f"{EXPECT_POINTS:.2f}" in stage2[0]
    # The arithmetic line still has to say zero: the report must not read as
    # though the uncounted points were awarded.
    assert "score = 0.00  (stage 1 fail)" in text


def test_the_module_table_renders_for_an_uncounted_stage():
    """The per-module rates are the diagnostic; a bare total does not locate a port.

    Ordered by descending weight, matching the counted table, so the two do not
    have to be read differently.
    """
    res = behavioural(*RUN3ISH)
    # Left set on purpose.  A stage may write what it likes into its own module
    # metadata, so this is the input that would put a `*` marker on the row if the
    # reader honoured it -- a marker meaning "one failure here zeroes the stage",
    # which is true of every weighted module and so distinguishes none of them.
    for unit in res.units:
        if unit.id == "prepare":
            unit.metadata["required"] = True
    text = report.render(grade("t", POLICY, audit(shim="fail"), res))
    # Anchored at the row's own position rather than anywhere in the line: the
    # stage-2 note names the module that fell short, and it wraps to a continuation
    # line indented past the table, which a substring test collects as a row.
    rows = [ln for ln in text.splitlines()
            if any(ln.startswith("    " + m) for m, _, _ in RUN3ISH)]
    assert [ln.split()[0] for ln in rows] == ["web-cases", "node-cases", "prepare"]
    assert "0.0000" in rows[0] and "1.0000" in rows[1]
    assert not any(ln.rstrip().endswith("*") for ln in rows), rows


def test_stage_three_not_run_is_unaffected():
    """Nothing exists for stage 3 here, and it must keep saying so."""
    text = report.render(grade("t", POLICY, audit(shim="fail"),
                              behavioural(*RUN3ISH)))
    stage3 = [ln for ln in text.splitlines() if "stage 3" in ln]
    assert len(stage3) == 1
    assert "NOT RUN (the audit gate failed)" in stage3[0]


def test_it_survives_the_json_round_trip():
    """`swerefactor report` re-renders from the file, so the field has to persist.

    A verdict written by one container and rendered by another is the normal path
    -- the scoring stage writes score.json and a human re-renders it later.
    """
    v = grade("t", POLICY, audit(shim="fail"), behavioural(*RUN3ISH))
    again = Verdict.from_dict(v.to_dict())
    assert again.unscored == v.unscored
    assert report.render(again) == report.render(v)


def test_an_old_verdict_without_the_field_still_renders():
    """Reading a score.json written before this field existed must not crash.

    All three keys are removed, because that is what "before this field existed"
    means: three branches built this field independently and named it `unscored`,
    `uncredited` and `uncounted`, `to_dict` now writes all three, and deleting some
    of them leaves the rest -- which is a verdict that *has* the field, not one that
    predates it.  The count went from two to three when the third branch merged, and
    a test that keeps deleting two would quietly stop testing what it says it does.
    """
    raw = grade("t", POLICY, audit(shim="fail"), behavioural(*RUN3ISH)).to_dict()
    del raw["unscored"]
    del raw["uncredited"]
    del raw["uncounted"]
    v = Verdict.from_dict(raw)
    assert v.unscored == {}
    assert "NOT RUN" in report.render(v)


def test_a_verdict_carrying_only_the_other_spelling_is_still_read():
    """The other half of that: a file written by the branch that named it
    `uncredited` has to load, or re-rendering one drops the measurement and
    reports NOT RUN about a stage whose numbers are in the file being read.
    """
    raw = grade("t", POLICY, audit(shim="fail"), behavioural(*RUN3ISH)).to_dict()
    del raw["unscored"]
    v = Verdict.from_dict(raw)
    assert v.unscored == raw["uncredited"]
    assert v.uncredited == raw["uncredited"], "the alias reads the same dict"
    assert "RAN, NOT COUNTED" in report.render(v)


def test_a_verdict_carrying_only_the_third_spelling_is_still_read():
    """And the same for `uncounted`, which is not merely a third name.

    It reports `modules` as a count where the other two carry the module rows, and
    `report.render` iterates those rows -- so restoring an int there gives a verdict
    that renders when the scorer builds it and raises `TypeError` when re-read from
    its own JSON.  A file with only this key keeps its count under `module_count`,
    and the module table is absent rather than broken: that file never stored rows.
    """
    raw = grade("t", POLICY, audit(shim="fail"), behavioural(*RUN3ISH)).to_dict()
    del raw["unscored"]
    del raw["uncredited"]
    v = Verdict.from_dict(raw)
    got = v.unscored["behavioural"]
    assert got["points"] == round(EXPECT_POINTS, 4)
    assert got["counted"] is False
    assert not isinstance(got.get("modules"), int), \
        "an int here is what report.render iterates"
    assert got["module_count"] == len(RUN3ISH)
    # The projection reads that fallback, so the count survives the round trip.
    assert v.uncounted["behavioural"]["modules"] == len(RUN3ISH)
    text = report.render(v)
    assert "RAN, NOT COUNTED" in text
    assert f"{EXPECT_POINTS:.2f}" in text


# --------------------------------------------------------------------------- #
# the report must not name a cause it does not have
# --------------------------------------------------------------------------- #


def test_a_clean_run_without_stage_three_blames_nobody():
    """Found by the same run: stage 3 absent, and the report invented a cause.

    Stages 1 and 2 both passed and nothing stopped the ladder -- `blocked_by` is
    empty -- yet the line read "NOT RUN (an earlier stage stopped the ladder)",
    contradicted by this verdict's own note four lines below it.  Running stages 1
    and 2 without 3 is the ordinary case when driving one task by hand.
    """
    v = grade("t", POLICY, audit(shim="pass"), behavioural(("m", 1.0, 1.0)))
    assert v.blocked_by == ""
    assert v.score == POLICY.behavioural_points
    text = report.render(v)
    assert "no verification result was produced" in text
    assert "an earlier stage stopped the ladder" not in text


def test_a_known_reason_is_still_translated():
    """The guard on the fix: `or` must not swallow a mapped reason.

    `.get(k) or fallback` differs from `.get(k, fallback)` for any key whose value
    is falsy, so the mapping has to be exercised, not just the new branch.
    """
    v = grade("t", POLICY, audit(shim="fail"), behavioural(*RUN3ISH))
    assert v.blocked_by == "audit-gate"
    assert "the audit gate failed" in report.render(v)


def test_an_unrecognised_reason_reaches_the_reader():
    v = grade("t", POLICY, audit(shim="pass"), behavioural(("m", 1.0, 1.0)))
    v.blocked_by = "some-rung-added-later"
    text = report.render(v)
    assert "some-rung-added-later" in text
    assert "no reason recorded" not in text


# --------------------------------------------------------------------------- #
# the other rung that stops the ladder
# --------------------------------------------------------------------------- #


def test_an_incomplete_stage_two_records_stage_three_the_same_way():
    """Stage 2 stops stage 3, which is the analogous false NOT RUN."""
    v = grade("t", POLICY, audit(shim="pass"), behavioural(*RUN3ISH),
              verification(6))
    assert v.blocked_by == "behavioural-incomplete"
    u = v.unscored["verification"]
    assert u["counted"] is False
    assert u["models_survived"] == 6
    assert v.verification_points == 0           # measured, still not paid
    assert v.score == v.behavioural_points
    stage3 = [ln for ln in report.render(v).splitlines() if "stage 3" in ln]
    assert "RAN, NOT COUNTED" in stage3[0]
    assert ("not counted: a behavioural module did not pass every scored check."
            in report.render(v))


def test_a_stage_that_cannot_be_summarised_does_not_break_the_verdict(monkeypatch):
    """Annotating must never be able to fail the thing it annotates.

    Injected rather than contrived from a malformed result: the shapes that are
    merely malformed (`units` empty, `status != ok`) are all handled before the
    arithmetic and return cleanly, so the only honest way to reach the defensive
    branch is to make the summarising call itself raise.  What is being asserted is
    that a zero stays a valid zero when it does.
    """
    from swerefactor import scoring

    def boom(*_a, **_kw):
        raise RuntimeError("weights are unusable")

    monkeypatch.setattr(scoring, "grade_behavioural", boom)
    v = grade("t", POLICY, audit(shim="fail"), behavioural(*RUN3ISH))
    assert v.score == 0 and v.valid
    assert v.unscored["behavioural"]["error"] == "RuntimeError: weights are unusable"
    assert "could not be summarised" in report.render(v)
