"""Stage 3 was payable-only.  Now it is also runnable-only, above both gates.

``scoring.grade`` refuses to pay for an verification result from a submission that
did not pass every scored check in stage 2.  Nothing refused to *run* one, so an
operator driving the three stages in sequence spent six adversary rounds -- six
model runs, each against a built tree -- producing a file the scorer was
guaranteed to discard.  ``cmd_verification`` now asks the scorer first.

Both rungs are asked, not only stage 2.  A submission whose audit gate
failed scores zero whatever stage 2 measured, so its rounds are discarded just as
surely -- and because stage 2 is the one stage that needs no credentials, "stage 1
failed and stage 2 ran anyway" is the ordinary shape of a hand-driven ladder
rather than a contrived one.  The tests below therefore pin *which* rung declined,
since an operator told about a stage 2 that could not have mattered goes looking at
the wrong stage.

The property tested hardest is the one whose absence is silent.  A gate that
writes a ``status="skip"`` file instead of no file at all grades as
``verification-error``: a harness fault charged to a submission whose only fault
was an earlier stage.  Both spellings pass a test that merely checks the rounds
did not run, so the tests below assert on what the *scorer* then makes of the
results directory, not only on what the command did.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from swerefactor import cli, scoring
from swerefactor.result import Check, StageResult, Unit

# --------------------------------------------------------------------------- #
# the stage-3 entry condition, enforced at the point of running
# --------------------------------------------------------------------------- #

EVALUATION_TOML = """\
schema = "swerefactor.evaluation/1"
task = "t"
results_dir = "RESULTS"

[scoring]
max_score = 100.0
behavioural_points = 40.0
verification_points = 60.0
verification_models = 6
points_per_survived_model = 10.0

[stages.audit]
runner = "audit"
result = "audit.json"
[[stages.audit.gate]]
id = "default_path"
required = true

[stages.behavioural]
title = "the behavioural suite"
timeout_sec = 600
image = "swerefactor/t-behavioural:1"
context = "behavioural"
result = "behavioural.json"
suite = "behavioural/suite.toml"

[stages.verification]
title = "the verification rounds"
result = "verification.json"
probe = "verification/probe.toml"
"""

PROBE_TOML = """\
schema = "swerefactor.verification-probe/1"
task = "t"

