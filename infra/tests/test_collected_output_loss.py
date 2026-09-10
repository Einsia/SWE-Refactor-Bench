"""An exclude pattern may not match a name the submission is required to produce.

``_collected_fixture_loss`` compares ``[[artifacts]] exclude`` against
``original.tar.gz``, so it sees a pattern that hits something State A *ships*.  The
complementary failure is a pattern that hits something a correct State B is required
to *create*: pristine State A holds no match, that check passes, and an identity run
passes too, because identity builds both sides from the same tarball and the tarball
is not what such a pattern is aimed at.  Nothing downstream can catch it either --
the collected tree simply arrives without the submission's own program in it.

lang06 is the measured instance and the reason this file exists.  ``jsonnet`` and
``jsonnetfmt`` were excluded as State A's linked ELF binaries at the repository
root; they are also the two names ``behavioural/suite.toml`` declares under
``programs``, and the shortest layout satisfying that contract names its project
directories after them.  Harbor turns each entry into a ``tar --exclude=`` flag, a
bare GNU tar pattern matches by basename at any depth, and on 2026-08-07 three of
the task's six runs were graded on a tree with both CLI projects deleted: 0 of 2609
behavioural checks, ``build/build/discovery`` reporting no projects at all.  The
three that capitalised their directories were collected intact and passed 160 to 179
of 2611.  One published score turned on it, two stage 1 reviews were grounded in the
mutilated tree, and nothing in the artifacts says a directory went missing.

Written against ``_collected_output_loss`` rather than through ``cmd_validate``:
the relation is between two declarations, and a whole task tree in between would
only add ways for a case to pass without the check having run.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from swerefactor import cli, config


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
suite = "behavioural/suite.toml"

[stages.verification]
runner = "swerefactor.verification"
result = "verification.json"
timeout_sec = 21600.0
"""

#: The two names lang06 declares, under the table its own suite puts them in.
SUITE = """\
schema = "swerefactor.suite/1"
task = "t"

[build]
programs = ["jsonnet", "jsonnetfmt"]

[[modules]]
id = "m"
title = "a module"
dir = "modules/m"
weight = 1.0
"""

#: The list as it stands after the fix: debris only, no required name.
TASK_CLEAN = """\
[task]
name = "swerefactor/t"

[[artifacts]]
source = "/workspace/repo"
exclude = ["bin", "obj", "publish", "*.dll", "*.o"]
"""

#: lang06's list as it stood on 2026-08-07, the two bare names included.
TASK_COLLIDING = """\
[task]
name = "swerefactor/t"

[[artifacts]]
source = "/workspace/repo"
exclude = ["bin", "obj", "publish", "*.dll", "*.o", "jsonnet", "jsonnetfmt"]
"""


def _check(tmp_path: Path, *, task: str, suite: str = SUITE,
           evaluation: str = EVALUATION) -> tuple[list[str], list[str]]:
    """Run the check over one task tree; return (problems, notes)."""
    (tmp_path / "tests" / "behavioural").mkdir(parents=True, exist_ok=True)
    (tmp_path / "task.toml").write_text(task, encoding="utf-8")
    (tmp_path / "tests" / "behavioural" / "suite.toml").write_text(
        suite, encoding="utf-8")
    ev_path = tmp_path / "tests" / "evaluation.toml"
    ev_path.write_text(evaluation, encoding="utf-8")
    notes: list[str] = []
    problems = cli._collected_output_loss(
        tmp_path, config.Evaluation.load(ev_path), notes)
    return problems, notes


# --------------------------------------------------------------------------- #
# the defect
# --------------------------------------------------------------------------- #

def test_a_pattern_matching_a_required_program_is_a_problem(tmp_path):
    """The lang06 list fails, once per colliding entry, naming both sides.

    One problem per pattern rather than per name: the fix is always to change one
    entry, and the message has to say which entry and which declared name it
    collided with, or a reader cannot tell a deliberate exclusion from this.
    """
    problems, _ = _check(tmp_path, task=TASK_COLLIDING)
    assert len(problems) == 2, f"expected one per colliding entry, got {problems}"
    joined = "\n".join(problems)
    for name in ("jsonnet", "jsonnetfmt"):
        assert f"excludes {name!r}" in joined, joined
        assert f"{name!r} that suite.toml declares" in joined, joined
    assert "deleted at collection" in joined, joined


