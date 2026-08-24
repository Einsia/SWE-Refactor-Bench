"""What a model-driven stage records about its own run.

Two fields a reader consults before any verdict: how long the stage took, and what
its tools actually returned.  Neither is a measurement of the submission, which is
why both went wrong quietly -- a stage can grade correctly while describing itself
incorrectly, and the description is what survives into the artifact.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from swerefactor import agentloop, audit, models
from swerefactor.config import Gate
from swerefactor.tools import toolbox_for

#: Comfortably over MAX_LOGGED_RESULT so the clip is unambiguous, and written as
#: numbered build-file lines because that is the shape of the record that first
#: showed the problem: a 902-line meson.build whose transcript stopped at line 83.
BIG_LINES = 900


@pytest.fixture
def trees(tmp_path: Path) -> tuple[Path, Path]:
    original = tmp_path / "original"
    workspace = tmp_path / "workspace"
    original.mkdir()
    workspace.mkdir()
    (original / "build.txt").write_text("original\n", encoding="utf-8")
    (workspace / "big.build").write_text(
        "".join(f"target_{i} = files('src/unit_{i}.c')\n" for i in range(BIG_LINES)),
        encoding="utf-8")
    return original, workspace


def _engine(trees, turns: list[dict], *, log_dir: Path | None = None):
    original, workspace = trees
    engine = audit.Auditor(
        task="t",
        gates=[Gate(id="g", title="g", question="q?", required=True)],
        prompt="Review {{task}}.\n{{gates}}",
        toolbox=toolbox_for(original, workspace),
        samples=1, log_dir=log_dir)

    def factory(_index: int) -> models.Driver:
        return models.build("scripted", "scripted-model", options={"turns": turns})
    return engine, factory


def _submit(verdict: str = "pass") -> dict:
    return {"tool_calls": [{
        "name": "submit_review",
        "arguments": {"summary": "s", "gates": [
            {"id": "g", "verdict": verdict, "confidence": 0.0,
             "reasoning": "scripted"}]},
    }]}


# --------------------------------------------------------------------------- #
# duration
# --------------------------------------------------------------------------- #


def test_the_stage_reports_a_duration_at_all(trees):
    """``StageResult.duration_sec`` has to be written, not left at its default.

    A stage reporting the 0.0 it starts at claims it took no time -- on the stage
    whose cost is three model reviews, which is the number a reader looks for first.
    """
    engine, factory = _engine(trees, [_submit()])
    res = engine.run(factory)
    assert res.duration_sec > 0, "the stage reported no duration"


def test_the_duration_tracks_a_real_interval(trees, monkeypatch):
    """Nonzero is not enough: it has to be the interval, not a small constant.

    A scripted run finishes in milliseconds, so a field that happened to be set to
    any small number would pass the test above.  Stalling each sample by a known
    amount is what distinguishes a measurement from a plausible-looking value.
    """
    engine, factory = _engine(trees, [_submit()])
    real = engine.sample

    def slow(driver, index):
        time.sleep(0.4)
        return real(driver, index)

    monkeypatch.setattr(engine, "sample", slow)
    clock = time.time()
    res = engine.run(factory)
    wall = time.time() - clock

    assert res.duration_sec >= 0.4, (
        f"reported {res.duration_sec:.3f}s for a run that slept 0.4s")
    assert res.duration_sec <= wall + 0.05, (
        f"reported {res.duration_sec:.3f}s, longer than the {wall:.3f}s it ran")


def test_the_duration_covers_aggregation_not_just_the_samples(trees, monkeypatch):
    """Wall clock, deliberately, rather than the sum of per-sample elapsed times.

    The samples do not account for grounding every citation or for the vote, and a
    duration that disagrees with the container's runtime is worse than one that is
    merely coarse.  So the roll-up must be at least the sum of its parts.

    The delay goes inside a *tool*, not around ``sample``: ``agentloop.run`` starts
    its clock before dispatching and stops it after, so only time spent in there
    reaches the per-sample ``elapsed_sec`` this compares against.  Stalling outside
    it would leave the sum near zero, and an assertion against a sum near zero holds
    however wrong the total is.
    """
    engine, factory = _engine(trees, [
        {"tool_calls": [{"name": "fetch_source",
                         "arguments": {"path": "original:build.txt"}}]},
        _submit(),
    ])
    real = engine.toolbox.dispatch

    def slow(name, args):
        time.sleep(0.4)
        return real(name, args)

    monkeypatch.setattr(engine.toolbox, "dispatch", slow)
    res = engine.run(factory)

    samples = res.metadata.get("samples") or []
    assert samples, "the result recorded no samples"
    total = sum(float(s.get("elapsed_sec") or 0) for s in samples)
    assert total >= 0.4, (
        f"the samples only account for {total:.3f}s, so this comparison would "
        f"hold against any duration")
    assert res.duration_sec >= total - 0.01, (
        f"reported {res.duration_sec:.3f}s against {total:.3f}s of samples")


# --------------------------------------------------------------------------- #
# the transcript
# --------------------------------------------------------------------------- #


def _tool_records(log_dir: Path) -> list[dict]:
    out = []
    for path in sorted(log_dir.glob("review-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            if rec.get("kind") == "tool":
                out.append(rec)
    return out


def test_a_clipped_tool_result_says_it_was_clipped(trees, tmp_path):
    """The transcript is the only place a review can answer for itself.

    A ``fetch_source`` record whose own header reads ``lines 1-900 of 900`` and
    whose body stops partway through reads as a review that voted on a fraction of
    the build system.  The model received all of it; only the artifact was cut.
    ``tools.py`` announces its own cap for this reason, and the log has to as well.
    """
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    engine, factory = _engine(trees, [
        {"tool_calls": [{"name": "fetch_source",
                         "arguments": {"path": "workspace:big.build"}}]},
        _submit(),
    ], log_dir=log_dir)
    engine.run(factory)

    records = _tool_records(log_dir)
    assert len(records) == 1, f"expected one tool record, got {len(records)}"
    rec = records[0]

    assert len(rec["result"]) > agentloop.MAX_LOGGED_RESULT, (
        "the record is not long enough to have been clipped")
    assert "transcript kept" in rec["result"], (
        "the transcript clipped a result without saying so")
    assert rec["result_bytes"] > agentloop.MAX_LOGGED_RESULT


def test_the_recorded_size_is_what_the_model_received(trees, tmp_path):
    """``result_bytes`` has to be the full length, not the kept length.

    Otherwise the field records the cap, which every clipped row already agrees on,
    and a reader still cannot tell how much of the file the review saw.
    """
    original, workspace = trees
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    engine, factory = _engine(trees, [
        {"tool_calls": [{"name": "fetch_source",
                         "arguments": {"path": "workspace:big.build"}}]},
        _submit(),
    ], log_dir=log_dir)
    engine.run(factory)

    served = toolbox_for(original, workspace).fetch_source("workspace:big.build")
    rec = _tool_records(log_dir)[0]
    assert rec["result_bytes"] == len(served), (
        f"recorded {rec['result_bytes']} B, the tool returned {len(served)} B")
    assert str(len(served)) in rec["result"], (
        "the clip marker does not state the full size")


def test_a_short_result_is_recorded_whole_and_unmarked(trees, tmp_path):
    """The cap must not annotate rows it did not touch."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    engine, factory = _engine(trees, [
        {"tool_calls": [{"name": "fetch_source",
                         "arguments": {"path": "original:build.txt"}}]},
        _submit(),
    ], log_dir=log_dir)
    engine.run(factory)

    rec = _tool_records(log_dir)[0]
    assert "transcript kept" not in rec["result"]
    assert rec["result_bytes"] == len(rec["result"])
    assert "original" in rec["result"]


