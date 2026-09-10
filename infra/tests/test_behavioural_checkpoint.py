"""What stage 2 leaves behind when it is killed part-way through its suite.

The stage clock is not the module clock.  A module's own budget is enforced by
the runner, inside the container, and a module is told when it will end
(``SRB_MODULE_DEADLINE``) so it can reserve a slice to write in -- which is why a
timed-out module can still publish the checks it found.  The *stage* clock is
enforced from outside: ``ladder.run_stage`` waits on ``docker start -a`` with a
timeout and then calls ``docker kill``, and PID 1 in that container goes without
a signal it can act on.  Nothing inside can reserve anything.

So the only thing that decides how much of a killed stage survives is how often
the result was written.  The stage reported ``left no result`` and the run was
refused a score it had largely earned.

These tests pin the three properties that make that survivable, and only those:
the file exists between modules, it describes the whole suite rather than the
part that finished, and the bookkeeping cannot itself end the stage.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from swerefactor.config import Suite
from swerefactor.behavioural import SuiteRunner
from swerefactor.result import StageResult

SUITE_TOML = """\
schema = "swerefactor.behavioural-suite/1"
task = "t"

[[module]]
id = "first"
title = "the one that finishes"
weight = 0.5
timeout_sec = 60

[[module]]
id = "second"
title = "the one the clock reaches"
weight = 0.3
timeout_sec = 60

[[module]]
id = "third"
title = "the one it does not"
weight = 0.2
timeout_sec = 60
"""

#: A module that passes one check, so a surviving row is distinguishable from a
#: seeded one by more than its status.
PASSES = """\
import json, os
json.dump({"checks": [{"id": "c", "verdict": "pass"}]},
          open(os.environ["SRB_RESULT"], "w"))
