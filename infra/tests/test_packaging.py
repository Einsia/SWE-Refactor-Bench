"""The two invocations the README and CONTRIBUTING.md tell a reader to run.

Both are documented, neither was checked, and a wrong ``pythonpath`` in the pytest
config is a suite that only runs for whoever exported the variable by hand.

Nothing here needs the harness installed: the sixty stage images set
``PYTHONPATH=/opt/swerefactor`` and copy the source in rather than pip-installing it,
so a command line is the whole interface, and the command lines the documentation
prints are the ones that have to work.

Also here: the dependency footprint, asserted against the source rather than
against a metadata list, because the footprint is what those images reproduce.

The fingerprint is what makes a score attributable at all.  A recorded score names
the digest of the source that produced it, so two runs of one task that disagree
can be told apart by what graded them -- see ``test_stage_self_record.py``.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from swerefactor import tomlcompat

REPO = Path(__file__).resolve().parents[2]
PYPROJECT = REPO / "pyproject.toml"


@pytest.fixture(scope="module")
def pyproject() -> dict:
    if not PYPROJECT.is_file():
        pytest.skip("no pyproject.toml at the repository root")
    return tomlcompat.loads(PYPROJECT.read_text(encoding="utf-8"))


def test_the_harness_imports_nothing_but_the_stdlib_and_tomli():
    """The dependency footprint, read off the source rather than a metadata list.

    ``tomli`` is the one exception, needed below 3.11 where there is no
    ``tomllib``, and ``swerefactor.tomlcompat`` prefers the stdlib module wherever it
    exists.  Anything else would have to be present inside twenty stage images
    that copy this source in rather than installing it, which is a build failure
    before anything has been measured.
    """
    pkg = REPO / "infra" / "swerefactor"
    assert (pkg / "__init__.py").is_file()
    local = {p.stem for p in pkg.glob("*.py")}
    allowed = set(sys.stdlib_module_names) | local | {
        "swerefactor", "tomli", "tomllib"}

    foreign: dict[str, set[str]] = {}
    for path in sorted(pkg.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                # Relative imports carry no module name to resolve; they are local
                # by construction.
                names = [node.module or ""] if not node.level else []
            else:
                continue
            for name in names:
                root = name.split(".")[0]
                if root and root not in allowed:
                    foreign.setdefault(path.name, set()).add(root)

    assert not foreign, f"the harness has acquired a dependency: {foreign}"


def test_the_documented_pytest_invocation_finds_the_harness(pyproject):
    """`python3 -m pytest infra/tests -q`, with no PYTHONPATH.

    README states it without the variable, which holds only while the ini sets
    `pythonpath`.  Run as a subprocess from the repository root with PYTHONPATH
    cleared, because this process already has the path inserted above.
    """
    ini = pyproject["tool"]["pytest"]["ini_options"]
    assert ini["pythonpath"] == ["infra"]
    assert ini["testpaths"] == ["infra/tests"]

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "infra/tests/test_packaging.py",
         "--collect-only", "-q"],
        cwd=str(REPO), env={"PATH": "/usr/bin:/bin", "HOME": str(Path.home())},
        capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]


def test_the_documented_validate_invocation_runs(pyproject):
    """`PYTHONPATH=infra python3 -m swerefactor validate --task-dir <task>`.

    One task, not twenty: this asserts the documented command line works, and the
    per-task configuration is validated by the task suites and by CI.
    """
    tasks = sorted(p for p in (REPO / "tasks").iterdir() if p.is_dir())
    if not tasks:
        pytest.skip("no tasks/ tree")
    proc = subprocess.run(
        [sys.executable, "-m", "swerefactor", "validate", "--task-dir", str(tasks[0])],
        cwd=str(REPO),
        env={"PATH": "/usr/bin:/bin", "HOME": str(Path.home()),
             "PYTHONPATH": str(REPO / "infra")},
        capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-2000:]


def test_ci_runs_what_the_readme_documents():
    """CI is the claim that these commands pass; it must run the ones documented.

    A grep rather than a YAML parse: this suite runs on the standard library
    alone, and pulling in a YAML reader to check four lines would make the
    harness's own dependency footprint larger than the harness.
    """
    workflow = REPO / ".github" / "workflows" / "ci.yml"
    if not workflow.is_file():
        pytest.skip("no CI workflow")
    text = workflow.read_text(encoding="utf-8")
    assert "pytest infra/tests" in text, "CI does not run the harness suite"
    assert "swerefactor validate" in text, "CI does not validate the tasks"
    # The floor and the two versions above it.  A matrix that drops 3.10 stops
    # exercising tomlcompat's fallback, which is the only path the donor image and
    # the authoring host do not share.
    for version in ('"3.10"', '"3.11"', '"3.12"'):
        assert version in text, f"CI does not test Python {version}"
