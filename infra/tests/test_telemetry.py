"""The per-run telemetry record, tested against the shape it reads.

``telemetry.json`` is where one run's grading cost lives in a single flat record:
stage 1's reviews and stage 3's rounds each carry their own ``usage`` inside
``score.json``'s ``metadata``.  These tests hold that input still and check that
the projection keeps the cost fields and drops the verdicts, and that a stage
which did not run contributes an empty list rather than a zero.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from swerefactor import telemetry


def _score_json() -> dict:
    """A verdict with the two ``metadata`` blocks telemetry reads, nothing more."""
    return {
        "schema": "swerefactor.score/1",
        "task": "build01-libsodium-autotools-to-cmake",
        "score": 70.0,
        "metadata": {
            "audit": {"samples": [
                {"index": 1, "outcome": "submitted",
                 "elapsed_sec": 72.0, "votes": {"g": {"verdict": "pass"}},
                 "usage": {"driver": "openai", "model": "gpt-5.6-sol",
                           "calls": 4, "input_tokens": 95081,
                           "output_tokens": 2243, "reasoning_effort": "medium"}},
                {"index": 2, "outcome": "submitted",
                 "elapsed_sec": 77.9,
                 "usage": {"driver": "anthropic", "model": "claude-sonnet-5",
                           "calls": 5, "input_tokens": 125235,
                           "output_tokens": 2688, "credits": 1.25}},
            ]},
            "verification": {"rounds": [
                {"adversary": "a01", "model": "gpt-5.6-sol", "outcome": "broken",
                 "reason": "detail a reader of cost does not need",
                 "elapsed_sec": 787.4,
                 "usage": {"driver": "openai", "model": "gpt-5.6-sol",
                           "calls": 23, "input_tokens": 825515,
                           "output_tokens": 18233, "reasoning_effort": "high"}},
            ]},
        },
    }


# --------------------------------------------------------------------------- #
# grading: the projection from score.json
# --------------------------------------------------------------------------- #

def test_grading_keeps_cost_and_drops_verdicts():
    got = telemetry.grading(_score_json())
    [s1, s2] = got["stage1"]
    assert s1 == {"index": 1, "driver": "openai", "model": "gpt-5.6-sol",
                  "elapsed_sec": 72.0, "calls": 4, "input_tokens": 95081,
                  "output_tokens": 2243, "credits": None}
    # credits carried through where the endpoint reported them; None, not 0,
    # where it did not -- so an absent figure is not read as a free round.
    assert s2["credits"] == 1.25
    assert "votes" not in s1                    # a verdict, not cost

    [r] = got["stage3"]
    assert r == {"adversary": "a01", "driver": "openai", "model": "gpt-5.6-sol",
                 "outcome": "broken", "elapsed_sec": 787.4, "calls": 23,
                 "input_tokens": 825515, "output_tokens": 18233, "credits": None}
    assert "reason" not in r                    # a verdict, not cost


def test_grading_is_empty_when_the_stage_did_not_run():
    got = telemetry.grading({"metadata": {}})
    assert got == {"stage1": [], "stage3": []}


# --------------------------------------------------------------------------- #
# for_run: one record per graded run
# --------------------------------------------------------------------------- #

def _run_dir(tmp_path: Path) -> Path:
    run = tmp_path / "build01-libsodium-autotools-to-cmake" / "kimi-k3" / "run1"
    run.mkdir(parents=True)
    (run / "score.json").write_text(json.dumps(_score_json()), encoding="utf-8")
    return run


def test_for_run_names_the_run_and_records_the_graders(tmp_path):
    got = telemetry.for_run(_run_dir(tmp_path))
    assert got["schema"] == telemetry.SCHEMA
    assert got["task"] == "build01-libsodium-autotools-to-cmake"
    assert got["model"] == "kimi-k3"
    assert got["run"] == "run1"

    assert len(got["stage1"]) == 2
    assert len(got["stage3"]) == 1


def test_for_run_takes_the_task_from_the_verdict(tmp_path):
    # The directory names the model and the run; the task is read from the verdict,
    # which names its own, so a telemetry row cannot disagree with the score beside
    # it about what was graded.
    run = tmp_path / "whatever-the-directory-is-called" / "kimi-k3" / "run1"
    run.mkdir(parents=True)
    (run / "score.json").write_text(json.dumps(_score_json()), encoding="utf-8")
    got = telemetry.for_run(run)
    assert got["task"] == "build01-libsodium-autotools-to-cmake"


def test_for_run_raises_when_there_is_no_score(tmp_path):
    run = tmp_path / "no-score" / "m" / "run1"
    run.mkdir(parents=True)
    with pytest.raises(FileNotFoundError):
        telemetry.for_run(run)


def test_write_round_trips_through_for_run(tmp_path):
    run = _run_dir(tmp_path)
    out = telemetry.write(run)
    assert out == run / "telemetry.json"
    again = json.loads(out.read_text(encoding="utf-8"))
    assert again["stage1"][0]["input_tokens"] == 95081
    assert len(again["stage3"]) == 1