"""


class Killed(BaseException):
    """Stands in for ``docker kill``.

    A ``BaseException`` on purpose: ``_save`` catches ``Exception`` so a
    checkpoint cannot end a stage that was going to finish, and a test that
    wanted to stop the suite mid-way with an ordinary error would be caught by
    the very guard the last test here asserts.  What this models is not an error
    at all -- it is the process ceasing to exist between two modules.
    """


def _suite(tmp_path: Path, bodies: dict[str, str]) -> Suite:
    root = tmp_path / "behavioural"
    (root / "lib").mkdir(parents=True)
    (root / "suite.toml").write_text(SUITE_TOML, encoding="utf-8")
    for mid, body in bodies.items():
        (root / "modules" / mid).mkdir(parents=True)
        (root / "modules" / mid / "run.py").write_text(body, encoding="utf-8")
    return Suite.load(root / "suite.toml")


def _runner(tmp_path: Path, bodies: dict[str, str], checkpoint):
    repo = tmp_path / "repo"
    repo.mkdir()
    original = tmp_path / "original"
    original.mkdir()
    return SuiteRunner(_suite(tmp_path, bodies), repo, original,
                       tmp_path / "work", lambda _m: None,
                       checkpoint=checkpoint)


def test_a_stage_killed_after_one_module_leaves_that_modules_measurement(tmp_path):
    """The whole point: the kill lands and the finished module is still on disk.

    Written the way ``cli.py`` writes it -- ``r.write(out)`` to a real path, read
    back with ``StageResult.read`` -- because the thing being tested is what the
    next reader finds, not what the runner was holding in memory when it died.
    """
    out = tmp_path / "behavioural.json"
    bodies = {"first": PASSES, "second": PASSES, "third": PASSES}

    saves = []

    def checkpoint(result):
        result.write(out)
        saves.append(result.unit("first").status)
        # Two modules have finished; the third never starts.
        if len(saves) == 3:
            raise Killed

    with pytest.raises(Killed):
        _runner(tmp_path, bodies, checkpoint).run()

    landed = StageResult.read(out)
    assert landed.unit("first").status == "ok", (
        "the stage was killed after `first` finished and its measurement did not "
        "survive; this is lang04/max losing ten modules to the eleventh")
    assert [c.ok for c in landed.checks] == [True, True], (
        "the surviving modules' checks are not in the file")


def test_the_survivor_describes_the_whole_suite_and_not_just_the_part_that_ran(
        tmp_path):
    """Three rows, always.  A denominator cannot be assembled from the finishers.

    This is the half that keeps the checkpoint from being a way to score well by
    hanging: if the file listed only what finished, a submission that wedges
    module 2 would have modules 3..N disappear from the arithmetic instead of
    scoring zero in it, and its rate would be computed over the modules it chose.
    """
    out = tmp_path / "behavioural.json"
    bodies = {"first": PASSES, "second": PASSES, "third": PASSES}

    def checkpoint(result):
        result.write(out)
        if sum(1 for u in result.units if u.status == "ok") == 1:
            raise Killed

    with pytest.raises(Killed):
        _runner(tmp_path, bodies, checkpoint).run()

    landed = StageResult.read(out)
    assert [u.id for u in landed.units] == ["first", "second", "third"], (
        "the killed stage's table is not the declared suite, in declared order")
    assert [u.status for u in landed.units] == ["ok", "unreached", "unreached"]
    assert sum(u.weight for u in landed.units) == pytest.approx(1.0), (
        "an unreached module gave up its weight; the modules that did not run "
        "have to stay in the denominator or hanging one pays")
    unreached = landed.unit("third")
    assert "recorded nothing" in unreached.summary, unreached.summary


def test_the_seeded_file_exists_before_the_first_module_starts(tmp_path):
    """A stage killed during module 1 still has to say what it was going to do.

    Otherwise the shortest hang -- wedge the first module -- is the one case that
    produces no file at all.  An all-unreached result is refused too, but it is
    refused *as a re-run*, by ``scoring``, with the suite visible; that is a
    different answer from silence.
    """
    out = tmp_path / "behavioural.json"
    seen: list[list[str]] = []

    def checkpoint(result):
        result.write(out)
        seen.append([u.status for u in result.units])
        if len(seen) == 1:
            raise Killed

    with pytest.raises(Killed):
        _runner(tmp_path, {"first": PASSES, "second": PASSES,
                           "third": PASSES}, checkpoint).run()

    assert seen[0] == ["unreached"] * 3, (
        f"the pre-flight checkpoint did not seed the suite: {seen[0]}")
    landed = StageResult.read(out)
    assert len(landed.units) == 3
    assert landed.metadata.get("modules") == 3, (
        "the seeded file does not say how many modules the stage has; a reader "
        f"cannot tell it is complete: {landed.metadata}")


def test_each_checkpoints_counts_describe_that_checkpoint(tmp_path):
    """Refreshed on every save, so a file read mid-stage is not stale bookkeeping.

    ``modules_ok`` is what a reader uses to tell a stage that was killed with two
    modules banked from one that was killed with none.  Written once it would
    describe the first checkpoint forever, and every later one would understate
    what survived by exactly the amount that matters.
    """
    counts: list[tuple[int, int]] = []

    def checkpoint(result):
        counts.append((result.metadata.get("modules_ok"),
                       result.metadata.get("modules_unreached")))

    _runner(tmp_path, {"first": PASSES, "second": PASSES,
                       "third": PASSES}, checkpoint).run()

    assert counts == [(0, 3), (1, 2), (2, 1), (3, 0)], (
        f"the counts do not advance with the suite: {counts}")


def test_a_checkpoint_that_raises_does_not_cost_the_stage_its_result(tmp_path):
    """The bookkeeping is best-effort; the measurement is not.

    A full disk between modules must not turn a stage that was going to finish
    into one that died writing about itself.  The complete result is written by
    the caller afterwards regardless, so a failed checkpoint costs nothing except
    the partial file -- and it says so in the log rather than silently.
    """
    lines: list[str] = []
    repo = tmp_path / "repo"
    repo.mkdir()
    original = tmp_path / "original"
    original.mkdir()
    runner = SuiteRunner(
        _suite(tmp_path, {"first": PASSES, "second": PASSES, "third": PASSES}),
        repo, original, tmp_path / "work", lines.append,
        checkpoint=lambda _r: (_ for _ in ()).throw(OSError("No space left")),
    )
    result = runner.run()

    assert [u.status for u in result.units] == ["ok"] * 3, (
        "a checkpoint that raised cost the stage modules it had measured")
    assert any("checkpoint failed" in l and "No space left" in l for l in lines), (
        f"the failure was swallowed without a word: {lines}")


def test_a_module_that_makes_the_runner_raise_is_still_checkpointed(tmp_path):
    """The save is outside the `try`, so a runner bug is banked like a result.

    A module whose directory is missing, or that makes ``run_module`` raise, has
    still just changed the result -- and if the next module is the one that eats
    the clock, that change is all the evidence there is of why the stage went
    wrong.  Modelled here with a module the suite declares and the tree does not.
    """
    out = tmp_path / "behavioural.json"

    def checkpoint(result):
        result.write(out)

    # `second` is declared by SUITE_TOML and has no directory on disk.
    _runner(tmp_path, {"first": PASSES, "third": PASSES}, checkpoint).run()

    landed = StageResult.read(out)
    assert landed.unit("second").status == "error", (
        "the module the runner could not run was left as `unreached`, which "
        "reads as `the clock never got here` -- the opposite of what happened")
    assert "does not exist" in landed.unit("second").summary
    assert landed.unit("third").status == "ok", (
        "the suite stopped at the broken module instead of carrying on")


def test_the_partial_file_is_json_a_reader_can_load(tmp_path):
    """Not a shape test: the kill can land during the write.

    ``StageResult.write`` is what decides whether a checkpoint interrupted
    part-way leaves a truncated file where the next reader expects an object.
    Asserted at the loader rather than by eye, since a partial file that parses
    is the entire value of writing partial files.
    """
    out = tmp_path / "behavioural.json"

    def checkpoint(result):
        result.write(out)
        if any(u.status == "ok" for u in result.units):
            raise Killed

    with pytest.raises(Killed):
        _runner(tmp_path, {"first": PASSES, "second": PASSES,
                           "third": PASSES}, checkpoint).run()

    raw = json.loads(out.read_text(encoding="utf-8"))
    assert raw["stage"] == "behavioural"
    assert len(raw["units"]) == 3
