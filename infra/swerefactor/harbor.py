"""Publishing a verdict in the shape Harbor reads.

Harbor's contract is one file of numbers: ``reward.json``, parsed as
``dict[str, float | int]``.  A verdict is a nested object with prose in it, so
this module flattens one into the other, and writes the readable version beside
it because a flat table of numbers is not what a person wants when a submission
scored zero.

Three files, one call:

    reward.json     numerics only -- what Harbor reads
    score.json      the whole verdict -- what a re-score or a report reads
    summary.txt     the rendered report -- what a person opens

The flattening is deliberately lossy in one direction only: every number in the
verdict appears in ``reward.json``, and nothing in ``reward.json`` is derived
from anything that is not in the verdict.  So the two can be compared, and a
disagreement is a bug rather than a rounding convention.

That includes the ``stage*_uncounted_*`` keys, which carry what a stage measured
before a rung above it discarded the result.  They exist so that this file cannot
describe a stage as not-run when its result was read; they are never addends, and
the counted figure for such a stage is the ``0.0`` sitting in ``stage2_points`` or
``stage3_points`` beside them.

``reward`` is the normalised 0..1 headline; ``score`` is the same result on the
published 100-point scale.  Both are written so that neither consumer has to
rescale, because a consumer that rescales is a consumer that can rescale wrongly.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from . import report
from .scoring import Verdict

REWARD_FILE = "reward.json"
VERDICT_FILE = "score.json"
SUMMARY_FILE = "summary.txt"


def flatten(verdict: Verdict) -> dict[str, float | int]:
    """The verdict as numbers, for Harbor.

    Names are stable: a dashboard built on ``stage2_points`` should not break
    because a module was renamed.  Per-module and per-adversary entries carry
    their own id, so they come and go with the suite -- which is why the totals
    they roll up into are also present unconditionally.
    """
    v = verdict
    flat: dict[str, float | int] = {
        # The headline, twice, on both scales.
        "reward": round(v.score / v.max_score, 6) if v.max_score else 0.0,
        "score": round(v.score, 4),
        "max_score": float(v.max_score),
        # Whether the number is about the submission at all.  A harness error
        # means "re-run this", and a consumer that ignores it would average an
        # outage into a model's pass rate.
        "valid": int(v.valid),
        # Stage 1: pass/fail/not-run, as two flags rather than a tri-state
        # integer, because "did it run" and "did it pass" are separate questions.
        "stage1_ran": int(v.audit_gate != "not-run"),
        "stage1_passed": int(v.audit_gate == "pass"),
        "stage1_failures": len(v.audit_failures),
        # Stage 2.  `ran` and `scored` are separate for the same reason stage 1's
        # two flags are: a driver may run stage 2 after a stage-1 fail, and a
        # dashboard counting executions must not read that as never having run.
        # `stage2_scored` is the one the points came from.
        "stage2_ran": int("behavioural" in v.stages_run
                         or bool(v.unscored_stages.get("behavioural"))),
        "stage2_scored": int("behavioural" in v.stages_run),
        "stage2_points": round(v.behavioural_points, 4),
        "stage2_rate": round(v.behavioural_rate, 6),
        "stage2_modules": len(v.modules),
        # Stage 3.
        "stage3_ran": int("verification" in v.stages_run
                         or bool(v.unscored_stages.get("verification"))),
        "stage3_scored": int("verification" in v.stages_run),
        "stage3_points": round(v.verification_points, 4),
        "stage3_models": v.adversaries_total,
        "stage3_survived": v.adversaries_survived,
        "stage3_broken": len(v.verification_breaks),
    }

    # Which stage stopped the ladder, as a flag per stage rather than a string:
    # reward.json holds numbers only.
    for stage in ("audit-gate", "behavioural-incomplete"):
        key = "blocked_by_" + stage.replace("-", "_")
        flat[key] = int(v.blocked_by == stage)

    # A stage that ran, handed in a result, and was discarded by a rung above it.
    # These are NOT addends -- `stage2_points` above stays the counted figure, and
    # it is 0.0 in exactly these cases.  They are here because the alternative is
    # a reward.json that says `stage2_ran: 0, stage2_points: 0.0` about a stage
    # whose 40.00 the scorer was holding, and a consumer reading only this file
    # would have no way to tell that from a stage that never started.  The `_ran`
    # flag disambiguates a discarded zero from nothing discarded at all.
    fn_unc = v.uncounted.get("behavioural") or {}
    adv_unc = v.uncounted.get("verification") or {}
    flat["stage2_uncounted_ran"] = int(bool(fn_unc.get("ran")))
    flat["stage2_uncounted_points"] = round(float(fn_unc.get("points", 0.0)), 4)
    flat["stage2_uncounted_rate"] = round(float(fn_unc.get("rate", 0.0)), 6)
    flat["stage2_uncounted_modules"] = int(fn_unc.get("modules", 0))
    # `_points` above is all-or-nothing, so on its own it reintroduces the problem
    # this block exists to solve, one field over: a tree one check short and a tree
    # that never compiled both publish `stage2_uncounted_points: 0.0`.  `_rate`
    # above separates those two, and this flag is the one `_points` came from --
    # published rather than left to be inferred from `_rate == 1.0`, because a
    # rate rounded to six places is not the comparison the scorer made.
    flat["stage2_uncounted_complete"] = int(bool(fn_unc.get(
        "complete", float(fn_unc.get("points", 0.0) or 0.0) > 0.0)))
    flat["stage3_uncounted_ran"] = int(bool(adv_unc.get("ran")))
    flat["stage3_uncounted_points"] = round(float(adv_unc.get("points", 0.0)), 4)
    flat["stage3_uncounted_models"] = int(adv_unc.get("models_total", 0))
    flat["stage3_uncounted_survived"] = int(adv_unc.get("models_survived", 0))

    for module in v.modules:
        key = _key(module.id)
        flat[f"module_{key}_rate"] = round(module.rate, 6)
        flat[f"module_{key}_weight"] = round(module.weight, 6)
        flat[f"module_{key}_passed"] = module.passed
        flat[f"module_{key}_failed"] = module.failed
        flat[f"module_{key}_errored"] = module.errored
        flat[f"module_{key}_skipped"] = module.skipped
        flat[f"module_{key}_ran"] = int(module.status == "ok")

    # One flag per adversary: 1 survived, 0 broke through.  A round that errored
    # is neither, so it is absent -- which is how "did not complete" stays
    # distinguishable from "found nothing" in a file of numbers.
    for entry in ((v.metadata.get("verification") or {}).get("rounds") or []):
        if not isinstance(entry, dict):
            continue
        rid = str(entry.get("adversary") or entry.get("id") or "")
        outcome = str(entry.get("outcome") or "")
        if not rid or outcome not in ("survived", "broken"):
            continue
        flat[f"adversary_{_key(rid)}_survived"] = int(outcome == "survived")

    # Provenance, as the only part of it that is a number.  The fingerprints
    # themselves are strings and belong in score.json; what a dashboard can act on
    # is the count: anything but 1 means this row cannot be attributed to one
    # harness.  0 is a verdict recorded before stages stamped themselves, and >1 is
    # a submission whose stages were graded by different builds of the harness --
    # which is reachable because the sixty stage images take it from a mutable tag.
    # A consumer comparing two runs of one task, or averaging across a fleet, wants
    # to exclude those rows rather than discover the split afterwards.
    flat["harnesses_distinct"] = len({
        str(h.get("fingerprint"))
        for h in verdict.harnesses if h.get("fingerprint")})
    return flat


def _key(raw: str) -> str:
    """A verdict id as a reward.json key: lowercase, underscores, nothing else."""
    out = "".join(c if c.isalnum() else "_" for c in str(raw).lower())
    return out.strip("_") or "unnamed"


def publish(verdict: Verdict, results_dir: str | Path, *,
            title: str = "") -> dict[str, Path]:
    """Write all three files and return where each one went.

    Every write is atomic, and ``reward.json`` is written last: a consumer that
    polls for it never sees a reward whose supporting detail has not landed yet.
    """
    out = Path(results_dir)
    out.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}

    written["verdict"] = _atomic(out / VERDICT_FILE,
                                json.dumps(verdict.to_dict(), indent=2) + "\n")
    written["summary"] = _atomic(out / SUMMARY_FILE,
                                 report.render(verdict, title) + "\n")
    written["reward"] = _atomic(out / REWARD_FILE,
                                json.dumps(flatten(verdict), indent=2,
                                           sort_keys=True) + "\n")
    return written


def publish_error(task: str, results_dir: str | Path, message: str, *,
                  max_score: float = 100.0) -> dict[str, Path]:
    """Publish a *harness* failure: reward 0, and ``valid`` false to say why.

    A grader that crashed must still write a reward file -- a missing file is
    indistinguishable from a pipeline that never ran, and it will eventually be
    read as a zero.  What it must not do is write a zero that looks like a
    judgement about the submission, so ``valid`` is 0 and the message is in
    ``score.json`` and ``summary.txt``.
    """
    verdict = Verdict(task=task, max_score=max_score, harness_error=message)
    verdict.notes.append("the score below is not a judgement about the "
                         "submission; the grader did not complete")
    return publish(verdict, results_dir)


def _atomic(path: Path, text: str) -> Path:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-",
                              suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def main(argv: list[str] | None = None) -> int:
    """``python -m swerefactor.harbor --task-dir ...`` -- what a task's score.py calls.

    Kept here rather than in each task's ``score.py`` because the ladder is the
    benchmark's, not the task's: twenty copies of this would be twenty things to
    keep in step, and a task that scored differently from the published policy
    would be a task nobody could compare against the other nineteen.
    """
    import argparse

    from . import config, result, scoring

    parser = argparse.ArgumentParser(
        prog="swerefactor-score",
        description="apply the published ladder to whichever stages ran")
    parser.add_argument("--task-dir", default=".",
                        help="the task directory (holds tests/evaluation.toml)")
    parser.add_argument("--results", default="",
                        help="override results_dir from evaluation.toml")
    args = parser.parse_args(argv)

    task_dir = Path(args.task_dir).resolve()
    eval_path = task_dir / "tests" / "evaluation.toml"

    try:
        evaluation = config.Evaluation.load(eval_path)
    except (config.ConfigError, OSError) as exc:
        # No evaluation.toml means no results_dir either, so fall back to the
        # conventional one: a reward file has to appear somewhere.
        target = args.results or "/logs/verifier"
        publish_error(task_dir.name, target, f"cannot read {eval_path}: {exc}")
        print(f"[swerefactor] error: {exc}")
        return 2

    if args.results:
        evaluation.results_dir = args.results

    stages: dict[str, Any] = {}
    for name in ("audit", "behavioural", "verification"):
        if name not in evaluation.stages:
            stages[name] = None
            continue
        path = evaluation.result_path(name)
        if not path.exists():
            stages[name] = None
            continue
        try:
            stages[name] = result.StageResult.read(path)
        except (OSError, ValueError) as exc:
            stages[name] = result.StageResult.failed(
                name, evaluation.task, f"result at {path} is unreadable: {exc}")

    # See the loop above: it collapses "the task did not declare this stage" and
    # "it did, and the file is missing" into the same None.  `declared` carries the
    # difference through, so a declared stage 3 with no result is a re-run rather
    # than a published 40.
    verdict = scoring.grade(evaluation.task, evaluation.scoring,
                            stages["audit"], stages["behavioural"],
                            stages["verification"],
                            declared=evaluation.stages)
    written = publish(verdict, evaluation.results_dir,
                      title=evaluation.title or evaluation.task)
    # publish() just rendered the summary and told us where it put it; re-reading
    # it keeps stdout and the file identical by construction.
    print(written["summary"].read_text(encoding="utf-8"))
    for label, path in written.items():
        print(f"[swerefactor] wrote {label}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
