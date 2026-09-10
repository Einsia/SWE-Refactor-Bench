"""The CLI seam: does each stage subcommand actually start its stage.

Every stage has unit tests over the class that does the work, and those pass a
logging callable, a work directory and a repo the way the class documents.  What
had no test was the layer that builds those arguments out of ``argv`` -- and that
is where a stage broke: ``cmd_behavioural`` passed the *log directory* into
``SuiteRunner``'s ``log`` parameter, so the first thing the runner did with it was
call it, and stage 2 died with ``'PosixPath' object is not callable`` on every
task in the benchmark.  Nothing caught it, because every other caller in the tree
passes a callable and the tests only ever exercised those callers.

So these tests drive the subcommands the way an operator does: build the parser's
namespace, run the command function, and require that the stage got far enough to
write a result file.  They deliberately do not check a score.  A score needs a
model or a toolchain; "the stage ran and produced a verdict-shaped file" needs
neither, and it is the property that was missing.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from swerefactor import cli, config

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

# The ladder requires stages 1 and 2 to be declared, so stage 1 is here even
# though these tests never run it: a stage 2 that only starts when stage 1 is
# absent would not be the configuration any task uses.
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
"""

SUITE_TOML = """\
schema = "swerefactor.behavioural-suite/1"
task = "t"

[[module]]
id = "m"
title = "the one module"
weight = 1.0
timeout_sec = 60
"""

#: A module that reports one passing check and nothing else.  It has no build
#: system and touches no submission, so it runs anywhere the tests do.
MODULE = """\
import json, os
json.dump({"checks": [{"id": "c", "verdict": "pass"}]},
          open(os.environ["SRB_RESULT"], "w"))
"""


