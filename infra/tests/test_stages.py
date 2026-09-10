"""Each stage has to stay in its lane, and the lanes are checkable.

The three stages of this benchmark answer three different questions, and each one
is only worth trusting because of what it *cannot* do:

    1. audit   reads both trees; executes nothing
    2. behavioural  builds both sides; compares what they answer
    3. verification builds both sides, reads both trees, and looks for a
                   disagreement between the two artefacts

The failure this file exists to prevent is stage 2 drifting back into stage 1's
territory, which is what had happened before: modules that started a service and
compared responses also opened source files and failed submissions over the text
in them.  A string in a source file is not behaviour, its meaning depends on which
line it is on, and by the time it reaches stage 2 there is nobody left who can
read the line -- only an assertion with points attached.

So the rule is mechanical: a stage-2 pytest module may not open the submitted
tree.  It is enforced by looking for the ways a test would get at it, and it is
enforced here rather than by review because "don't read the repo in stage 2" is
exactly the kind of rule that holds for a year and then quietly stops.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from swerefactor import config

REPO = Path(__file__).resolve().parents[2]
TASKS = sorted(p for p in (REPO / "tasks").iterdir() if p.is_dir()) \
    if (REPO / "tasks").is_dir() else []

pytestmark = pytest.mark.skipif(not TASKS, reason="no tasks/ tree beside infra/")


def _behavioural_test_files(task: Path):
    suite = task / "tests" / "behavioural" / "modules"
    if not suite.is_dir():
        return
    for path in sorted(suite.rglob("test_*.py")):
        yield path


#: How a stage-2 test would reach the submitted tree.  ``SRB_REPO`` is the
#: environment variable holding it, and ``repo`` is the fixture that hands it over; a
#: module needing a working directory to launch from is a different thing and reads
#: the variable at module scope, which is why the check is on the *test functions*
#: rather than on the file.
#:
#: Names only, and that is this pattern's limit -- it is a list of the four spellings
#: in use, so a tree fixture called something else, ``delivered`` say, is invisible to
#: it.  `test_no_behavioural_plugin_hands_out_a_tree` below follows the value instead
#: of the name; this one stays because a test *parameter* is the cheap half and reads
#: well in the failure message.
TREE_ACCESS = re.compile(
    r"def (test_\w+)\([^)]*\brepo\b[^)]*\)"          # the fixture, as a parameter
    r"|def (test_\w+)\([^)]*\boriginal\b[^)]*\)")    # or the reference tree

#: The environment variables that hold the two trees.  A stage-2 fixture whose body
#: reaches one of these is handing out a tree whatever it is called.
TREE_ENV = ("SRB_REPO", "SRB_ORIGINAL")


@pytest.mark.parametrize("task", TASKS, ids=lambda p: p.name)
def test_no_behavioural_test_takes_a_tree_fixture(task):
    """Stage 2 compares artefacts.  A test that takes the tree is in stage 1.

    The two names are ``repo`` and ``original``: the submitted tree and the
    reference.  Neither has a fixture in any task's stage-2 plugin any more, so a
    test asking for one would error rather than pass silently -- this check is so
    the *fixture* does not come back, which is the step that would make the tests
    possible again.
    """
    offenders: list[str] = []
    for path in _behavioural_test_files(task):
        body = path.read_text(errors="replace")
        for match in TREE_ACCESS.finditer(body):
            name = match.group(1) or match.group(2)
            rel = path.relative_to(task)
            offenders.append(f"{rel}::{name}")
    assert not offenders, (
        f"{task.name}: these stage-2 tests take a source tree as a fixture, "
        f"which makes them assertions about an implementation rather than about "
        f"a built artefact. They belong in the stage that reads code and cannot "
        f"run it: {offenders}"
    )


@pytest.mark.parametrize("task", TASKS, ids=lambda p: p.name)
def test_no_behavioural_plugin_offers_a_tree_fixture(task):
    """And no stage-2 plugin offers the fixture in the first place.

    Deliberately stricter than it needs to be. The test above catches a test that
    uses the fixture; this one catches the fixture, which is the thing that makes
    the next such test a two-line change instead of a decision.
    """
    lib = task / "tests" / "behavioural" / "lib"
    if not lib.is_dir():
        pytest.skip("no stage-2 lib directory")
    offenders: list[str] = []
    for path in sorted(lib.glob("*.py")):
        body = path.read_text(errors="replace")
        for match in re.finditer(r"@pytest\.fixture[^\n]*\)?\s*\ndef (\w+)\(", body):
            if match.group(1) in ("repo", "original", "tree", "source_tree"):
                offenders.append(f"{path.relative_to(task)}::{match.group(1)}")
    assert not offenders, (
        f"{task.name}: the stage-2 fixture plugin hands out the source tree: "
        f"{offenders}. Stage 2 measures a build; the tree is stage 1's input."
    )


def _tree_bound_names(tree: ast.Module) -> set[str]:
    """Module-level names bound, however indirectly, to one of the tree paths.

    Follows assignment through the spellings these plugins actually use --
    ``REPO = os.environ.get("SRB_REPO", ...)``, ``Path(os.environ["SRB_REPO"])``,
    and a second name assigned from the first. Two passes is enough for every
    plugin in the tree and there is no need to guess at more: an alias chain three
    deep to disguise a tree fixture is not the accident this check is for.
    """
    names: set[str] = set()
    for _ in range(2):
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            src = ast.dump(node.value)
            hit = any(v in src for v in TREE_ENV) or any(
                f"id='{n}'" in src for n in names)
            if not hit:
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
    return names


@pytest.mark.parametrize("task", TASKS, ids=lambda p: p.name)
def test_no_behavioural_plugin_hands_out_a_tree(task):
    """The same rule, by what a fixture returns rather than by what it is called.

    The two checks above are name lists, and a name list only catches the names
    somebody already thought of. build01's tree fixture was called ``delivered``:
    it read `SRB_REPO`, it handed the submitted sources to any test that asked, and
    it sat one line below a comment saying stage 2 does not do that -- for as long
    as the list said `repo`/`original`/`tree`/`source_tree`.

    So this walks the plugin's AST, works out which module-level names hold a tree
    (`REPO = os.environ.get("SRB_REPO", ...)` and anything assigned from it), and
    fails any fixture whose body mentions one. Renaming does not help, because the
    value is what is being followed.

    A fixture that legitimately needs a path under a tree -- a build's working
    directory, a before/after snapshot -- opts out with a `srb-allow-tree: <why>`
    comment in its body. That is not a loophole: it is a one-line, greppable,
    reviewed statement, which is the difference between an exception and a drift.
    """
    lib = task / "tests" / "behavioural" / "lib"
    if not lib.is_dir():
        pytest.skip("no stage-2 lib directory")
    offenders: list[str] = []
    for path in sorted(lib.glob("*.py")):
        body = path.read_text(errors="replace")
        try:
            tree = ast.parse(body)
        except SyntaxError:  # pragma: no cover - a broken plugin fails elsewhere
            continue
        tree_names = _tree_bound_names(tree)
        if not tree_names:
            continue
        lines = body.splitlines()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not any("fixture" in ast.dump(d) for d in node.decorator_list):
                continue
            start, end = node.lineno - 1, (node.end_lineno or node.lineno)
            text = "\n".join(lines[start:end])
            if "srb-allow-tree" in text:
                continue
            used = sorted(n.id for n in ast.walk(node)
                          if isinstance(n, ast.Name) and n.id in tree_names)
            if used:
                offenders.append(
                    f"{path.relative_to(task)}::{node.name} (returns a path from "
                    f"{', '.join(sorted(set(used)))})")
    assert not offenders, (
        f"{task.name}: these stage-2 fixtures hand out a source tree regardless of "
        f"what they are named: {offenders}. Stage 2 compares built artefacts; the "
        f"tree is stage 1's input. If one of these is a genuine build input, say so "
        f"with a `srb-allow-tree: <why>` comment in the fixture body."
    )


@pytest.mark.parametrize("task", TASKS, ids=lambda p: p.name)
def test_a_declared_scan_is_advisory_and_reaches_the_prompt(task):
    """If a task declares a scan, its findings have to have somewhere to go.

    Two ways for a scan to be pointless: the prompt does not interpolate it, so
    the observations are computed and discarded, or the prompt does not say they
    are advisory, so a reviewer reads a list of failures as a list of verdicts and
    the scoring problem is back with an extra step.
    """
    evaluation = config.Evaluation.load(task / "tests" / "evaluation.toml")
    stage = evaluation.stages.get("audit")
    if stage is None or not stage.enabled:
        pytest.skip("no audit stage")
    scan_rel = stage.options.get("scan")
    if not scan_rel:
        pytest.skip("no scan declared")

    scan_path = evaluation.resolve(str(scan_rel))
    assert scan_path.exists(), f"{task.name}: scan = {scan_rel!r} does not exist"
    suite = config.Suite.load(scan_path, schema=config.SCAN_SCHEMA)
    assert suite.modules, f"{task.name}: the scan suite declares no modules"

    prompt = evaluation.resolve(str(stage.options["prompt"])).read_text()
    assert "{{findings}}" in prompt, (
        f"{task.name}: a scan is declared but the prompt has no {{{{findings}}}} "
        f"placeholder, so its observations would be computed and thrown away"
    )
    lowered = prompt.lower()
    assert "lead" in lowered or "advisory" in lowered, (
        f"{task.name}: the prompt interpolates scan findings without telling the "
        f"reviewer they are leads rather than verdicts. A list of failures read as "
        f"a list of failures is the scoring problem the scan was moved to avoid."
    )
