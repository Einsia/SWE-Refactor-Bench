"""The verifier phase has to be long enough for the ladder declared inside it.

Two files, one relation, and no runner that applies either side.  Harbor reads
``[verifier].timeout_sec`` from ``task.toml`` and kills the phase at it; the three
``[stages.*].timeout_sec`` in ``tests/evaluation.toml`` say what the ladder spends
out of that phase, and nothing in this repository reads *them* at all.  So the two
numbers can only fail by drifting apart, and ``validate`` is the one command that
holds both files open at once.

pf03 shows what that costs without ever breaking the relation: 73800s of ladder
under a 75600s ceiling holds, and leaves 1800s of margin where the other nineteen
leave 5400 or more, for a ladder fw05 carries 79200 for.  Both numbers date from
the commit that created the task, so nothing drifted -- the arithmetic was simply
never done, and its comment said twenty-one hours and then decomposed them into
1.5 + 4 + 6 = 11.5h.  Hence the note: the inequality cannot fail on a legal
outlier, so the margin is printed where a reader can compare it.

Written against ``_ladder_budget`` rather than through ``cmd_validate``: the
relation is between two numbers, and a whole task tree in between would only add
ways for a case to pass without the check having run.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from swerefactor import cli, config


# The smallest pair of files that loads: a three-stage ladder summing to 5400 +
# 10800 + 21600 = 37800, under a ceiling with an hour to spare.
EVALUATION = """\
schema = "swerefactor.evaluation/1"
task = "t"
results_dir = "/logs/verifier"

[scoring]
max_score = 100.0
behavioural_points = 40.0
verification_points = 60.0
verification_models = 6
points_per_survived_model = 10.0

[stages.audit]
runner = "swerefactor.audit"
result = "audit.json"
timeout_sec = 5400.0

[[stages.audit.gate]]
id = "g"
title = "a gate"
question = "is it ported?"
required = true

[stages.behavioural]
runner = "swerefactor.behavioural"
result = "behavioural.json"
timeout_sec = 10800.0

[stages.verification]
runner = "swerefactor.verification"
result = "verification.json"
timeout_sec = 21600.0
"""

TASK = """\
[task]
name = "swerefactor/t"