[[adversary]]
id = "a1"
model = "claude-sonnet-5"
driver = "anthropic"
"""


def _task_tree(tmp_path: Path) -> Path:
    tests = tmp_path / "tests"
    (tests / "verification").mkdir(parents=True)
    (tests / "evaluation.toml").write_text(
        EVALUATION_TOML.replace("RESULTS", str(tmp_path / "results")),
        encoding="utf-8")
    (tests / "verification" / "probe.toml").write_text(PROBE_TOML, encoding="utf-8")
    for name in ("repo", "original"):
        (tmp_path / name).mkdir()
    return tmp_path


def _namespace(task_dir: Path, **over) -> argparse.Namespace:
    """The namespace the parser builds for ``verification``, not a hand-made one.

    Built through ``build_parser`` on purpose: a flag added to the subcommand and
    not to this fixture is the failure mode these tests exist to catch, and a
    literal namespace here would hide it.
    """
    args = cli.build_parser().parse_args([
        "verification", "--task-dir", str(task_dir),
        "--results", str(task_dir / "results"),
        "--repo", str(task_dir / "repo"),
        "--original", str(task_dir / "original"),
        "--work", str(task_dir / "work"),
    ])
    for key, value in over.items():
        setattr(args, key, value)
    return args


def _behavioural(tmp_path: Path, rate: float, *, status: str = "ok",
                required: bool = True, weight: float = 1.0) -> Path:
    """A stage-2 result paying ``rate`` of its one module, written where stage 3 looks."""
    res = StageResult(stage="behavioural", task="t", status=status)
    res.units.append(Unit(id="m", title="the one module", weight=weight,
                          metadata={"required": required}))
    passed = int(round(rate * 10))
    for i in range(10):
        res.add(Check(id=f"m/c{i}", unit="m",
                      verdict="pass" if i < passed else "fail",
                      summary="", weight=1.0))
    out = tmp_path / "results" / "behavioural.json"
    res.write(out)
    return out


def _audit(tmp_path: Path, verdict: str = "pass", *,
               status: str = "ok", required: bool = True) -> Path:
    """A stage-1 result where stage 3 looks, deciding the gate ``verdict`` way.

    Written by every test whose subject is *stage 2*, because the rung below it is
    asked first now: a task tree with no ``audit.json`` declines at
    ``audit-missing`` and never reaches the arithmetic those tests are about.
    """
    res = StageResult(stage="audit", task="t", status=status)
    res.add(Check(id="default_path", unit="", verdict=verdict,
                  required=required, summary="the declared gate"))
    out = tmp_path / "results" / "audit.json"
    res.write(out)
    return out


def test_a_full_marks_stage_two_reaches_the_gate(tmp_path):
    _task_tree(tmp_path)
    res = StageResult.read(_behavioural(tmp_path, 1.0))
    reached, points, why = scoring.behavioural_reaches_gate(
        res, scoring.ScoringPolicy(), "t")
    assert reached, why
    assert points == pytest.approx(40.0)


def test_one_failed_check_does_not_reach_the_gate(tmp_path):
    """The shipped policy is the whole stage, so 9 of 10 does not reach it.

    Worth its own case because the paid figure cannot tell this apart from a tree
    that never compiled -- both are 0.0.  The measured rate is the only thing that
    separates them, so the sentence an operator reads has to carry it, and that is
    what is asserted rather than the points.
    """
    _task_tree(tmp_path)
    res = StageResult.read(_behavioural(tmp_path, 0.9))
    reached, points, why = scoring.behavioural_reaches_gate(
        res, scoring.ScoringPolicy(), "t")
    assert not reached
    assert points == 0.0
    assert "did not pass every scored check" in why
    assert "0.9000" in why, why


def test_the_reason_names_the_module_error_rather_than_a_failed_check(tmp_path):
    """A required module that never ran is not a submission that scored badly.

    The distinction is the one ``grade_behavioural`` draws with its own
    ``blocked_by`` ids, and it is why this reason is taken from the grader instead
    of composed here: an operator told "did not pass every scored check" for a run
    whose container died goes looking at the tree under test.
    """
    _task_tree(tmp_path)
    res = StageResult.read(_behavioural(tmp_path, 1.0, status="error"))
    reached, _points, why = scoring.behavioural_reaches_gate(
        res, scoring.ScoringPolicy(), "t")
    assert not reached
    assert "behavioural-error" in why
    assert "did not pass every scored check" not in why


def test_an_absent_stage_two_does_not_reach_the_gate(tmp_path):
    reached, points, why = scoring.behavioural_reaches_gate(
        None, scoring.ScoringPolicy(), "t")
    assert not reached and points == 0.0
    assert "no result file" in why


def test_the_gate_does_not_mutate_the_result_it_was_handed(tmp_path):
    """It grades through a throwaway verdict; the caller's result is evidence.

    ``grade_behavioural`` assigns onto the verdict it is given and reads the
    result, and this pins that: stage 3 asking whether it may run must not be
    able to change what stage 2 recorded, since the same file is what
    ``cmd_score`` grades afterwards.
    """
    _task_tree(tmp_path)
    path = _behavioural(tmp_path, 0.9)
    before = path.read_bytes()
    res = StageResult.read(path)
    scoring.behavioural_reaches_gate(res, scoring.ScoringPolicy(), "t")
    res.write(path)
    assert path.read_bytes() == before


def test_stage_three_writes_no_file_below_the_gate(tmp_path, capsys):
    """No file, not a skip file.

    A ``status="skip"`` result is read back by ``_read_stage`` and graded as
    ``status != "ok"``, which is ``verification-error``: a harness fault on a
    submission whose only fault was stage 2.  Absent is what the scorer already
    means by "the ladder correctly stopped below here", so absent is what this
    writes -- and the assertion is on the directory, because a test that only
    checked the exit code passes for either spelling.
    """
    task_dir = _task_tree(tmp_path)
    _audit(tmp_path)
    _behavioural(tmp_path, 0.9)
    rc = cli.cmd_verification(_namespace(task_dir))
    assert rc == 0
    out = capsys.readouterr().out
    assert not (tmp_path / "results" / "verification.json").exists(), out
    assert [p.name for p in sorted((tmp_path / "results").glob("*.json"))] \
        == ["audit.json", "behavioural.json"]
    assert "not run" in out and "did not pass every scored check" in out


def test_the_absent_file_grades_as_the_ladder_stopping_not_as_an_error(tmp_path):
    """The point of writing nothing, asserted through the scorer.

    This is the test that would have caught the skip-file version: it runs the
    real ladder over exactly what the gate leaves on disk and requires that
    stage 3's absence is charged to stage 2.
    """
    task_dir = _task_tree(tmp_path)
    # Stage 1 has to pass for the ladder to reach stage 2 at all: without it the
    # verdict blocks at `audit-missing` and says nothing about stage 3.  Which
    # is now true twice over -- of the scorer, as it always was, and of the gate in
    # `cmd_verification`, which asks the same rung first.
    _audit(tmp_path)
    _behavioural(tmp_path, 0.9)
    assert cli.cmd_verification(_namespace(task_dir)) == 0

    args = cli.build_parser().parse_args([
        "score", "--task-dir", str(task_dir),
        "--results", str(task_dir / "results")])
    assert cli.cmd_score(args) == 0
    verdict = json.loads(
        (tmp_path / "results" / "score.json").read_text(encoding="utf-8"))
    assert verdict["blocked_by"] == "behavioural-incomplete"
    assert not verdict["harness_error"], verdict["harness_error"]


def test_the_gate_removes_an_earlier_runs_result(tmp_path, capsys):
    """A stale file must not stand as this run's answer.

    Left in place it is worse than a wrong number: it is a *passing* stage 3 from
    a previous submission sitting in the results directory of one that did not
    earn the stage, and the scorer would pay for it.
    """
    task_dir = _task_tree(tmp_path)
    stale = tmp_path / "results" / "verification.json"
    StageResult(stage="verification", task="t").write(stale)
    _audit(tmp_path)
    _behavioural(tmp_path, 0.9)
    assert cli.cmd_verification(_namespace(task_dir)) == 0
    assert not stale.exists()
    assert "removed stale" in capsys.readouterr().out


def test_force_runs_the_rounds_below_the_gate(tmp_path, capsys):
    """The override reaches the stage rather than exiting early.

    Asserted by where it fails: with no driver available the command dies in
    setup, which is proof it got past the gate.  A run that returned 0 having
    written nothing would be the gate still firing.
    """
    task_dir = _task_tree(tmp_path)
    _audit(tmp_path)
    _behavioural(tmp_path, 0.9)
    rc = cli.cmd_verification(_namespace(task_dir, force=True, driver="nonesuch"))
    assert rc != 0
    assert "not run --" not in capsys.readouterr().out


def test_the_gate_is_asked_before_a_driver_is_built(tmp_path, capsys):
    """Order matters: the whole saving is in what does not get set up.

    A gate placed after the adjudicator was constructed would still write no
    result file and still return 0, so every assertion above passes -- while a
    graded run under an incomplete stage 2 goes on failing at ``adjudicator
    unavailable`` on a
    task whose credentials were never needed.  ``--driver nonesuch`` is a driver
    that cannot be built: reaching it is a non-zero exit, so 0 here is the proof
    that nothing tried.
    """
    task_dir = _task_tree(tmp_path)
    _audit(tmp_path)
    _behavioural(tmp_path, 0.9)
    rc = cli.cmd_verification(_namespace(task_dir, driver="nonesuch"))
    assert rc == 0, capsys.readouterr().out


# --------------------------------------------------------------------------- #
# the stage-1 rung, enforced at the same point
# --------------------------------------------------------------------------- #


def test_a_passing_stage_one_passes_the_gate(tmp_path):
    _task_tree(tmp_path)
    res = StageResult.read(_audit(tmp_path))
    passed, why = scoring.audit_passes_gate(res, scoring.ScoringPolicy(), "t")
    assert passed, why


def test_a_failed_required_gate_does_not_pass(tmp_path):
    _task_tree(tmp_path)
    res = StageResult.read(_audit(tmp_path, "fail"))
    passed, why = scoring.audit_passes_gate(res, scoring.ScoringPolicy(), "t")
    assert not passed
    assert "audit-gate" in why and "default_path" in why


def test_a_failed_non_required_gate_still_passes(tmp_path):
    """The gate is the conjunction of the *required* checks, and only those.

    Pinned here because this function is a second reader of ``grade_audit``
    and the cheapest wrong implementation of it -- "any check failed" -- passes
    every other case in this file.  An advisory observation that failed must not
    cost a submission the stage it earned.
    """
    _task_tree(tmp_path)
    res = StageResult(stage="audit", task="t")
    res.add(Check(id="default_path", verdict="pass", required=True, summary=""))
    res.add(Check(id="an_observation", verdict="fail", required=False, summary=""))
    passed, why = scoring.audit_passes_gate(res, scoring.ScoringPolicy(), "t")
    assert passed, why


def test_an_undecided_gate_does_not_pass_and_says_so(tmp_path):
    """Undecided is not failed, and the sentence has to keep them apart.

    ``audit-undecided`` asks for a re-run and ``audit-gate`` does not, so
    an operator reading one as the other either re-runs a settled cheat or accepts
    an outage as a verdict.
    """
    _task_tree(tmp_path)
    res = StageResult.read(_audit(tmp_path, "error"))
    passed, why = scoring.audit_passes_gate(res, scoring.ScoringPolicy(), "t")
    assert not passed
    assert "audit-undecided" in why and "could not be decided" in why


def test_a_gate_with_nothing_required_does_not_pass(tmp_path):
    """A gate that cannot fail is an authoring bug, not a pass.

    Same judgement ``grade_audit`` makes, and the reason this function asks it
    rather than testing ``verdict == "pass"`` itself: a suite whose required flags
    all vanished would otherwise buy six adversary rounds for every submission
    including an untouched State A.
    """
    _task_tree(tmp_path)
    res = StageResult.read(_audit(tmp_path, required=False))
    passed, why = scoring.audit_passes_gate(res, scoring.ScoringPolicy(), "t")
    assert not passed
    assert "audit-empty" in why


def test_an_absent_stage_one_does_not_pass(tmp_path):
    passed, why = scoring.audit_passes_gate(None, scoring.ScoringPolicy(), "t")
    assert not passed
    assert "audit-missing" in why and "no result file" in why


def test_stage_three_writes_no_file_when_stage_one_failed(tmp_path, capsys):
    """Full marks in stage 2 do not buy the rounds if the gate failed.

    The case the change exists for.  Stage 2 needs no credentials and is the stage
    an operator runs by hand, so a tree that failed its audit gate and then
    scored 40.000 is a shape that occurs -- and every assertion this file made
    before the second rung was asked passed for it while six model runs were bought
    and discarded.
    """
    task_dir = _task_tree(tmp_path)
    _audit(tmp_path, "fail")
    _behavioural(tmp_path, 1.0)
    rc = cli.cmd_verification(_namespace(task_dir))
    assert rc == 0
    out = capsys.readouterr().out
    assert not (tmp_path / "results" / "verification.json").exists(), out
    assert "not run" in out and "audit-gate" in out


def test_the_reason_names_stage_one_rather_than_stage_two(tmp_path, capsys):
    """Which rung declined, in the line an operator reads.

    Both rungs write no file and exit 0, so a gate that asked them in the wrong
    order -- or reported the second's reason for the first's refusal -- is
    invisible to every other assertion here.  A submission whose gate failed and
    whose stage 2 also fell short must be told about the gate: stage 2 could not
    have mattered, and an operator sent to it goes looking at the wrong tree.
    """
    task_dir = _task_tree(tmp_path)
    _audit(tmp_path, "fail")
    _behavioural(tmp_path, 0.9)
    assert cli.cmd_verification(_namespace(task_dir)) == 0
    out = capsys.readouterr().out
    assert "audit-gate" in out
    assert "did not pass every scored check" not in out
    assert "behavioural-incomplete" not in out


def test_the_absent_file_is_charged_to_stage_one_by_the_scorer(tmp_path):
    """The scorer's account of the same directory names the gate, not stage 3.

    The analogue of the stage-2 case above, and the one that would catch a skip
    file: ``blocked_by`` has to stay ``audit-gate`` with no harness error, so
    stage 3's absence reads as the ladder stopping where it should rather than as
    an verification stage that broke.
    """
    task_dir = _task_tree(tmp_path)
    _audit(tmp_path, "fail")
    _behavioural(tmp_path, 1.0)
    assert cli.cmd_verification(_namespace(task_dir)) == 0

    args = cli.build_parser().parse_args([
        "score", "--task-dir", str(task_dir),
        "--results", str(task_dir / "results")])
    assert cli.cmd_score(args) == 0
    verdict = json.loads(
        (tmp_path / "results" / "score.json").read_text(encoding="utf-8"))
    assert verdict["blocked_by"] == "audit-gate"
    assert not verdict["harness_error"], verdict["harness_error"]
    assert verdict["score"] == 0.0


def test_force_runs_the_rounds_below_the_stage_one_gate(tmp_path, capsys):
    """The override covers both rungs, or it covers neither usefully.

    An adversary is developed against a tree that is not meant to pass, and such a
    tree fails stage 1 as readily as stage 2 -- often it has no ``audit.json``
    at all, because running stage 1 costs a model call.
    """
    task_dir = _task_tree(tmp_path)
    _behavioural(tmp_path, 1.0)
    rc = cli.cmd_verification(_namespace(task_dir, force=True, driver="nonesuch"))
    assert rc != 0
    assert "not run --" not in capsys.readouterr().out


def test_the_stage_one_gate_does_not_mutate_what_it_was_handed(tmp_path):
    """Same property as stage 2's: asking is not grading.

    ``grade_audit`` clears and fills ``audit_failures`` on the verdict it
    is given, and this pins that none of it reaches the result file stage 1 wrote
    -- which ``cmd_score`` reads afterwards as the evidence about the submission.
    """
    _task_tree(tmp_path)
    path = _audit(tmp_path, "fail")
    before = path.read_bytes()
    res = StageResult.read(path)
    scoring.audit_passes_gate(res, scoring.ScoringPolicy(), "t")
    res.write(path)
    assert path.read_bytes() == before


def test_a_disabled_stage_still_writes_its_skip_file(tmp_path):
    """The pre-existing skip path is not the one this change touches.

    ``[stages.verification] enabled = false`` is a statement about the *task* and
    keeps writing a file, which is how the scorer tells "this task has no stage 3"
    from "this submission did not reach it".  Pinned on the order: ``cmd_verification``
    asks about the disabled stage before it asks about the rungs below, and
    swapping the two would silently change what a disabled stage produces.
    """
    task_dir = _task_tree(tmp_path)
    toml = (task_dir / "tests" / "evaluation.toml")
    toml.write_text(
        toml.read_text(encoding="utf-8").replace(
            '[stages.verification]\n', '[stages.verification]\nenabled = false\n'),
        encoding="utf-8")
    _audit(tmp_path)
    _behavioural(tmp_path, 0.9)
    assert cli.cmd_verification(_namespace(task_dir)) == 0
    got = json.loads(
        (tmp_path / "results" / "verification.json").read_text(encoding="utf-8"))
    assert got["status"] == "skip"

