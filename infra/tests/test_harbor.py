"""The Harbor-facing publication step.

What matters here is not the arithmetic -- test_scoring covers that -- but the
handover: reward.json holds numbers only, every stage's outcome survives the
flattening, and a grader that crashed still writes a file that says so instead of
a zero that reads like a judgement.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from swerefactor import config, harbor, result, scoring  # noqa: E402


def _policy() -> config.ScoringPolicy:
    return config.ScoringPolicy.from_dict({
        "max_score": 100.0,
        "behavioural_points": 40.0,
        "verification_points": 60.0,
        "verification_models": 6,
        "points_per_survived_model": 10.0,
    }, "test")


def _audit(passed: bool = True) -> result.StageResult:
    res = result.StageResult(stage="audit", task="t", status="ok")
    res.add(result.Check(id="default_path", verdict="pass" if passed else "fail",
                         summary="the new implementation serves traffic",
                         required=True, unit="default_path"))
    return res


def _behavioural(rate: float = 1.0, total: int = 10) -> result.StageResult:
    """One module at `rate`, expanded into `total` checks.

    `total` is a parameter because payment no longer separates one rate from
    another: every rate below 1.0 is paid the same zero, so a test about a near miss
    and a test about a collapse ask for identical points and are told apart only by
    the rate that got measured.  Ten checks cannot express 0.995, and 0.995 is what
    the incomplete-stage cases below are about.
    """
    res = result.StageResult(stage="behavioural", task="t", status="ok")
    good = int(round(rate * total))
    for i in range(total):
        res.add(result.Check(id=f"corpus/{i}", unit="corpus",
                             verdict="pass" if i < good else "fail",
                             summary=f"case {i}"))
    res.units.append(result.Unit(id="corpus", title="the corpus", weight=1.0,
                                 status="ok"))
    return res


def _verification(survived: int, broken: int = 0,
                 errored: int = 0) -> result.StageResult:
    res = result.StageResult(stage="verification", task="t", status="ok")
    rounds = []
    for i in range(survived):
        rid = f"a{i:02d}-survivor"
        res.add(result.Check(id=rid, unit=rid, verdict="pass",
                             summary="found nothing", metadata={"kind": "round"}))
        rounds.append({"adversary": rid, "outcome": "survived"})
    for i in range(broken):
        rid = f"b{i:02d}-breaker"
        res.add(result.Check(id=rid, unit=rid, verdict="fail",
                             summary="/range suffix ranges are wrong",
                             metadata={"kind": "round"}))
        rounds.append({"adversary": rid, "outcome": "broken"})
    for i in range(errored):
        rid = f"e{i:02d}-errored"
        res.add(result.Check(id=rid, unit=rid, verdict="error",
                             summary="the API was unreachable",
                             metadata={"kind": "round"}))
        rounds.append({"adversary": rid, "outcome": "error"})
    res.metadata["rounds"] = rounds
    return res


# --------------------------------------------------------------------------- #
# flatten
# --------------------------------------------------------------------------- #


def test_reward_file_holds_numbers_only(tmp_path: Path) -> None:
    """Harbor parses reward.json as dict[str, float | int].

    A string in there is not a cosmetic problem: it is a parse error on the file
    that carries the result, which turns a graded run into a lost one.
    """
    verdict = scoring.grade("t", _policy(), _audit(), _behavioural(1.0),
                            _verification(survived=6))
    harbor.publish(verdict, tmp_path, title="fw01")
    raw = json.loads((tmp_path / "reward.json").read_text())
    assert raw, "reward.json is empty"
    for key, value in raw.items():
        assert isinstance(key, str)
        assert isinstance(value, (int, float)) and not isinstance(value, bool), (
            f"reward.json[{key!r}] is {type(value).__name__}, not a number")


def test_the_harness_count_is_the_only_provenance_in_reward_json(
        tmp_path: Path) -> None:
    """A fingerprint is a string, and this file is parsed as numbers.

    So the count travels and the identities stay in score.json.  Anything but 1
    means the row cannot be attributed to one harness, which is what a consumer
    averaging across a fleet has to be able to see.
    """
    verdict = scoring.grade("t", _policy(), _audit(), _behavioural(1.0),
                            _verification(survived=6))
    flat = harbor.flatten(verdict)
    assert flat["harnesses_distinct"] == 1, verdict.harnesses

    verdict.harnesses = [
        {"fingerprint": "aaaaaaaaaaaa", "stage": "audit"},
        {"fingerprint": "bbbbbbbbbbbb", "stage": "behavioural"},
        {"fingerprint": "bbbbbbbbbbbb", "stage": "verification"},
    ]
    assert harbor.flatten(verdict)["harnesses_distinct"] == 2, "two builds read as one"

    verdict.harnesses = []
    assert harbor.flatten(verdict)["harnesses_distinct"] == 0, (
        "a verdict from before provenance existed claimed a harness")


def test_perfect_run_is_full_marks_on_both_scales(tmp_path: Path) -> None:
    verdict = scoring.grade("t", _policy(), _audit(), _behavioural(1.0),
                            _verification(survived=6))
    harbor.publish(verdict, tmp_path)
    raw = json.loads((tmp_path / "reward.json").read_text())
    assert raw["score"] == pytest.approx(100.0)
    assert raw["reward"] == pytest.approx(1.0)
    assert raw["stage1_passed"] == 1
    assert raw["stage2_points"] == pytest.approx(40.0)
    assert raw["stage3_points"] == pytest.approx(60.0)
    assert raw["stage3_survived"] == 6
    assert raw["valid"] == 1


def test_failed_gate_zeroes_and_says_which_stage_stopped_it(tmp_path: Path) -> None:
    verdict = scoring.grade("t", _policy(), _audit(passed=False), None, None)
    harbor.publish(verdict, tmp_path)
    raw = json.loads((tmp_path / "reward.json").read_text())
    assert raw["score"] == 0.0
    assert raw["reward"] == 0.0
    assert raw["stage1_ran"] == 1
    assert raw["stage1_passed"] == 0
    assert raw["stage1_failures"] >= 1
    assert raw["blocked_by_audit_gate"] == 1
    assert raw["stage2_ran"] == 0
    assert raw["stage3_ran"] == 0
    # A zero from a failed gate is a judgement about the submission, so it is a
    # valid result -- unlike a zero from a crashed grader.
    assert raw["valid"] == 1


def test_an_incomplete_stage_two_stops_the_ladder_and_pays_nothing(
        tmp_path: Path) -> None:
    """995 of 1000 checks: as near as a miss comes, paid what a collapse is paid.

    So the paid figure is not what this file has to carry.  `stage2_rate` beside a
    paid 0.00 is the difference between a port with one broken case and a tree that
    never built, and a consumer reading only reward.json has nowhere else to find
    it -- which makes the rate, not the points, the assertion that matters here.
    1000 checks because ten cannot express 0.995; the payment needs no denominator
    at all.
    """
    verdict = scoring.grade("t", _policy(), _audit(),
                            _behavioural(0.995, total=1000), None)
    harbor.publish(verdict, tmp_path)
    raw = json.loads((tmp_path / "reward.json").read_text())
    assert raw["stage2_points"] == 0.0
    assert raw["score"] == 0.0
    assert raw["stage2_rate"] == pytest.approx(0.995)
    assert raw["blocked_by_behavioural_incomplete"] == 1
    assert raw["stage3_ran"] == 0
    assert raw["stage3_points"] == 0.0


def test_a_discarded_stage_reaches_reward_json(tmp_path: Path) -> None:
    """The stages get run one at a time by hand, so this case is not hypothetical.

    Without these keys reward.json says ``stage2_scored: 0, stage2_points: 0.0``
    about a stage whose 40.00 the scorer read off disk -- and while `stage2_ran`
    does distinguish that from a stage that never started, it says nothing about the
    figure, so the only consumer that reads this file still could not recover it.
    """
    verdict = scoring.grade("t", _policy(), _audit(passed=False),
                            _behavioural(1.0), None)
    harbor.publish(verdict, tmp_path)
    raw = json.loads((tmp_path / "reward.json").read_text())
    assert raw["score"] == 0.0
    # `stage2_scored`, not `stage2_ran`, for "it did not count".  This asserted
    # `stage2_ran == 0` about a stage it had just handed a result for, which is the
    # claim the whole test objects to, one key over: `stage2_ran` answers "did it
    # execute" and is 1 here, and `stage2_scored` answers "did the points come from
    # it".  The property the line is for is unchanged and now checked on the key
    # that carries it.
    assert raw["stage2_ran"] == 1                              # it did happen
    assert raw["stage2_scored"] == 0                           # and did not count
    assert raw["stage2_points"] == 0.0                         # and paid nothing
    assert raw["stage2_uncounted_ran"] == 1                    # said twice, by design
    assert raw["stage2_uncounted_points"] == pytest.approx(40.0)
    assert raw["stage2_uncounted_rate"] == pytest.approx(1.0)
    assert raw["stage2_uncounted_modules"] >= 1
    # The counted keys are the ones that add up, and they still do.
    assert raw["stage2_points"] + raw["stage3_points"] == pytest.approx(raw["score"])


def test_the_uncounted_flag_distinguishes_a_discarded_zero_from_no_discard(
    tmp_path: Path,
) -> None:
    """0.00 uncounted points is ambiguous on its own; the `_ran` flag settles it."""
    clean = scoring.grade("t", _policy(), _audit(), _behavioural(1.0),
                          _verification(survived=6))
    harbor.publish(clean, tmp_path)
    raw = json.loads((tmp_path / "reward.json").read_text())
    assert raw["stage2_uncounted_ran"] == 0
    assert raw["stage3_uncounted_ran"] == 0
    assert raw["stage2_uncounted_points"] == 0.0
    assert raw["stage3_uncounted_points"] == 0.0


def test_stage_three_discarded_by_an_incomplete_stage_two_reaches_reward_json(
    tmp_path: Path,
) -> None:
    # An incomplete stage 2 that still measured something -- see
    # `test_an_incomplete_stage_two_stops_the_ladder_and_pays_nothing` for the
    # denominator.  A stage 2 at 0.0 would not show that six survivals were
    # discarded rather than never run.
    verdict = scoring.grade("t", _policy(), _audit(),
                            _behavioural(0.995, total=1000),
                            _verification(survived=6))
    harbor.publish(verdict, tmp_path)
    raw = json.loads((tmp_path / "reward.json").read_text())
    assert raw["score"] == 0.0
    # Same swap as above, and note the contrast with
    # `test_an_incomplete_stage_two_stops_the_ladder_and_pays_nothing`, which
    # asserts `stage3_ran == 0` for the same stage 2: there stage 3 is passed as
    # None and genuinely did not run.  Here it handed in a result.  The two tests
    # only look contradictory until the argument is `None` in one and a stage result
    # in the other.
    assert raw["stage3_ran"] == 1
    assert raw["stage3_scored"] == 0
    assert raw["stage3_points"] == 0.0
    assert raw["stage3_uncounted_ran"] == 1
    assert raw["stage3_uncounted_points"] == pytest.approx(60.0)
    assert raw["stage3_uncounted_survived"] == 6


def test_every_number_in_the_verdict_reaches_reward_json(tmp_path: Path) -> None:
    """The module docstring's invariant, on the shape that stresses it.

    `uncounted` carries what a stage measured but the ladder did not count, so a
    flattening that dropped it would leave reward.json making a claim the report does
    not -- to the one audience that cannot read prose.
    """
    # 0.9 rather than a perfect stage: at rate 1.0 every behavioural value is either
    # 1.0 or 40.0, so an assertion could be satisfied by the wrong key and still
    # pass.  At 0.9 the stage is incomplete, so the values are 0.0 paid, 0.9
    # measured, not complete and 1 module -- all distinct, and each has to be carried
    # by its own key.
    verdict = scoring.grade("t", _policy(), _audit(passed=False),
                            _behavioural(0.9), _verification(survived=6))
    flat = harbor.flatten(verdict)
    for stage, keys in (("behavioural",
                         ("points", "rate", "modules")),
                        ("verification", ("points", "models_survived"))):
        for key in keys:
            value = verdict.uncounted[stage][key]
            assert any(v == pytest.approx(value) for v in flat.values()), (
                f"uncounted[{stage!r}][{key!r}] = {value} appears in no "
                f"reward.json key")


def test_one_break_costs_exactly_its_ten_points(tmp_path: Path) -> None:
    verdict = scoring.grade("t", _policy(), _audit(), _behavioural(1.0),
                            _verification(survived=5, broken=1))
    harbor.publish(verdict, tmp_path)
    raw = json.loads((tmp_path / "reward.json").read_text())
    assert raw["stage3_survived"] == 5
    assert raw["stage3_points"] == pytest.approx(50.0)
    assert raw["stage3_broken"] == 1
    assert raw["score"] == pytest.approx(90.0)


def test_per_adversary_flags_distinguish_error_from_found_nothing(
        tmp_path: Path) -> None:
    """An errored round is absent from the per-adversary flags.

    Recording it as 0 would read as "this model found a defect", and as 1 as "this
    model cleared the submission".  Neither happened.
    """
    verdict = scoring.grade("t", _policy(), _audit(), _behavioural(1.0),
                            _verification(survived=4, broken=1, errored=1))
    harbor.publish(verdict, tmp_path)
    raw = json.loads((tmp_path / "reward.json").read_text())
    flags = {k: v for k, v in raw.items() if k.startswith("adversary_")}
    assert len(flags) == 5, flags
    assert flags["adversary_a00_survivor_survived"] == 1
    assert flags["adversary_b00_breaker_survived"] == 0
    assert not any("errored" in k for k in flags)
    # Errored rounds pay nothing.
    assert raw["stage3_points"] == pytest.approx(40.0)


def test_module_rates_survive_the_flattening(tmp_path: Path) -> None:
    verdict = scoring.grade("t", _policy(), _audit(), _behavioural(0.9), None)
    harbor.publish(verdict, tmp_path)
    raw = json.loads((tmp_path / "reward.json").read_text())
    assert raw["module_corpus_rate"] == pytest.approx(0.9)
    assert raw["module_corpus_passed"] == 9
    assert raw["module_corpus_failed"] == 1
    assert raw["module_corpus_ran"] == 1


def test_module_ids_become_safe_keys(tmp_path: Path) -> None:
    verdict = scoring.Verdict(task="t", max_score=100.0)
    verdict.modules.append(scoring.ModuleScore(
        id="corpus/status-codes", title="", weight=1.0, rate=1.0,
        passed=1, failed=0, errored=0, skipped=0, status="ok"))
    flat = harbor.flatten(verdict)
    assert "module_corpus_status_codes_rate" in flat


# --------------------------------------------------------------------------- #
# publish
# --------------------------------------------------------------------------- #


def test_publish_writes_all_three_files(tmp_path: Path) -> None:
    verdict = scoring.grade("t", _policy(), _audit(), _behavioural(1.0),
                            _verification(survived=6))
    written = harbor.publish(verdict, tmp_path, title="fw01: Flask to ASGI")
    assert set(written) == {"reward", "verdict", "summary"}
    for path in written.values():
        assert path.exists() and path.stat().st_size > 0
    # score.json round-trips through the reader the report command uses.
    reread = scoring.Verdict.read(tmp_path / "score.json")
    assert reread.score == pytest.approx(verdict.score)
    assert "fw01" in (tmp_path / "summary.txt").read_text()


def test_harness_error_publishes_an_invalid_zero(tmp_path: Path) -> None:
    """A crashed grader must write a file, and it must not look like a verdict.

    A missing reward.json is eventually read as a zero by something downstream.
    A published zero with valid=0 is the only shape that cannot be mistaken for a
    judgement about the submission.
    """
    harbor.publish_error("t", tmp_path, "the verifier container ran out of memory")
    raw = json.loads((tmp_path / "reward.json").read_text())
    assert raw["reward"] == 0.0
    assert raw["valid"] == 0
    detail = json.loads((tmp_path / "score.json").read_text())
    assert "out of memory" in detail["harness_error"]
    assert "NOT A VALID RESULT" in (tmp_path / "summary.txt").read_text()


def test_reward_is_written_last(tmp_path: Path, monkeypatch) -> None:
    """A consumer polling for reward.json must not find it before the detail.

    Nothing enforces the order except the order of the calls, so it is asserted:
    a reward whose score.json has not landed is a result that cannot be explained.
    """
    order: list[str] = []
    real = harbor._atomic

    def spy(path: Path, text: str) -> Path:
        order.append(path.name)
        return real(path, text)

    monkeypatch.setattr(harbor, "_atomic", spy)
    harbor.publish(scoring.Verdict(task="t"), tmp_path)
    assert order[-1] == "reward.json", order


# --------------------------------------------------------------------------- #
# the entry point every task's score.py calls
# --------------------------------------------------------------------------- #


def _task_dir(tmp_path: Path, *, results: Path) -> Path:
    task = tmp_path / "task"
    (task / "tests").mkdir(parents=True)
    (task / "tests" / "evaluation.toml").write_text(f"""