[verifier]
timeout_sec = 41400.0
"""

LADDER = 37800.0          # 5400 + 10800 + 21600, as the fixture declares it


def _check(tmp_path: Path, *, task: str = TASK,
           evaluation: str = EVALUATION) -> tuple[list[str], list[str]]:
    """Run the check over one pair of files; return (problems, notes)."""
    (tmp_path / "tests").mkdir(exist_ok=True)
    (tmp_path / "task.toml").write_text(task, encoding="utf-8")
    ev_path = tmp_path / "tests" / "evaluation.toml"
    ev_path.write_text(evaluation, encoding="utf-8")
    notes: list[str] = []
    problems = cli._ladder_budget(tmp_path, config.Evaluation.load(ev_path), notes)
    return problems, notes


def _edit(text: str, old: str, new: str) -> str:
    """One anchored substitution, with the anchor proven unique."""
    assert text.count(old) == 1, f"{old!r} is not a unique anchor"
    return text.replace(old, new)


# --------------------------------------------------------------------------- #
# the control
# --------------------------------------------------------------------------- #

def test_a_ceiling_above_the_ladder_is_no_problem_and_says_the_margin(tmp_path):
    """A task whose numbers agree reports the arithmetic and the spare time.

    The note is the useful half of this check.  The inequality holds in twenty of
    twenty tasks now, so if passing were silent the command would say nothing at
    all about budgets, and pf03's 1800s beside nineteen 5400s -- true, legal, and
    the shape of a ceiling nobody re-derived -- would stay invisible.
    """
    problems, notes = _check(tmp_path)
    assert problems == [], f"consistent budgets reported: {problems}"

    budget = [n for n in notes if "ladder budget" in n]
    assert len(budget) == 1, f"expected one ladder note, got {notes}"
    assert "5400 + 10800 + 21600 = 37800s" in budget[0], budget[0]
    assert "41400s verifier phase" in budget[0], budget[0]
    assert "3600s spare" in budget[0], budget[0]


def test_the_note_reads_in_ladder_order_not_alphabetical(tmp_path):
    """The sum renders in the order the stages run.

    ``evaluation.stages`` is a dict keyed by stage name, and sorting those names
    gives verification, behavioural, audit -- so the obvious implementation
    prints "21600 + 10800 + 5400", which is the ladder backwards.  It sums to the
    same total, which is why this is worth a test of its own: the number is right
    and the sentence is wrong, and a reader comparing it against the
    "5400 + 10800 + 21600 = 37800s" written in their own task.toml comment has to
    work out which of the two is confused.
    """
    _, notes = _check(tmp_path)
    note = next(n for n in notes if "ladder budget" in n)
    order = [note.index(f"{v:g}") for v in (5400, 10800, 21600)]
    assert order == sorted(order), f"stages out of ladder order: {note}"


# --------------------------------------------------------------------------- #
# the failure the check exists for
# --------------------------------------------------------------------------- #

def test_a_ceiling_below_the_ladder_is_a_problem_naming_both_sides(tmp_path):
    """The message has to carry the ceiling, the sum and the shortfall.

    All three, because the fix is a judgement between them: 1800s short can be
    either a ceiling that was never re-derived or a stage budget that grew too
    far, and an author who is told only "too short" has to go and re-add the three
    numbers to find out which file to edit.
    """
    short = _edit(TASK, "timeout_sec = 41400.0", "timeout_sec = 36000.0")
    problems, notes = _check(tmp_path, task=short)

    assert len(problems) == 1, f"expected one problem, got {problems}"
    (problem,) = problems
    assert "36000s" in problem, problem
    assert "5400 + 10800 + 21600 = 37800s" in problem, problem
    assert "1800s more" in problem, problem
    assert not [n for n in notes if "ladder budget" in n], (
        f"a short ladder must not also be reported as fitting: {notes}")


def test_the_problem_says_what_the_kill_costs(tmp_path):
    """Not just that Harbor kills it, but that the killed stage is a re-run.

    A stage that never wrote a result file is scored as a harness error -- no
    points for it and no points against it, the whole phase invalid.  So the cost
    of 1800s of arithmetic is another twenty-one-hour run, and that is the part
    that makes this worth fixing before a run rather than after one.
    """
    short = _edit(TASK, "timeout_sec = 41400.0", "timeout_sec = 36000.0")
    (problem,) = _check(tmp_path, task=short)[0]
    assert "re-run" in problem, problem


def test_equal_is_not_short(tmp_path):
    """A ceiling exactly equal to the sum passes.

    The relation SCHEMA.md states is "at least the sum".  Whether zero margin is
    *wise* is another question -- the image builds happen inside this phase too --
    but it is not a contradiction between the files, and a validator that failed
    it would be inventing a margin policy the schema does not state.
    """
    exact = _edit(TASK, "timeout_sec = 41400.0", f"timeout_sec = {LADDER}")
    problems, notes = _check(tmp_path, task=exact)
    assert problems == [], f"a ceiling equal to the sum is not short: {problems}"
    assert "0s spare" in next(n for n in notes if "ladder budget" in n)


# --------------------------------------------------------------------------- #
# which stages are in the sum
# --------------------------------------------------------------------------- #

def test_a_disabled_stage_spends_nothing(tmp_path):
    """`enabled = false` takes the stage out of the sum and out of the sentence.

    A disabled stage is not run, so its budget is not spent, and a task that
    switched one off would otherwise be told to reserve time for it.
    """
    off = _edit(EVALUATION, '''[stages.verification]
runner = "swerefactor.verification"''', '''[stages.verification]
enabled = false
runner = "swerefactor.verification"''')
    problems, notes = _check(tmp_path, evaluation=off)
    note = next(n for n in notes if "ladder budget" in n)
    assert problems == []
    assert "5400 + 10800 = 16200s" in note, note
    assert "21600" not in note, f"a disabled stage is still in the sum: {note}"


def test_a_stage_with_no_timeout_is_in_the_sum_at_its_default(tmp_path):
    """An omitted `timeout_sec` counts as the loader's 3600, not as zero.

    The default is what such a stage actually gets, so it is what has to fit.
    Treating silence as zero would let a task declare no budgets at all and be
    congratulated on a ladder that costs nothing.
    """
    bare = _edit(EVALUATION, """result = "behavioural.json"
