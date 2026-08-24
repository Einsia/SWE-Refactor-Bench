"""Every task's stage 3 must be able to say "this tree could not be tested".

The adjudicator's only signal from a candidate is its exit status, and
``Candidate.discriminates`` is ``orig.passed and not sub.passed``.  A tree that
would not build fails every candidate run on it, which is the same shape as a tree
that behaves differently -- so a submission that does not build reads as a
divergence, reproduces across every rerun, and is upheld.  Six rounds report the
same false finding, about a question stage 2 already answered by failing the tree.
``FAULT_EXIT_CODES`` (sysexits 64-78) is the way out: ``Execution.fault``
makes the adjudicator answer ``invalid`` for that round whichever tree it was.

There are two spellings, both in use, and the second was missing when this file
was written:

  * seventeen tasks build in ``run-candidate.sh`` and exit in the failure branch
    directly -- ``exit 71``, ``fault 71 "..."``, ``die_original``.
  * build01, build03 and pf02 build INSIDE the candidate's own pytest process,
    where a failed build is an ``AssertionError`` and pytest exits 1 -- below the
    fault range, and so read as the candidate's own verdict about behaviour.
    Those three register ``lib/srbfault.py``, which turns a build failure that no
    candidate caught into the same exit status the other seventeen write by hand.

What a pass here proves is narrow, and worth being exact about: that a fault
channel EXISTS and is wired up, not that every build path in every task uses it.
The latter is not decidable from the outside -- whether a given failure branch
should be a fault or a verdict is a judgement about that task.  What this catches
is the whole channel being absent or silently unhooked, which is how the three
tasks above shipped.

``exit 64`` is excluded from the count.  All 20 tasks spell the same role guard --
the role argument the harness passes as ``$1`` being neither ``original`` nor
``submission`` -- so "a fault code appears somewhere" is already true everywhere
and says nothing about the build path.  The property that separated the three broken tasks from the
seventeen sound ones is a fault code BEYOND that guard.

The plugin half is also checked at image-build time, by each of the three tasks'
``check-probe.py``, which imports the real modules.  This file parses instead of
importing: a test in ``infra/`` should not execute task code, and all three
plugins are named ``srbfault``, so importing two of them in one interpreter would
hand the second task the first task's table.
"""

from __future__ import annotations

import ast
import builtins
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from swerefactor.verification import FAULT_EXIT_CODES

REPO = Path(__file__).resolve().parents[2]
TASKS = sorted(p for p in (REPO / "tasks").iterdir() if p.is_dir()) \
    if (REPO / "tasks").is_dir() else []

#: The role guard every task shares.  See the module docstring for why a fault
#: code has to be found beyond it for the channel to mean anything.
ROLE_GUARD_CODE = 64

#: The shapes the twenty runners actually use, taken from all of them rather than
#: from one: a bare ``exit N``, a shell helper (``fault 71 "..."``, ``die 70``),
#: and the parenthesised forms a runner written in Python would use.  A needle
#: keyed to a single idiom would report the tasks spelling it differently as
#: uncovered, and the point of this test is the ones that differ.
SHELL_FAULT = re.compile(
    r"\b(?:exit|fault|die|bail|abort)\s+(\d{1,3})\b"
    r"|\b(?:sys\.exit|exit|fault|die)\(\s*(\d{1,3})"
)

#: How the three in-pytest tasks register the plugin on the pytest command line.
PLUGIN_FLAG = "-p srbfault"
PLUGIN_FILE = "lib/srbfault.py"