# --- which harness graded it ------------------------------------------------
#
# The third thing a stage records about itself, and the one that was missing: no
# score could be attributed to a build.  All sixty stage Dockerfiles take the
# harness from `ARG INFRA_IMAGE=swerefactor/infra:1`, a mutable tag, so two runs of
# one task can be graded by two different harnesses with nothing else in the
# record to separate them.  These tests hold the fingerprint to the one property
# that makes it worth recording -- that it moves when the source moves.


def test_the_fingerprint_moves_when_a_module_changes(tmp_path: Path):
    """Otherwise it is a name again, with more digits.

    Computed over a copy of the package rather than by editing the installed one:
    a test that mutates `swerefactor/` on disk to prove a digest changes would leave
    the tree it is grading modified.
    """
    import importlib.util
    import shutil

    pkg = Path(audit.__file__).resolve().parent

    def fingerprint_of(root: Path) -> str:
        spec = importlib.util.spec_from_file_location(
            f"_fp_{root.name}", root / "__init__.py")
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.fingerprint()

    a = tmp_path / "copy_a"
    shutil.copytree(pkg, a, ignore=shutil.ignore_patterns("__pycache__"))
    before = fingerprint_of(a)
    assert len(before) == 12 and before == fingerprint_of(a), "not deterministic"

    b = tmp_path / "copy_b"
    shutil.copytree(a, b)
    (b / "scoring.py").write_text(
        (b / "scoring.py").read_text(encoding="utf-8") + "\n# a change\n",
        encoding="utf-8")
    assert fingerprint_of(b) != before, (
        "a changed module left the fingerprint alone; it cannot attribute a score")

    # Moving a line between modules keeps every byte and must still register,
    # which is why each file's name is mixed in with its contents.
    c = tmp_path / "copy_c"
    shutil.copytree(a, c)
    (c / "renamed_scoring.py").write_bytes((c / "scoring.py").read_bytes())
    (c / "scoring.py").unlink()
    assert fingerprint_of(c) != before, "the same bytes under a new name read as identical"


def test_a_stage_result_records_the_harness_that_wrote_it(tmp_path: Path):
    """And a result read back names the harness that graded it, not this one."""
    import swerefactor
    from swerefactor.result import StageResult

    res = StageResult(stage="behavioural", task="lang01-cmark-c-to-rust")
    assert res.harness["fingerprint"] == swerefactor.fingerprint()

    path = res.write(tmp_path / "result.json")
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["harness"] == res.harness
    # Provenance is not somewhere a task can write: `metadata` is free-form and
    # this must not be reachable through it.
    assert "harness" not in raw["metadata"]

    stale = dict(raw, harness={"fingerprint": "000000000000"})
    assert StageResult.from_dict(stale).harness["fingerprint"] == "000000000000", (
        "re-reading re-stamped the result, so every old run claims the current build")
    assert json.loads(StageResult.from_dict(stale).write(
        tmp_path / "again.json").read_text(encoding="utf-8"))["harness"] == stale["harness"]

    legacy = dict(raw)
    legacy.pop("harness")
    assert StageResult.from_dict(legacy).harness == {}, "invented a harness for an old result"


def test_provenance_cannot_turn_a_graded_run_into_a_harness_error(monkeypatch):
    """A stage that finished grading must still be able to write its result."""
    from swerefactor import result as result_mod

    def explode() -> str:
        raise RuntimeError("no source to hash")

    monkeypatch.setattr("swerefactor.fingerprint", explode)
    assert result_mod._harness_stamp() == {}
    res = result_mod.StageResult(stage="audit", task="t")
    assert res.harness == {}
    assert res.to_dict()["harness"] == {}