schema = "swerefactor.evaluation/1"
task = "t"
title = "a task"
results_dir = "{results}"

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
runner = "behavioural"
result = "behavioural.json"

[stages.verification]
runner = "verification"
result = "verification.json"
""", encoding="utf-8")
    return task


def test_main_grades_whatever_stages_are_present(tmp_path: Path) -> None:
    results = tmp_path / "logs"
    results.mkdir()
    task = _task_dir(tmp_path, results=results)
    _audit().write(results / "audit.json")
    _behavioural(1.0).write(results / "behavioural.json")
    _verification(survived=6).write(results / "verification.json")

    assert harbor.main(["--task-dir", str(task)]) == 0
    raw = json.loads((results / "reward.json").read_text())
    assert raw["score"] == pytest.approx(100.0)


def test_main_treats_an_absent_stage_three_as_not_run(tmp_path: Path) -> None:
    results = tmp_path / "logs"
    results.mkdir()
    task = _task_dir(tmp_path, results=results)
    _audit().write(results / "audit.json")
    # An incomplete stage 2, so the ladder stops before stage 3 is looked for and
    # the absent file is the correct outcome rather than a missing one.  0.995 rather
    # than an outright zero so the run measured something: the case is about which
    # rung the absence belongs to, and a stage 2 that measured nothing would leave
    # that ambiguous.
    _behavioural(0.995, total=1000).write(results / "behavioural.json")

    assert harbor.main(["--task-dir", str(task)]) == 0
    raw = json.loads((results / "reward.json").read_text())
    assert raw["stage3_ran"] == 0
    assert raw["score"] == 0.0
    assert raw["blocked_by_behavioural_incomplete"] == 1
    # And still a judgement about the submission, which is the whole contrast with
    # the test below: there the same absent stage 3 makes the row unusable.
    assert raw["valid"] == 1


def test_main_will_not_publish_forty_as_a_total_when_stage_three_is_missing(
        tmp_path: Path) -> None:
    """The case the test above does not reach, and the one that happens in anger.

    There, stage 2 was incomplete and stopped the ladder, so no stage-3 file is
    exactly right.  Here stage 2 was paid whole, the ladder went looking for the
    stage the task declared, and there was nothing there -- an verification runner
    that never started, a container that lost its results mount, a driver that
    skipped the stage.  60 of the 100 points were never contested, which is most of
    them.

    `main` collapses "not declared" and "declared but absent" into one `None` a few
    lines before it grades; the whole fix is that it now says which it was.
    """
    results = tmp_path / "logs"
    results.mkdir()
    task = _task_dir(tmp_path, results=results)
    _audit().write(results / "audit.json")
    _behavioural(1.0).write(results / "behavioural.json")
    assert not (results / "verification.json").exists()

    assert harbor.main(["--task-dir", str(task)]) == 0
    raw = json.loads((results / "reward.json").read_text())
    assert raw["valid"] == 0, "a consumer averaging this in would score an outage"
    assert raw["stage3_ran"] == 0
    assert raw["stage3_points"] == 0.0
    # The 40 it did measure is still published, so the next reader can see how far
    # the run got; `valid` is the field that says it is not a total.
    assert raw["stage2_points"] == pytest.approx(40.0)

    detail = json.loads((results / "score.json").read_text())
    assert detail["blocked_by"] == "verification-missing"
    # And the readable file says so in words, not only as an id.
    summary = (results / "summary.txt").read_text(encoding="utf-8")
    assert "verification stage produced no result" in summary


def test_main_survives_an_unreadable_stage_file(tmp_path: Path) -> None:
    """Corrupt JSON from one stage is a harness fault, not a zero."""
    results = tmp_path / "logs"
    results.mkdir()
    task = _task_dir(tmp_path, results=results)
    (results / "audit.json").write_text("{ this is not json", encoding="utf-8")

    assert harbor.main(["--task-dir", str(task)]) == 0
    detail = json.loads((results / "score.json").read_text())
    assert detail["valid"] is False
    assert json.loads((results / "reward.json").read_text())["valid"] == 0


def test_main_without_an_evaluation_file_still_writes_a_reward(
        tmp_path: Path) -> None:
    results = tmp_path / "logs"
    assert harbor.main(["--task-dir", str(tmp_path / "nowhere"),
                        "--results", str(results)]) == 2
    raw = json.loads((results / "reward.json").read_text())
    assert raw["valid"] == 0
    assert raw["reward"] == 0.0


@pytest.mark.parametrize("old,new", [
    # Unparseable, which never reaches a coercion.
    ('task = "t"', 'task = "t"\n[[oops'),
    # A string where a number goes.
    ("behavioural_points = 40.0", 'behavioural_points = "abc"'),
    # A bool where a number goes.  `float(True)` is 1.0, so a coercion that does
    # not check the type accepts it silently; here it is caught, and the arithmetic
    # check that 6 x 1.0 is not 60.0 catches it a second way.
    ("points_per_survived_model = 10.0", "points_per_survived_model = true"),
    # A fraction, where `int()` would have truncated to the right answer and
    # hidden the typo.
    ("verification_models = 6", "verification_models = 6.5"),
])
def test_main_with_a_malformed_evaluation_file_still_writes_a_reward(
        tmp_path: Path, old: str, new: str) -> None:
    """The sibling of the test above, and the one that was missing.

    Absent already worked: it raises ``ConfigError``, which this handler catches.
    Malformed did not.  The coercions ran ``float()`` on file data, so a bad
    value raised ``ValueError``, which is not in ``(ConfigError, OSError)`` --
    it escaped the handler, printed a traceback, exited 1, and published no
    ``reward.json`` at all.  This is the entry point every task's ``score.py``
    calls, so the consequence was a graded run with no reward file, which reads
    as a pipeline that never started rather than as a task nobody can grade.
    """
    results = tmp_path / "logs"
    results.mkdir()
    task = _task_dir(tmp_path, results=results)
    path = task / "tests" / "evaluation.toml"
    # Replacing rather than inserting.  Every scoring key is already in the
    # template, so an inserted one is a duplicate-key *parse* error -- which
    # this fix also handles, but it is not the bad-value case the label claims,
    # and three cases all testing the parser would have looked like coverage.
    text = path.read_text(encoding="utf-8")
    assert text.count(old) == 1, f"{old!r} is not a unique anchor"
    path.write_text(text.replace(old, new), encoding="utf-8")

    assert harbor.main(["--task-dir", str(task), "--results", str(results)]) == 2
    raw = json.loads((results / "reward.json").read_text())
    assert raw["valid"] == 0
    assert raw["reward"] == 0.0
    detail = json.loads((results / "score.json").read_text())
    assert detail["harness_error"], "a zero with no reason reads as a judgement"