def test_the_state_a_check_cannot_see_it(tmp_path):
    """The reason this check exists: the tarball holds no match to find.

    ``_collected_fixture_loss`` is given the same colliding list and a tarball
    that, like lang06's, tracks neither name -- both are build output -- so it
    reports nothing.  If that check could catch this, this one would be dead code,
    and the assertion is what keeps that claim honest rather than remembered.
    """
    import io
    import tarfile

    env = tmp_path / "environment"
    env.mkdir()
    (tmp_path / "task.toml").write_text(TASK_COLLIDING, encoding="utf-8")
    with tarfile.open(env / "original.tar.gz", "w:gz") as tf:
        for rel in ("Makefile", "core/lexer.cpp", "cmd/jsonnet.cpp"):
            data = b"x"
            info = tarfile.TarInfo(f"jsonnet-0.20.0/{rel}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))

    notes: list[str] = []
    assert cli._collected_fixture_loss(tmp_path, env, notes) == [], (
        "the State A check fired on a tarball that tracks neither name")
    assert any("none matching any of the 3 paths" in n for n in notes), notes


# --------------------------------------------------------------------------- #
# the control
# --------------------------------------------------------------------------- #

def test_a_clean_list_passes_and_says_what_it_compared(tmp_path):
    """A list naming no required output is silent except for its note.

    The note carries the count of names actually checked.  Without it a task
    whose suite declares nothing looks identical to one whose suite declares two
    and collides with neither, and only the second has been checked.
    """
    problems, notes = _check(tmp_path, task=TASK_CLEAN)
    assert problems == [], f"a clean list reported: {problems}"
    hits = [n for n in notes if "required program name" in n]
    assert len(hits) == 1, f"expected one note, got {notes}"
    assert "5 artifact exclusion(s)" in hits[0], hits[0]
    assert "2 required program name(s)" in hits[0], hits[0]


def test_a_glob_that_happens_to_match_is_still_a_problem(tmp_path):
    """Matching is by pattern, not by literal equality.

    ``jsonnet*`` is not a name anyone would write to exclude a binary, but a task
    reaching for one glob to cover several products can produce it, and tar would
    apply it to the submission's directory exactly as it applies a bare name.
    """
    task = TASK_CLEAN.replace('"*.o"', '"*.o", "jsonnet*"')
    problems, _ = _check(tmp_path, task=task)
    assert len(problems) == 1, f"expected the glob to fire, got {problems}"
    assert "excludes 'jsonnet*'" in problems[0], problems[0]
    # Both declared names match it, and both are named in the one problem.
    assert "'jsonnet', 'jsonnetfmt'" in problems[0], problems[0]


def test_a_program_count_is_not_a_claim_about_names(tmp_path):
    """``programs = 8`` says how many, not which, and cannot collide.

    pf03 declares a count under that key and build01 declares
    ``corpus_programs = 80``; reading either as a name would make the check fire on
    a pattern that matches the string "8".  Anything but a list of strings is not a
    declaration of names, so a task that only counts is passed over in silence --
    no problem and no note, because nothing was compared.
    """
    suite = SUITE.replace('programs = ["jsonnet", "jsonnetfmt"]', "programs = 8")
    task = TASK_CLEAN.replace('"*.o"', '"*.o", "8"')
    problems, notes = _check(tmp_path, task=task, suite=suite)
    assert problems == [], f"a count was read as a name: {problems}"
    assert not [n for n in notes if "required program name" in n], notes


def test_the_field_is_found_wherever_the_suite_puts_it(tmp_path):
    """Declared names are harvested by key, at any depth.

    The suites do not agree on which table holds the build contract, and a task is
    free to move it.  Keying on the table instead of the field would make the check
    pass by not looking, which is the failure mode it exists to prevent.
    """
    suite = SUITE.replace(
        "[build]\nprograms = [\"jsonnet\", \"jsonnetfmt\"]",
        "[contract.build.expectations]\nprograms = [\"jsonnet\", \"jsonnetfmt\"]")
    problems, _ = _check(tmp_path, task=TASK_COLLIDING, suite=suite)
    assert len(problems) == 2, f"nested declaration was not found: {problems}"
