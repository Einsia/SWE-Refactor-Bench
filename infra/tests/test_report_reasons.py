"""The report must not name a cause the verdict does not record.

`report.py` had no test of any kind.

Three fixes before this one were kept as patch files and all three were lost. A
patch is not a mechanism -- nothing fails when someone reverts it -- so the
property lives here instead, where reverting it is loud.

Everything below asserts on **rendered output**, never on report.py's text. A
grep cannot tell a sentence the report prints from a comment about one, and an
honest denial has to mention an earlier stage in order to deny that one blocked
anything, so both `in source` and `not in source` give the wrong answer on a
correct file.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from swerefactor.config import ScoringPolicy
from swerefactor.report import render
from swerefactor.result import Check, StageResult, Unit
from swerefactor.scoring import Verdict, grade

POLICY = ScoringPolicy()

# The wordings that assert an upstream failure.  A report may only use one of
# these when the verdict actually names a blocker.
BLAME = ("an earlier stage", "earlier stage stopped", "stopped the ladder")


def audit(**gates: str) -> StageResult:
    res = StageResult(stage="audit", task="t", status="ok")
    for gate, verdict in gates.items():
        res.add(Check(id=gate, verdict=verdict, summary=verdict, required=True))
    return res


def behavioural(*modules: tuple[str, float, float]) -> StageResult:
    res = StageResult(stage="behavioural", task="t", status="ok")
    for mid, weight, module_rate in modules:
        res.units.append(Unit(id=mid, title=mid, weight=weight, status="ok"))
        passing = round(module_rate * 10)
        for i in range(10):
            res.add(Check(id=f"{mid}/c{i}", unit=mid,
                          verdict="pass" if i < passing else "fail", summary=""))
    return res


def stage3_line(verdict: Verdict) -> str:
    for line in render(verdict).splitlines():
        if "stage 3" in line:
            return line.strip()
    raise AssertionError("render() emitted no stage-3 line")


def healthy_ladder() -> Verdict:
    """Stages 1 and 2 pass; stage 3 was simply not run.

    This is the ordinary state of a ladder driven for two stages, and it is the
    exact shape the bug fired on -- four times, each under a passing gate.
    """
    return grade("t", POLICY, audit(g="pass"), behavioural(("m", 1.0, 1.0)), None)


# --------------------------------------------------------------------------- #
# the defect itself
# --------------------------------------------------------------------------- #


def test_open_ladder_does_not_blame_an_earlier_stage():
    v = healthy_ladder()
    assert v.blocked_by == "", "precondition: nothing blocked this ladder"
    assert v.audit_gate == "pass"

    line = stage3_line(v)
    for phrase in BLAME:
        assert phrase not in line, (
            f"stage-3 line asserts a blocker that blocked_by does not record: {line}"
        )
    assert "NOT RUN" in line


def test_open_ladder_does_not_contradict_its_own_notes():
    """The strongest form: the bug made one report state both things.

    ``grade_verification`` appends "the verification stage produced no result
    file" when handed None, and the same render printed a blocker four rows
    above it.  A reader could not tell which half to believe.
    """
    v = healthy_ladder()
    text = render(v)
    assert any("produced no result file" in n for n in v.notes), "precondition"
    said_no_result = "produced no result file" in text
    blamed_upstream = any(p in stage3_line(v) for p in BLAME)
    assert not (said_no_result and blamed_upstream), (
        "the report blames an earlier stage and also reports that no earlier "
        "stage failed:\n" + text
    )


# --------------------------------------------------------------------------- #
# and the half a one-sided fix would break
# --------------------------------------------------------------------------- #


def test_a_real_blocker_still_names_itself():
    v = grade("t", POLICY, audit(g="fail"), None, None)
    assert v.blocked_by == "audit-gate", "precondition"
    assert "the audit gate failed" in stage3_line(v)


def test_an_undecidable_gate_still_names_itself():
    """An undecided gate names itself rather than a generic line."""
    v = grade("t", POLICY, audit(g="error"), None, None)
    assert v.blocked_by == "audit-undecided", "precondition"
    assert "could not be decided" in stage3_line(v)


def test_a_zeroed_module_names_the_incomplete_stage():
    """The one blocker a module at zero can set, asserted end to end.

    A module at zero stops the ladder one way and one way only -- by leaving a
    scored check unanswered -- and the sentence has to be that one, naming the rule
    and not anything the module said about itself.
    """
    res = behavioural(("m", 1.0, 0.0))
    # Set, and expected to change nothing: a stage may write what it likes into its
    # own module metadata, and none of it may steer the blocker.
    res.units[0].metadata["required"] = True
    v = grade("t", POLICY, audit(g="pass"), res, None)
    assert v.blocked_by == "behavioural-incomplete", "precondition"
    assert "did not pass every scored check" in stage3_line(v)
    assert "required" not in stage3_line(v)


@pytest.mark.parametrize("blocked_by,expected", [
    ("audit-gate", "the audit gate failed"),
    ("audit-missing", "the audit stage produced no result"),
    ("audit-error", "the audit stage did not complete"),
    ("audit-undecided", "the audit gate could not be decided"),
    ("audit-empty", "the audit gate declares no required checks"),
    ("behavioural-missing", "the behavioural stage produced no result"),
    ("behavioural-error", "the behavioural stage did not complete"),
    ("behavioural-empty", "the behavioural stage declared no modules"),
    ("behavioural-weights", "the behavioural module weights are unusable"),
    ("behavioural-incomplete", "a behavioural module did not pass every scored check"),
])
def test_every_blocker_scoring_can_assign_has_a_sentence(blocked_by, expected):
    v = healthy_ladder()
    v.blocked_by = blocked_by
    assert expected in stage3_line(v)


def test_an_unmapped_blocker_prints_verbatim_rather_than_inventing_one():
    """A blocker added to scoring.py and not to report.py must still be legible."""
    v = healthy_ladder()
    v.blocked_by = "some-blocker-added-later"
    line = stage3_line(v)
    assert "some-blocker-added-later" in line
    for phrase in BLAME:
        assert phrase not in line


def test_the_mapping_covers_every_id_scoring_can_assign():
    """Guard against the two files drifting apart.

    Every ``verdict.blocked_by = "..."`` in scoring.py must render as prose, not
    as a bare id -- otherwise the next blocker reaches a human as jargon.
    """
    import re

    src = (Path(__file__).resolve().parents[1] / "swerefactor" / "scoring.py").read_text()
    assigned = set(re.findall(r'blocked_by\s*=\s*"([a-z-]+)"', src))
    assert assigned, "found no blocked_by assignments; did scoring.py move?"

    for bid in sorted(assigned):
        v = healthy_ladder()
        v.blocked_by = bid
        line = stage3_line(v)
        assert bid not in line, (
            f"blocked_by={bid!r} reaches the reader as a raw id; add it to "
            f"report.py's mapping: {line}"
        )


# --------------------------------------------------------------------------- #
# the stage-2 line has the same shape, and is worth pinning while we are here
# --------------------------------------------------------------------------- #


def test_stage_2_not_run_does_not_invent_a_cause_either():
    v = grade("t", POLICY, audit(g="pass"), None, None)
    assert v.blocked_by == "behavioural-missing", "precondition"
    line = next(ln for ln in render(v).splitlines() if "stage 2" in ln)
    assert "NOT RUN" in line


def stage2_line(verdict: Verdict) -> str:
    for line in render(verdict).splitlines():
        if "stage 2" in line and "....." in line:
            return line.strip()
    raise AssertionError("render() emitted no stage-2 header line")


def broken_stage2(note: str) -> StageResult:
    """Stage 2 ran, failed before any module, and recorded why.

    The shape `ladder._record_stage_failure` writes: status error, no units, no
    checks, and the cause under `metadata["error"]`.  Ten runs in the 0807
    campaign were this exact state -- a `docker cp` into the stage container that
    broke its pipe -- and two were the stage's own 12600s timeout.
    """
    res = StageResult(stage="behavioural", task="t", status="error")
    res.metadata["error"] = note
    res.metadata["recorded_by"] = "ladder"
    return res


def test_a_failed_gate_does_not_take_credit_for_stage_2_breaking():
    """The twelve published runs, as a property.

    Both facts are true here -- the gate failed AND stage 2 broke -- which is
    what made the wrong sentence so quiet: nothing on the page contradicted it.
    The test therefore asserts on the stage-2 line naming its own cause, not on
    the absence of the gate elsewhere in the report.
    """
    v = grade("t", POLICY, audit(g="fail"),
              broken_stage2("docker cp into the stage container broke its pipe"),
              None)
    assert v.blocked_by == "audit-gate", "precondition: the gate did fail"
    line = stage2_line(v)
    assert "broke its pipe" in line, (
        f"the stage's own recorded cause did not reach the report: {line}")
    assert "the audit gate failed" not in line, (
        f"stage 2's line blames the gate for a failure the gate did not cause: "
        f"{line}")
    assert "NOT RUN" not in line, (
        f"a stage that ran and left a result is reported as never having run: "
        f"{line}")


def test_a_stage_that_really_never_ran_still_names_the_gate():
    """The converse leak, and the reason the label is not simply reworded.

    With no stage-2 result at all there is no observed cause, and the gate IS the
    reason nothing ran.  A fix that always prefers the stage's own account would
    print an empty parenthesis here.
    """
    v = grade("t", POLICY, audit(g="fail"), None, None)
    line = stage2_line(v)
    assert "NOT RUN" in line
    assert "the audit gate failed" in line


def test_a_measured_stage_2_under_a_failed_gate_is_unaffected():
    """The fix must not reach the path that already worked.

    A stage 2 that produced a module table is reported as RAN, NOT COUNTED with
    its rate; that branch is upstream of the reason text and stays there.
    """
    v = grade("t", POLICY, audit(g="fail"), behavioural(("m", 1.0, 0.8)), None)
    line = stage2_line(v)
    assert "RAN, NOT COUNTED" in line
    assert "NO RESULT" not in line and "NOT RUN" not in line


# --------------------------------------------------------------------------- #
# and the leak in the other direction
# --------------------------------------------------------------------------- #

# The open-ladder sentence itself, as rendered.  Kept as a constant so a reword
# has one place to update and cannot half-land.
ABSENT = "no stage-3 result was collected"


def test_the_open_ladder_line_says_what_did_happen():
    """Not blaming anyone is half the property; the other half is naming the state.

    A line reading only ``NOT RUN`` passes every BLAME assertion above and still
    tells a reader nothing, so the absence has to be stated positively too.
    """
    assert ABSENT in stage3_line(healthy_ladder())


def test_a_blocked_ladder_does_not_also_claim_the_result_was_never_collected():
    """The converse leak: one sentence per state, not both.

    A resolution that appends the open-ladder wording unconditionally satisfies
    every test above -- the blocker is still named -- while telling a reader that
    nothing stopped the ladder on a run where stage 1 failed.
    """
    v = grade("t", POLICY, audit(g="fail"), None, None)
    assert v.blocked_by == "audit-gate", "precondition"
    line = stage3_line(v)
    assert "the audit gate failed" in line
    assert ABSENT not in line, f"a blocked run also claims an open ladder: {line}"