def _task_tree(tmp_path: Path) -> Path:
    """A task directory with just enough of stage 2 to be startable."""
    tests = tmp_path / "tests"
    (tests / "behavioural" / "modules" / "m").mkdir(parents=True)
    (tests / "evaluation.toml").write_text(
        EVALUATION_TOML.replace("RESULTS", str(tmp_path / "results")),
        encoding="utf-8")
    (tests / "behavioural" / "suite.toml").write_text(SUITE_TOML, encoding="utf-8")
    (tests / "behavioural" / "modules" / "m" / "run.py").write_text(
        MODULE, encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    return tmp_path


def _namespace(task_dir: Path, **over) -> argparse.Namespace:
    args = argparse.Namespace(
        task_dir=str(task_dir),
        results=str(task_dir / "results"),
        log_dir=None,
        repo=str(task_dir / "repo"),
        original=str(task_dir / "does-not-exist"),
        work=str(task_dir / "work"),
    )
    for key, value in over.items():
        setattr(args, key, value)
    return args


def test_cmd_behavioural_starts_the_stage(tmp_path, capsys):
    """The regression: this raised TypeError before the log argument was fixed."""
    task_dir = _task_tree(tmp_path)
    rc = cli.cmd_behavioural(_namespace(task_dir))
    assert rc == 0, capsys.readouterr().out
    written = json.loads((task_dir / "results" / "behavioural.json").read_text())
    assert written["stage"] == "behavioural"
    assert written["status"] == "ok"
    assert [c["id"] for c in written["checks"]] == ["m/c"]


def test_cmd_behavioural_logs_through_the_cli_logger(tmp_path, capsys):
    """The runner's progress reaches stdout rather than a directory.

    Asserted because the broken version failed *by* logging: any future change
    that hands the runner something other than a callable breaks this line first.
    """
    task_dir = _task_tree(tmp_path)
    cli.cmd_behavioural(_namespace(task_dir))
    out = capsys.readouterr().out
    assert "behavioural suite: 1 module(s)" in out
    assert "[m] 1/1 passed" in out


def test_the_per_module_summary_names_every_number_it_prints(tmp_path, capsys):
    """Both pairs on the line, and no weight sum masquerading as either.

    Measured on lang04, when ``pooled`` still summed weight: the summary printed
    "0/10 checks" for a module of 4 and "0/90 checks" for a module of 36, each
    directly under that module's own truthful "0/4 passed" / "0/36 passed".  The
    mislabel was not even monotone -- one module's weight sum was smaller than its
    count and another's larger -- so a reader reconciling the two had nothing to go
    on.

    ``pooled`` counts checks now, so the two pairs share a numerator and differ
    only in the denominator: all recorded checks, then the scored ones.  Through
    this path they *coincide* -- a skip no longer separates them, since it is
    charged, and a weight-0 row cannot reach a `behavioural.json` because collection
    deletes it.  Both are still printed and still named, because the second is what
    a result written by something other than `behavioural.py` would be read with;
    the weight-0 case is asserted where it can be constructed, on `pooled` itself.

    So the module here is a pass, a fail and a skip.  The skip must appear in the
    denominator and *also* be reported as a check that did not run: one event, two
    statements, and a line that makes only the second is the easy thing to write.
    The two weight-5 rows prove no weight sum reaches the line -- 5+5=10 must
    appear nowhere in it.
    """
    task_dir = _task_tree(tmp_path)
    (task_dir / "tests" / "behavioural" / "modules" / "m" / "run.py").write_text(
        'import json, os\n'
        'json.dump({"checks": [{"id": "a", "verdict": "pass", "weight": 5},\n'
        '                      {"id": "b", "verdict": "fail", "weight": 5},\n'
        '                      {"id": "c", "verdict": "skip", "weight": 1}]},\n'
        '          open(os.environ["SRB_RESULT"], "w"))\n', encoding="utf-8")

    cli.cmd_behavioural(_namespace(task_dir))
    line = next(l for l in capsys.readouterr().out.splitlines()
                if "module weight" in l)

    assert "1/3 check(s) passed" in line, line
    assert "1/3 scored" in line, line
    assert "1 did not run (charged 0)" in line, line
    assert "rate 0.3333" in line, line
    # The word appears once, for the module's own weight.  A check's weight is not
    # a quantity -- it is one bit deciding whether the check is recorded at all --
    # so a per-check weight figure on this line would be inventing one.
    assert line.count("weight") == 1, line
    assert "10" not in line, f"a weight sum reached the line: {line}"


def test_cmd_behavioural_reports_a_missing_submission(tmp_path):
    """A missing repo is a written failure, not a traceback.

    The stage's contract is that it always leaves a result file behind, because a
    pipeline that reads one cannot tell "no file" from "the harness died".
    """
    task_dir = _task_tree(tmp_path)
    args = _namespace(task_dir, repo=str(task_dir / "no-such-repo"))
    rc = cli.cmd_behavioural(args)
    assert rc != 0
    written = json.loads((task_dir / "results" / "behavioural.json").read_text())
    assert written["status"] == "error"


# --------------------------------------------------------------------------- #
# model and driver must agree
# --------------------------------------------------------------------------- #

# The two drivers differ in their URL, their tool-schema key and their message
# shape, and `driver` defaults to anthropic.  So a task naming an OpenAI model and
# omitting the line produces requests no endpoint answers -- per sample, for the
# whole timeout, reported as a harness fault rather than as a config mistake.
#
# The other direction is why this check exists at all.  One task's stage 1 named a
# claude model with no driver line, which is correctly served and therefore looked
# fine; it was also the only stage in the benchmark on the anthropic dialect, and
# so the only one exposed to a gateway that rewrites a tool's schema by name.
# Nobody was going to notice that by reading twenty files.


def _dialect_problems(toml: str, tmp_path: Path) -> list[str]:
    (tmp_path / "tests").mkdir(exist_ok=True)
    path = tmp_path / "tests" / "evaluation.toml"
    path.write_text(toml, encoding="utf-8")
    evaluation = config.Evaluation.load(path)
    return cli._model_dialects(evaluation, [])


def _with_audit(model: str, driver: str | None) -> str:
    line = f'\ndriver = "{driver}"' if driver else ""
    return EVALUATION_TOML.replace(
        '[stages.audit]\nrunner = "audit"',
        f'[stages.audit]\nrunner = "audit"\n'
        f'model = "{model}"{line}')


def test_an_openai_model_left_on_the_default_driver_is_caught(tmp_path):
    problems = _dialect_problems(_with_audit("gpt-5.6-sol", None), tmp_path)
    assert len(problems) == 1
    assert "defaults to anthropic" in problems[0]
    assert 'driver = "openai"' in problems[0]


def test_a_claude_model_on_the_openai_driver_is_caught(tmp_path):
    problems = _dialect_problems(_with_audit("claude-opus-5", "openai"),
                                 tmp_path)
    assert len(problems) == 1
    assert "speaks the anthropic dialect" in problems[0]


def test_a_correct_pairing_is_accepted(tmp_path):
    assert _dialect_problems(_with_audit("gpt-5.6-sol", "openai"),
                             tmp_path) == []
    assert _dialect_problems(_with_audit("claude-opus-5", "anthropic"),
                             tmp_path) == []


def test_a_right_but_unstated_driver_is_still_reported(tmp_path):
    """Correct by default is not the same as legible.

    This is the exact shape of the stage that broke: served correctly, and the
    only one of twenty on that dialect, with nothing in the file saying so.
    """
    problems = _dialect_problems(_with_audit("claude-opus-5", None), tmp_path)
    assert len(problems) == 1
    assert "the line is missing" in problems[0]


def test_an_unknown_driver_is_named_with_the_alternatives(tmp_path):
    problems = _dialect_problems(_with_audit("claude-opus-5", "antropic"),
                                 tmp_path)
    assert len(problems) == 1
    assert "unknown driver 'antropic'" in problems[0]
    assert "anthropic" in problems[0]


def test_a_scripted_driver_is_exempt(tmp_path):
    """``--driver scripted`` replaces the endpoint, so no dialect is involved."""
    assert _dialect_problems(_with_audit("claude-opus-5", "scripted"),
                             tmp_path) == []


def test_a_model_whose_name_implies_nothing_is_taken_as_declared(tmp_path):
    """A gateway may serve anything under a name of its own choosing.

    Guessing at an unrecognised name would make this check fire on every task
    using a private deployment, so it declines to have an opinion and says so in
    the notes rather than in the problems.
    """
    notes: list[str] = []
    path = tmp_path / "tests" / "evaluation.toml"
    path.parent.mkdir(exist_ok=True)
    path.write_text(_with_audit("house-model-v3", "openai"), encoding="utf-8")
    problems = cli._model_dialects(config.Evaluation.load(path), notes)
    assert problems == []
    assert any("does not imply a dialect" in n for n in notes)


def test_the_adjudicator_is_checked_on_its_own(tmp_path):
    """It is a separate model call and defaults independently of the stage's line.

    All twenty tasks had omitted it, and it is the call that rules on whether a
    claimed defect is in scope -- so a mispair there costs the round its
    adjudication rather than merely failing loudly.
    """
    toml = EVALUATION_TOML + (
        '\n[stages.verification]\nrunner = "verification"\n'
        'model = "gpt-5.6-sol"\ndriver = "openai"\n'
        '\n[stages.verification.adjudicator]\nmodel = "claude-opus-5"\n'
        'driver = "openai"\n')
    problems = _dialect_problems(toml, tmp_path)
    assert len(problems) == 1
    assert "[stages.verification.adjudicator]" in problems[0]


def test_cmd_score_will_not_call_forty_a_total_when_stage_three_is_missing(
        tmp_path, capsys):
    """`srb score` is the other production entry point, and it had no test at all.

    ``_read_stage`` returns None both for a stage the task never declared and for a
    declared stage whose file is absent -- its own docstring says "Absent is not
    the same as failed" -- and ``cmd_score`` then handed both to the scorer as the
    same None.  So a run whose verification stage never started scored 40.00, with
    nothing in ``score.json`` to say that 60 points had gone uncontested -- three
    fifths of the ladder, and the larger share of it.

    Driven through the parser rather than a hand-built namespace, because the bug
    this file exists for was in argument plumbing.
    """
    task_dir = _task_tree(tmp_path)
    results = tmp_path / "results"
    results.mkdir(exist_ok=True)
    (tests_dir := task_dir / "tests") / "evaluation.toml"
    # Stage 3 declared, and deliberately never written.
    (tests_dir / "evaluation.toml").write_text(
        EVALUATION_TOML.replace("RESULTS", str(results))
        + '\n[stages.verification]\nrunner = "verification"\n'
          'result = "verification.json"\n',
        encoding="utf-8")
    json.dump({"schema": "swerefactor.stage/1", "stage": "audit", "task": "t",
               "status": "ok",
               "checks": [{"id": "default_path", "unit": "default_path",
                           "verdict": "pass", "required": True,
                           "summary": "serves traffic"}]},
              open(results / "audit.json", "w"))
    json.dump({"schema": "swerefactor.stage/1", "stage": "behavioural", "task": "t",
               "status": "ok",
               "units": [{"id": "m", "title": "the one module", "weight": 1.0,
                          "status": "ok"}],
               "checks": [{"id": "m/c", "unit": "m", "verdict": "pass",
                           "summary": "a case"}]},
              open(results / "behavioural.json", "w"))
    assert not (results / "verification.json").exists()

    args = cli.build_parser().parse_args(
        ["score", "--task-dir", str(task_dir), "--results", str(results)])
    assert cli.cmd_score(args) == 0

    verdict = json.loads((results / "score.json").read_text(encoding="utf-8"))
    assert verdict["valid"] is False
    assert verdict["blocked_by"] == "verification-missing"
    # The 40 stage 2 was paid stays readable on the verdict; what `valid: false`
    # withholds is the claim that it is the whole score.
    assert verdict["behavioural"]["points"] == pytest.approx(40.0)
    assert verdict["verification"]["points"] == 0.0
    assert "verification stage" in capsys.readouterr().out


@pytest.mark.parametrize("stage", ["audit", "behavioural", "verification"])
def test_every_stage_command_takes_the_same_five_paths(stage):
    """The namespace these commands read is uniform, and stays uniform.

    ``cmd_*`` reads its inputs off a namespace built by the parser, so a flag
    renamed in one subcommand and not the others is an AttributeError at run time
    on whichever stage was not updated.  This pins the shared shape.
    """
    parser = cli.build_parser()
    args = parser.parse_args([stage, "--task-dir", "/t"])
    for name in ("task_dir", "results", "log_dir", "repo", "original", "work"):
        assert hasattr(args, name), f"{stage} lost --{name.replace('_', '-')}"


# --------------------------------------------------------------------------- #
# a broken config exits 2 and still writes a result
# --------------------------------------------------------------------------- #

# `docs/SCHEMA.md` states both halves: 2 means the task's own configuration is
# broken, and the result file is written on every path including the failing
# ones.  A malformed `evaluation.toml` broke both at once, and for the same
# reason -- the coercions ran `float()` straight on file data, so a bad value
# raised ValueError instead of ConfigError, escaped every handler, printed a
# traceback and exited 1.  The result path is read out of that same file, so
# there was also nowhere to write to and nothing was written.
#
# The two halves fail differently and are worth separating.  Exiting 1 tells a
# harness to retry an authoring bug forever.  Writing no file is worse: absent
# is how the ladder says "stage 3 was correctly never reached", so a
# misconfigured task reads as a working one that stopped early.


def _break(task_dir: Path, which: str, old: str, new: str) -> None:
    """Replace one line of a config, or delete the file when ``old`` is empty.

    Replacement rather than insertion, because every key these cases care about
    is already in the fixture: an inserted ``behavioural_points`` is a
    duplicate-key *parse* error, not the bad-value error the case is named for.
    Both go through the fix, so an inserting version of this passed -- while
    testing the TOML parser three times and the coercions never.
    """
    path = task_dir / "tests" / which
    if not old:
        path.unlink()
        return
    text = path.read_text(encoding="utf-8")
    assert text.count(old) == 1, f"{which}: {old!r} is not a unique anchor"
    path.write_text(text.replace(old, new), encoding="utf-8")


@pytest.mark.parametrize("which,old,new", [
    # A string where a number goes, in each of the two files a stage reads.
    ("evaluation.toml", "behavioural_points = 40.0", 'behavioural_points = "abc"'),
    # This one already worked: a module's weight was the single coercion that had
    # been wrapped, so it raised ConfigError and its stage wrote a result.  It is
    # here as the guard on the behaviour the others were brought up to, not as
    # new coverage -- it passes with or without the fix.
    ("behavioural/suite.toml", "weight = 1.0", 'weight = "abc"'),
    # A bool where a number goes.  `float(True)` is 1.0, so a coercion that does not
    # check the type takes it and the ladder runs on a policy nobody declared.
    ("evaluation.toml", "points_per_survived_model = 10.0",
     "points_per_survived_model = true"),
    # A fraction where truncating to 6 would have hidden the typo.
    ("evaluation.toml", "verification_models = 6", "verification_models = 6.5"),
    # Unparseable, which never reached a coercion at all.
    ("evaluation.toml", 'task = "t"', 'task = "t"\n[[oops'),
    ("behavioural/suite.toml", 'task = "t"', 'task = "t"\n[[oops'),
    # Absent, which already raised ConfigError and so already exited 2 -- it is
    # here for the other half, the file that was still not written.
    ("evaluation.toml", "", ""),
])
def test_a_broken_config_exits_two_and_leaves_a_result(tmp_path, which, old, new):
    task_dir = _task_tree(tmp_path)
    _break(task_dir, which, old, new)

    # Through `main`, not `cmd_behavioural`: the ConfigError-to-2 mapping is
    # `main`'s, and calling the command directly would test half the path.
    rc = cli.main(["behavioural", "--task-dir", str(task_dir),
                   "--repo", str(task_dir / "repo"),
                   "--results", str(task_dir / "results")])
    # `new` and not a `line` that is not a parameter here: the name was wrong in
    # both of these messages, so the one thing a failure of this test could not
    # do was report which case failed -- the f-string raised NameError from
    # inside the assert, and only on the failing path, where nothing else was
    # going to explain it either.
    assert rc == cli.CONFIG_ERROR, f"{which} {new!r} exited {rc}"

    written = task_dir / "results" / "behavioural.json"
    assert written.exists(), f"{which} {new!r} wrote no result file"
    got = json.loads(written.read_text(encoding="utf-8"))
    assert got["status"] == "error"
    assert got["stage"] == "behavioural"
    assert got["notes"], "a failed stage with no note says nothing about why"


def test_the_inferred_task_id_says_that_it_was_inferred(tmp_path):
    """The fallback names the task from its directory, and admits as much.

    ``task`` is a field in the file that just failed to load, so the id in this
    result is a guess -- a good one, since it is where all twenty get it from,
    but not a reading.  A result that presented it as read would be claiming
    to have parsed the file it is reporting as unparseable.
    """
    task_dir = _task_tree(tmp_path)
    _break(task_dir, "evaluation.toml", 'task = "t"', 'task = "t"\n[[oops')
    cli.main(["behavioural", "--task-dir", str(task_dir),
              "--repo", str(task_dir / "repo"),
              "--results", str(task_dir / "results")])

    got = json.loads(
        (task_dir / "results" / "behavioural.json").read_text(encoding="utf-8"))
    assert got["task"] == task_dir.name
    assert any("inferred" in note for note in got["notes"]), got["notes"]
    assert got["metadata"].get("config_error") is True


@pytest.mark.parametrize("stage", ["audit", "behavioural", "verification"])
def test_every_stage_writes_its_own_result_when_the_config_will_not_load(
        tmp_path, stage):
    """All three stages, not just the one the bug was found on.

    The fallback names the file after the stage, so a stage wired to the wrong
    name would overwrite another stage's evidence -- and the stage that ran is
    not always the stage whose file appeared.
    """
    task_dir = _task_tree(tmp_path)
    _break(task_dir, "evaluation.toml", 'task = "t"', 'task = "t"\n[[oops')
    rc = cli.main([stage, "--task-dir", str(task_dir),
                   "--repo", str(task_dir / "repo"),
                   "--results", str(task_dir / "results")])
    assert rc == cli.CONFIG_ERROR
    results = task_dir / "results"
    assert [p.name for p in sorted(results.glob("*.json"))] == [f"{stage}.json"]
    got = json.loads((results / f"{stage}.json").read_text(encoding="utf-8"))
    assert got["stage"] == stage
