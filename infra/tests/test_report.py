"""The rendered report, tested where it makes a claim of its own.

`render` mostly transcribes fields, and transcription does not need a test.  The
exception is the stage-3 "NOT RUN" line: there the report *explains* an absence,
and an explanation can be wrong in a way the JSON never is.  A grading run that
was simply never asked for stage 3 is the common case -- `verify.sh <task> <sub>
<out> audit behavioural score` -- and it must not be reported as a failure.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from swerefactor.config import ScoringPolicy
from swerefactor.report import render
from swerefactor.result import Check, StageResult, Unit
from swerefactor.scoring import grade

POLICY = ScoringPolicy()

# The sentence this file exists to keep out of a clean run's report.
INVENTED = "an earlier stage stopped the ladder"


def _mapped_reasons() -> list[str]:
    """Every cause `render` can attribute a missing stage 3 to.

    Read out of the source rather than restated here.  A copy would let the table
    grow a new wrong-cause entry that this file still passes, which is the failure
    mode being tested: the table is the thing that must not be reachable from an
    unblocked run, so the test has to see the real one.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(sys.modules["swerefactor.report"]))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict) or not node.keys:
            continue
        keys = [k.value for k in node.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)]
        if "audit-gate" in keys and "behavioural-incomplete" in keys:
            return [v.value for v in node.values
                    if isinstance(v, ast.Constant) and isinstance(v.value, str)]
    raise AssertionError(
        "could not find the blocked_by cause table in swerefactor.report; if it moved "
        "or changed shape, this test is no longer checking anything"
    )


def _audit(**gates: str) -> StageResult:
    res = StageResult(stage="audit", task="t", status="ok")
    for gate, verdict in gates.items():
        res.add(Check(id=gate, verdict=verdict, summary=verdict, required=True))
    return res


def _behavioural(rate: float = 1.0) -> StageResult:
    res = StageResult(stage="behavioural", task="t", status="ok")
    res.units.append(Unit(id="m", title="m", weight=1.0, status="ok"))
    passing = round(rate * 10)
    for i in range(10):
        res.add(Check(id=f"m/c{i}", unit="m",
                      verdict="pass" if i < passing else "fail", summary=""))
    return res


def _stage3_line(text: str) -> str:
    return next(ln for ln in text.splitlines() if "stage 3" in ln)


def test_stage3_absent_on_a_passing_run_names_no_cause():
    """The regression: a clean ladder run that stopped after stage 2 by request.

    This is the shape `verify.sh ... audit behavioural score` produces, which
    is how every stage-1/stage-2-only grading run is invoked.  Nothing blocked
    anything -- stage 1 passed and stage 2 passed every check -- so the report may
    not say something did.
    """
    v = grade("t", POLICY, _audit(shim="pass"), _behavioural(1.0))

    assert v.audit_gate == "pass"
    assert v.blocked_by == ""          # nothing blocked the ladder
    assert v.valid
    assert "verification" not in v.stages_run

    line = _stage3_line(render(v))
    assert "NOT RUN" in line
    assert INVENTED not in line, line
    # Both halves of the sentence, because the defect being fixed is "the report
    # named a cause it did not have" and only the denial rules that out.  Pinning
    # the first clause alone admits any replacement cause -- including the retired
    # sentence under a synonym, and including re-accusing a stage that passed.
    assert "no stage-3 result was collected; no earlier stage blocked it" in line, line
    # Nothing from the cause table may appear on a run where nothing blocked
    # anything.  This is the general form of the assert above: it fails for every
    # mapped reason, not just the one spelling INVENTED holds.
    for reason in _mapped_reasons():
        assert reason not in line, f"named a mapped cause on an unblocked run: {reason}"


def test_a_real_block_is_still_explained():
    """The fallback must not have cost us the genuine causes."""
    v = grade("t", POLICY, _audit(shim="fail"), _behavioural(1.0))
    assert v.blocked_by == "audit-gate"
    assert "the audit gate failed" in _stage3_line(render(v))

    v = grade("t", POLICY, _audit(shim="pass"), _behavioural(0.1))
    assert v.blocked_by == "behavioural-incomplete"
    assert "did not pass every scored check" in _stage3_line(render(v))


def test_an_unmapped_block_reason_is_shown_verbatim():
    """A `blocked_by` this table has not learned yet must survive to the reader.

    Renaming a reason upstream should degrade to showing the raw token, not to
    swallowing it and printing a guess.
    """
    v = grade("t", POLICY, _audit(shim="pass"), _behavioural(1.0))
    v.blocked_by = "some-future-reason"
    line = _stage3_line(render(v))
    assert "some-future-reason" in line, line
    assert INVENTED not in line, line


def test_a_harness_fault_is_named_rather_than_blamed_on_a_stage():
    """Stage 3 was asked for and the harness broke: say so, don't accuse stage 1."""
    v = grade("t", POLICY, _audit(shim="pass"), _behavioural(1.0))
    v.harness_error = "the verification image is not present"
    line = _stage3_line(render(v))
    assert "the verification image is not present" in line, line
    assert INVENTED not in line, line


# --- who graded it, in the footer -------------------------------------------


def _footer(text: str) -> str:
    return next((ln for ln in text.splitlines() if "graded by" in ln), "")


def test_the_footer_names_the_harness_each_stage_recorded():
    v = grade("t", POLICY, _audit(shim="pass"), _behavioural(1.0))
    import swerefactor

    assert swerefactor.fingerprint() in _footer(render(v))


def test_stages_graded_by_different_harnesses_are_not_reconciled():
    """Two builds graded one submission; the report may not pick one and move on.

    The sixty stage images take the harness from a mutable tag, so this is a
    reachable state rather than a hypothetical, and it is the state in which a
    disagreement between two stages cannot be attributed to either.
    """
    v = grade("t", POLICY, _audit(shim="pass"), _behavioural(1.0))
    v.harnesses = [{"fingerprint": "aaaaaaaaaaaa", "stage": "audit"},
                   {"fingerprint": "bbbbbbbbbbbb", "stage": "behavioural"}]
    line = _footer(render(v))
    assert "aaaaaaaaaaaa" in line and "bbbbbbbbbbbb" in line, line
    assert "disagree" in line, line


def test_a_verdict_without_provenance_claims_none():
    """No line at all beats a line reading "unknown", and beats naming this build.

    Every score.json written before the field existed lands here.
    """
    v = grade("t", POLICY, _audit(shim="pass"), _behavioural(1.0))
    v.harnesses = []
    assert _footer(render(v)) == ""


def test_provenance_survives_a_score_json_round_trip(tmp_path):
    """The footer has to hold for a report re-rendered from disk, which is how
    every report other than the grading run's own is produced."""
    from swerefactor.scoring import Verdict

    v = grade("t", POLICY, _audit(shim="pass"), _behavioural(1.0))
    expected = _footer(render(v))
    assert expected

    back = Verdict.read(v.write(tmp_path / "score.json"))
    assert back.harnesses == v.harnesses
    assert _footer(render(back)) == expected