timeout_sec = 10800.0""", 'result = "behavioural.json"')
    problems, notes = _check(tmp_path, evaluation=bare)
    note = next(n for n in notes if "ladder budget" in n)
    assert problems == []
    assert "5400 + 3600 + 21600 = 30600s" in note, note


# --------------------------------------------------------------------------- #
# the ceiling is missing or is not a number
# --------------------------------------------------------------------------- #

def test_no_verifier_timeout_at_all_is_reported(tmp_path):
    """Silence on the ceiling is a problem, not a pass.

    Nothing in this repository reads ``[verifier]``, so an absent ceiling breaks
    no code here -- it means Harbor applies whatever its own default is, and the
    ladder's fit becomes unknowable from the files.  The message carries the sum,
    which is the number the author needs in order to choose a ceiling.
    """
    for task in ("[task]\nname = \"swerefactor/t\"\n",              # no [verifier]
                 "[verifier]\nuser = \"root\"\n"):                # no timeout_sec
        problems, notes = _check(tmp_path, task=task)
        assert len(problems) == 1, f"{task!r} -> {problems}"
        assert "declares no [verifier].timeout_sec" in problems[0], problems[0]
        assert "37800s" in problems[0], problems[0]
        assert not [n for n in notes if "ladder budget" in n], notes


def test_a_quoted_ceiling_is_not_a_number_of_seconds(tmp_path):
    """`timeout_sec = "41400"` is reported rather than compared.

    ``float("41400")`` would succeed and hide the quotes, but the field is
    Harbor's and this reader cannot promise Harbor parses a string the same way.
    Saying so names a one-character fix; comparing it silently blesses a file
    whose meaning depends on another parser's leniency.
    """
    quoted = _edit(TASK, "timeout_sec = 41400.0", 'timeout_sec = "41400"')
    problems, notes = _check(tmp_path, task=quoted)
    assert len(problems) == 1, problems
    assert "not a number of seconds" in problems[0], problems[0]
    assert "'41400'" in problems[0], problems[0]
    assert not [n for n in notes if "ladder budget" in n], notes


def test_a_bool_ceiling_is_not_a_one_second_phase(tmp_path):
    """`timeout_sec = true` is named, not measured.

    ``float(True)`` is 1.0, so converting would have produced a real finding with
    an absurd cause: a phase one second long, 37799s short of its ladder.  The
    author would read a sentence about arithmetic when what they typed was a
    wrong type -- the same reason ``config._number`` rejects bools instead of
    counting ``true`` as one model.
    """
    flag = _edit(TASK, "timeout_sec = 41400.0", "timeout_sec = true")
    problems, _ = _check(tmp_path, task=flag)
    assert len(problems) == 1, problems
    assert "not a number of seconds" in problems[0], problems[0]
    assert "True" in problems[0], problems[0]
    assert "37799" not in problems[0], f"reported as arithmetic: {problems[0]}"


def test_an_unparseable_task_toml_is_reported_once(tmp_path):
    """A broken task.toml gets one sentence, not a traceback.

    ``validate`` runs every check before reporting, so this one has to fail
    quietly enough for the rest of the file's problems to still be printed.
    """
    problems, _ = _check(tmp_path, task="[verifier]\ntimeout_sec = \n")
    assert len(problems) == 1, problems
    assert "could not be parsed" in problems[0], problems[0]


def test_a_missing_task_toml_is_left_to_the_caller(tmp_path):
    """No task.toml means no finding from here.

    ``cmd_validate`` already reports the file as missing by name, and a second
    sentence about budgets in a directory with no task file would be noise.
    """
    (tmp_path / "tests").mkdir()
    ev = tmp_path / "tests" / "evaluation.toml"
    ev.write_text(EVALUATION, encoding="utf-8")
    notes: list[str] = []
    assert cli._ladder_budget(tmp_path, config.Evaluation.load(ev), notes) == []
    assert notes == []


# --------------------------------------------------------------------------- #
# the twenty tasks
# --------------------------------------------------------------------------- #

TASKS = sorted((Path(__file__).resolve().parents[2] / "tasks").glob("*/task.toml"))


@pytest.mark.parametrize("task_toml", TASKS, ids=lambda p: p.parent.name)
def test_every_shipped_task_declares_a_ladder_that_fits(task_toml):
    """The relation holds across the tasks as shipped.

    The check running over its own corpus rather than over a fixture.  Worth being
    exact about what it did and did not catch: no task ever violated the
    inequality, pf03 included -- 73800s of ladder under a 75600s ceiling holds.
    What pf03 had was 1800s of margin where the other nineteen have 5400 or more,
    and a comment decomposing its 21h into 1.5 + 4 + 6 = 11.5h.  Neither is
    something an inequality can fail on, which is why the note carries the margin:
    the number nobody re-derived is legal, and only visible beside its neighbours.
    """
    notes: list[str] = []
    evaluation = config.Evaluation.load(
        task_toml.parent / "tests" / "evaluation.toml")
    problems = cli._ladder_budget(task_toml.parent, evaluation, notes)
    assert problems == [], f"{task_toml.parent.name}: {problems}"
    assert [n for n in notes if "ladder budget" in n], (
        f"{task_toml.parent.name}: nothing reported the ladder budget")


def test_the_corpus_is_the_twenty_tasks():
    """Guard the parametrisation above against silently collecting nothing.

    A glob that stops matching turns twenty assertions into zero passes and no
    failures, which reads in the summary exactly like the check having run.
    """
    assert len(TASKS) == 20, f"expected 20 tasks, found {len(TASKS)}"