def shell_fault_codes(verification: Path) -> dict[int, str]:
    """Fault codes reachable from this stage's shell, and where each was found.

    Comment lines are dropped, and that is not cosmetic: several of these scripts
    explain the fault convention in prose that quotes the codes, and one lib module
    documents ``exit 71`` in a docstring.  Matching those would let a task pass this
    test on its own description of a defence it no longer has.
    """
    found: dict[int, str] = {}
    for script in sorted(verification.rglob("*.sh")):
        for number, raw in enumerate(script.read_text(encoding="utf-8").splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            for match in SHELL_FAULT.finditer(line):
                code = int(match.group(1) or match.group(2))
                if code in FAULT_EXIT_CODES:
                    found.setdefault(
                        code, f"{script.relative_to(verification)}:{number}")
    return found


def plugin_table(plugin: Path) -> dict[tuple[str, str], int]:
    """``FAULTS`` out of lib/srbfault.py, read rather than imported.

    ``ast.literal_eval`` on the assignment's value: the table is a literal by
    construction, and parsing keeps a test in infra/ from importing task code.
    """
    tree = ast.parse(plugin.read_text(encoding="utf-8"), filename=str(plugin))
    for node in tree.body:
        targets = ([node.target] if isinstance(node, ast.AnnAssign)
                   else node.targets if isinstance(node, ast.Assign) else [])
        for target in targets:
            if isinstance(target, ast.Name) and target.id == "FAULTS":
                assert node.value is not None, f"{plugin}: FAULTS has no value"
                return ast.literal_eval(node.value)
    raise AssertionError(f"{plugin} defines no FAULTS table")


def exception_classes(module: Path) -> set[str]:
    """Names in ``module`` that are classes deriving, transitively, from a builtin
    exception.

    Transitively because a task is free to give its fault class a base of its own,
    and the plugin matches through the MRO, so such a class is still a fault.  The
    walk is over the names this file defines; a base from somewhere else is taken on
    faith, since the alternative is importing.
    """
    tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
    bases: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            bases[node.name] = [b.id for b in node.bases if isinstance(b, ast.Name)]

    def derives(name: str, seen: frozenset[str] = frozenset()) -> bool:
        if name in seen:
            return False                       # a cycle cannot reach a builtin
        for base in bases.get(name, ()):
            builtin = getattr(builtins, base, None)
            if isinstance(builtin, type) and issubclass(builtin, BaseException):
                return True
            if derives(base, seen | {name}):
                return True
        return False

    return {name for name in bases if derives(name)}


@pytest.mark.parametrize("task", TASKS, ids=lambda p: p.name)
def test_every_task_can_report_a_tree_it_could_not_test(task):
    """One of the two channels has to be there, or a build failure scores as a find."""
    verification = task / "tests" / "verification"
    if not (verification / "run-candidate.sh").is_file():
        pytest.skip("no stage-3 candidate runner")

    shell = {code: where for code, where in shell_fault_codes(verification).items()
             if code != ROLE_GUARD_CODE}
    runner = (verification / "run-candidate.sh").read_text(encoding="utf-8")
    plugin = verification / PLUGIN_FILE
    via_plugin = plugin.is_file() and PLUGIN_FLAG in runner

    assert shell or via_plugin, (
        f"{task.name}'s stage 3 has no way to report a tree that could not be "
        f"tested. Every fault code its shell reaches is the role guard "
        f"({ROLE_GUARD_CODE}), and it does not register {PLUGIN_FILE} with "
        f"`{PLUGIN_FLAG}`. If this task builds in run-candidate.sh, exit one of "
        f"{FAULT_EXIT_CODES[0]}-{FAULT_EXIT_CODES[-1]} in the failure branch; if it "
        f"builds inside the candidate's pytest process, add the plugin. Otherwise a "
        f"submission that does not build passes on the original, fails on the "
        f"submission, reproduces, and is charged up to 60 points for a finding "
        f"stage 2 already scores, or declines to."
    )


@pytest.mark.parametrize("task", TASKS, ids=lambda p: p.name)
def test_a_fault_plugin_is_wired_up_and_its_table_resolves(task):
    """Where the plugin exists, all four of its halves have to hold.

    Each is silent on its own: the table names (module, class), so a renamed lib or
    a renamed exception makes the plugin simply never fire, and a table that is
    never registered is a file nobody loads.  Nothing errors in any of those cases
    -- the fault goes back to being pytest's exit 1, which is a verdict about
    behaviour.  Checked in both directions, so a plugin present but unregistered
    fails here even if the task also faults from its shell.
    """
    verification = task / "tests" / "verification"
    plugin = verification / PLUGIN_FILE
    runner = verification / "run-candidate.sh"
    registered = runner.is_file() and PLUGIN_FLAG in runner.read_text(encoding="utf-8")

    if not plugin.is_file():
        assert not registered, (
            f"{task.name}/tests/verification/run-candidate.sh registers "
            f"`{PLUGIN_FLAG}`, but {PLUGIN_FILE} is not there. pytest fails to load "
            f"a named plugin, so every candidate run would exit 4 -- read as the "
            f"candidate's own trouble, on both trees, for the whole stage."
        )
        pytest.skip("this task faults from its shell; no plugin to check")

    assert registered, (
        f"{task.name} ships {PLUGIN_FILE} but run-candidate.sh does not register it "
        f"with `{PLUGIN_FLAG}`, so it is a file nobody loads and a build failure "
        f"reaches the adjudicator as pytest's exit 1."
    )

    table = plugin_table(plugin)
    assert table, (
        f"{task.name}'s {PLUGIN_FILE} has an empty FAULTS table, so no exception is "
        f"a fault and the plugin cannot fire."
    )

    for (module_name, class_name), code in sorted(table.items()):
        assert code in FAULT_EXIT_CODES, (
            f"{task.name}'s {PLUGIN_FILE} maps {module_name}.{class_name} to exit "
            f"{code}, outside {FAULT_EXIT_CODES[0]}-{FAULT_EXIT_CODES[-1]}. The "
            f"adjudicator would read it as a verdict about behaviour."
        )
        module = verification / "lib" / f"{module_name}.py"
        assert module.is_file(), (
            f"{task.name}'s {PLUGIN_FILE} keys FAULTS on module {module_name!r}, "
            f"and lib/{module_name}.py is not there. The plugin matches on the "
            f"module as well as the class name, so it would never fire."
        )
        assert class_name in exception_classes(module), (
            f"{task.name}'s {PLUGIN_FILE} names {module_name}.{class_name}, which "
            f"lib/{module_name}.py does not define as an exception class. The "
            f"plugin would never fire."
        )


def test_the_three_in_pytest_tasks_are_the_ones_with_a_plugin():
    """A census, so the two channels stay a deliberate split rather than a drift.

    Not a restatement of the per-task tests: those pass for a task with neither
    channel documented and both spellings half-present.  This is the shape of the
    benchmark as a whole -- exactly the tasks that build inside the candidate's
    pytest process carry a plugin, and every other one faults from its shell.
    """
    in_pytest = {"build01-libsodium-autotools-to-cmake",
                 "build03-pycryptodome-setuptools-to-meson",
                 "pf02-stylus-web-platform-port"}
    with_plugin = {task.name for task in TASKS
                   if (task / "tests" / "verification" / PLUGIN_FILE).is_file()}
    if not TASKS:
        pytest.skip("no tasks/ directory")

    assert with_plugin == in_pytest, (
        f"the tasks carrying {PLUGIN_FILE} are {sorted(with_plugin)}, and the tasks "
        f"that build inside the candidate's own pytest process are "
        f"{sorted(in_pytest)}. If a task moved its build into run-candidate.sh it "
        f"should exit a fault code there and drop the plugin; if it moved the other "
        f"way it needs one. Update this list with the change, not after it."
    )


# --------------------------------------------------------------------------- #
# The helpers, against the shapes that would make the tests above vacuous.
# --------------------------------------------------------------------------- #

def test_a_commented_fault_is_not_a_fault(tmp_path):
    """The case that motivated dropping comment lines: prose that quotes the code."""
    (tmp_path / "run-candidate.sh").write_text(
        "#!/bin/sh\n"
        "# a tree that will not build exits 71, which the adjudicator reads as a\n"
        "# fault rather than as a divergence\n"
        'echo "built" || exit 1\n',
        encoding="utf-8")
    assert shell_fault_codes(tmp_path) == {}


def test_a_fault_helper_call_counts(tmp_path):
    """fw06's spelling: a shell function, not a bare ``exit``."""
    (tmp_path / "run-candidate.sh").write_text(
        '#!/bin/sh\nfault() { exit "$1"; }\n'
        'fault 71 "the tree does not build; see $STATE/build.log"\n',
        encoding="utf-8")
    assert sorted(shell_fault_codes(tmp_path)) == [71]


def test_an_inline_brace_exit_counts(tmp_path):
    """pf01's and fw03's spelling: an ``exit`` inside a ``|| { ...; }``."""
    (tmp_path / "run-candidate.sh").write_text(
        '#!/bin/sh\ncp -a "$T" "$S" || { log "cannot copy the tree"; exit 70; }\n',
        encoding="utf-8")
    assert sorted(shell_fault_codes(tmp_path)) == [70]


def test_a_code_below_the_fault_range_is_not_counted(tmp_path):
    """pytest's own 1-5 are verdicts, and 0 is a pass."""
    (tmp_path / "run-candidate.sh").write_text(
        '#!/bin/sh\nexit 1\nexit 2\nexit 5\nexit 63\n', encoding="utf-8")
    assert shell_fault_codes(tmp_path) == {}


def test_the_role_guard_alone_leaves_a_task_uncovered(tmp_path):
    """The exact shape build01, build03 and pf02 shipped in.

    Their runners fault on nothing but the role guard, and their build happens
    inside pytest.  Without the plugin the whole channel is absent, and this is the
    assertion that says so.
    """
    verification = tmp_path / "verification"
    verification.mkdir()
    (verification / "run-candidate.sh").write_text(
        '#!/bin/sh\ncase "$1" in original|submission) ;; '
        '*) echo "unknown target role" >&2; exit 64 ;; esac\n'
        'exec python3 -m pytest -q "$@"\n', encoding="utf-8")
    beyond = {c for c in shell_fault_codes(verification) if c != ROLE_GUARD_CODE}
    assert not beyond
    assert not (verification / PLUGIN_FILE).is_file()


def test_the_table_is_read_from_an_annotated_assignment(tmp_path):
    """The three plugins annotate FAULTS; a plain assignment has to work too."""
    annotated = tmp_path / "annotated.py"
    annotated.write_text(
        'FAULTS: dict[tuple[str, str], int] = {("srbsodium", "BuildFailed"): 71}\n',
        encoding="utf-8")
    plain = tmp_path / "plain.py"
    plain.write_text('FAULTS = {("srbstylus", "DriverFailure"): 70}\n', encoding="utf-8")
    assert plugin_table(annotated) == {("srbsodium", "BuildFailed"): 71}
    assert plugin_table(plain) == {("srbstylus", "DriverFailure"): 70}


def test_a_module_with_no_table_is_an_error_not_an_empty_dict(tmp_path):
    """An empty read would be indistinguishable from a table naming nothing."""
    module = tmp_path / "nofaults.py"
    module.write_text("_FAULTS = {}\n", encoding="utf-8")
    with pytest.raises(AssertionError, match="defines no FAULTS"):
        plugin_table(module)


def test_an_exception_subclass_is_found_through_its_own_base(tmp_path):
    """The plugin matches through the MRO, so a task's own hierarchy still counts."""
    module = tmp_path / "lib.py"
    module.write_text(
        "class BuildFailed(AssertionError):\n    pass\n\n\n"
        "class WontConfigure(BuildFailed):\n    pass\n\n\n"
        "class Configuration:\n    pass\n\n\n"
        "class Loop(Loop):\n    pass\n",
        encoding="utf-8")
    found = exception_classes(module)
    assert {"BuildFailed", "WontConfigure"} <= found
    assert "Configuration" not in found        # a plain class is not a fault
    assert "Loop" not in found                 # a cycle terminates rather than hangs
