"""The two things stage 2 does about running graded code in its own container.

Neither is a privilege boundary and neither is claimed to be.  What they are is
the two failures that a shared container produced in practice:

* a module's environment names every path in the grading apparatus, and handing
  that environment on to a submission's build tells it where the answers are;
* a process the module started outlives the module, which means it is still
  running while the runner reads the result file, and still holding its port
  when the next module believes it has one.

Both are tested here against a real suite on disk, because both are properties
of what the runner does to a child process rather than of a data structure.
"""

from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from swerefactor.config import Suite
from swerefactor.contract import CONTRACT_ENV, reap_group, submission_env
from swerefactor.behavioural import SuiteRunner

#: Behavioural-suite files that copy the ambient environment and are exempt from
#: the scrub, because they run from the Dockerfile at image build time -- before
#: a submission exists to hand an environment to, and before the runner has set
#: any of the eight contract names.  Listed by hand rather than sniffed out of
#: the source: an exemption that a comment can grant by accident is not an
#: exemption anybody controls.  ``test_the_image_build_exemptions_are_still...``
#: below checks that each one is still only reachable from a Dockerfile.
IMAGE_BUILD_ONLY = frozenset({
    "lang05-goyaml-go-to-zig/tests/behavioural/lib/freeze.py",
})

SUITE_TOML = """\
schema = "swerefactor.behavioural-suite/1"
task = "t"

[[module]]
id = "m"
title = "the one module"
weight = 1.0
timeout_sec = 60
"""


def _suite(tmp_path: Path, body: str) -> Suite:
    """A one-module suite whose module is ``body``."""
    root = tmp_path / "behavioural"
    (root / "modules" / "m").mkdir(parents=True)
    (root / "lib").mkdir()
    (root / "suite.toml").write_text(SUITE_TOML, encoding="utf-8")
    (root / "modules" / "m" / "run.py").write_text(body, encoding="utf-8")
    return Suite.load(root / "suite.toml")


def _run(tmp_path: Path, body: str):
    suite = _suite(tmp_path, body)
    repo = tmp_path / "repo"
    repo.mkdir()
    original = tmp_path / "original"
    original.mkdir()
    lines: list[str] = []
    runner = SuiteRunner(suite, repo, original, tmp_path / "work", lines.append)
    return runner.run(), lines


# -- the environment handed on -------------------------------------------------

def test_submission_env_removes_every_contract_name():
    base = {name: f"/grader/{name}" for name in CONTRACT_ENV}
    base["PATH"] = "/usr/bin"
    env = submission_env(base)
    assert not (CONTRACT_ENV & set(env)), "a contract name survived the scrub"
    assert env["PATH"] == "/usr/bin", "the rest of the environment was disturbed"


def test_submission_env_keeps_names_a_task_declared():
    """SRB_SHIM and its kind are inputs to the build, not locations of answers.

    This is the distinction the scrub is built on: a task that shims its compiler
    or points at an offline proxy has to have those variables reach the build.  A
    scrub by ``SRB_`` prefix would break exactly those tasks, which is why the
    filter is a set of eight names and not a prefix.
    """
    env = submission_env({"SRB_SHIM": "/shim", "SRB_GOPROXY_ROOT": "/proxy",
                          "SRB_REPO": "/graded"})
    assert env["SRB_SHIM"] == "/shim"
    assert env["SRB_GOPROXY_ROOT"] == "/proxy"
    assert "SRB_REPO" not in env


def test_submission_env_lets_a_caller_pass_one_through_on_purpose():
    env = submission_env({"SRB_REPO": "/graded"}, {"SRB_REPO": "/on-purpose"})
    assert env["SRB_REPO"] == "/on-purpose"


def test_the_module_itself_still_gets_the_whole_contract(tmp_path):
    """The scrub is for what a module launches, never for the module.

    A module that could not read SRB_RESULT could not report anything, so this
    asserts the runner's own contract is untouched by the change above.
    """
    body = """\
import json, os
json.dump({"checks": [{"id": c,
                       "verdict": "pass" if c in os.environ else "fail"}
                      for c in sorted(%r)]},
          open(os.environ["SRB_RESULT"], "w"))
""" % (sorted(CONTRACT_ENV),)
    result, _ = _run(tmp_path, body)
    assert result.checks, "the module wrote nothing"
    missing = [c.id for c in result.checks if not c.ok]
    assert not missing, f"the module was not given: {missing}"


def test_the_runner_logs_the_stage_it_is_filing_under(tmp_path):
    """Stage 1's scan borrows this runner, so the label has to follow the result.

    `scan.py` sets `runner.result.stage = "audit"` and then calls `run()`.
    The opening log line was a literal "behavioural suite", so a stage-1 console
    log announced a behavioural suite and followed it with per-module "33/54
    passed" counts -- stage-1 numbers under stage 2's name, in the one artifact a
    task author reads to find out which stage measured what.
    """
    suite = _suite(tmp_path, "")
    repo = tmp_path / "repo"
    repo.mkdir()
    lines: list[str] = []
    runner = SuiteRunner(suite, repo, None, tmp_path / "work", lines.append)
    runner.result.stage = "audit"
    runner.run()

    opening = next(l for l in lines if "module(s)" in l)
    assert opening.startswith("audit suite:"), opening
    assert "behavioural" not in opening


def test_the_suites_own_env_outranks_the_ambient_one(tmp_path, monkeypatch):
    """A variable a suite declares cannot be redefined from outside the stage.

    This is the only thing standing between lang04's interpreter tripwire and an
    environment variable.  Its driver reads ``SRB_SHIM`` and, when it is "off",
    installs no shim: a submission is then free to spawn node, every
    interpreter-related check is void, and the stage still produces an ordinary
    0-70 rate with a note.  Nothing refuses that run.  What makes it unreachable
    is precedence alone -- ``suite.toml`` sets ``SRB_SHIM = "enforce"`` and
    ``_env_for`` applies ``suite.env`` *after* copying ``os.environ``, so a
    `docker run -e SRB_SHIM=off` is overwritten rather than honoured.

    Two adjacent lines, in that order, are the whole guarantee, and swapping them
    reads like a harmless tidy-up.  So it is asserted here rather than trusted:
    the enforcement is the ordering, not the presence of the setting.
    """
    suite = _suite(tmp_path, "")
    suite.env["SRB_SHIM"] = "enforce"
    monkeypatch.setenv("SRB_SHIM", "off")

    repo, original = tmp_path / "repo", tmp_path / "original"
    repo.mkdir()
    original.mkdir()
    runner = SuiteRunner(suite, repo, original, tmp_path / "work", lambda _m: None)
    env = runner._env_for(suite.modules[0], tmp_path, tmp_path, tmp_path / "r.json",
                          deadline=1_000_000.0)

    assert env["SRB_SHIM"] == "enforce", (
        "the ambient environment overrode the suite's own setting; a caller can "
        "disable a task's tripwire from outside the stage")


def test_a_module_is_told_the_deadline_the_runner_will_enforce(tmp_path):
    """The budget reaches the module, so a timeout can be a partial measurement.

    A module cannot reserve time to write in if it does not know when it will be
    killed, and every module in this benchmark writes its result once at the end --
    so before these names existed, a SIGKILL at the deadline discarded every check
    the module had already found.  lang02's `streaming` lost 175 recorded results
    that way and the run was refused a score it had earned.

    Asserted as a pair, and against the runner's own limit rather than a constant:
    the seconds are what a module sizes its sub-timeouts against and the absolute
    time is what it compares a clock to, and a module trusting the second while the
    first disagrees would reserve its write slice in the wrong place.
    """
    suite = _suite(tmp_path, "")
    module = suite.modules[0]
    repo, original = tmp_path / "repo", tmp_path / "original"
    repo.mkdir()
    original.mkdir()
    runner = SuiteRunner(suite, repo, original, tmp_path / "work", lambda _m: None)
    env = runner._env_for(module, tmp_path, tmp_path, tmp_path / "r.json",
                          deadline=1_700_000_000.0)

    assert env["SRB_MODULE_TIMEOUT_SEC"] == f"{module.timeout_sec:g}"
    assert env["SRB_MODULE_DEADLINE"] == "1700000000.000", (
        "the deadline handed to the module is not the one the caller passed; a "
        "module that reserves a write slice against it will still be killed mid-write")


# -- the process group ---------------------------------------------------------

def test_a_process_left_running_does_not_outlive_its_module(tmp_path):
    """The module exits; its orphan does not survive the runner reading a result.

    The child writes its own pid where the test can find it, then sleeps far past
    the run.  Without the group sweep it is still alive here -- and still able to
    rewrite the result file the runner is about to read.
    """
    body = """\
import json, os, subprocess, sys
pidfile = os.path.join(os.environ["SRB_WORK"], "orphan.pid")
p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
open(pidfile, "w").write(str(p.pid))
json.dump({"checks": [{"id": "spawned", "verdict": "pass"}]},
          open(os.environ["SRB_RESULT"], "w"))
"""
    result, _ = _run(tmp_path, body)
    assert [c.id for c in result.checks] == ["m/spawned"]

    pid = int((tmp_path / "work" / "m" / "orphan.pid").read_text())
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            break
        time.sleep(0.1)
    else:
        os.kill(pid, signal.SIGKILL)
        pytest.fail(f"pid {pid} outlived its module")

    unit = result.unit("m")
    assert unit.metadata.get("reaped_process_group") is True


def test_reaping_does_not_disturb_a_module_that_cleaned_up(tmp_path):
    body = """\
import json, os
json.dump({"checks": [{"id": "tidy", "verdict": "pass"}]},
          open(os.environ["SRB_RESULT"], "w"))
"""
    result, _ = _run(tmp_path, body)
    unit = result.unit("m")
    assert unit.status == "ok"
    assert unit.metadata["exit_code"] == 0
    assert [c.ok for c in result.checks] == [True]


def test_a_module_that_hangs_is_still_a_timeout_and_is_still_reaped(tmp_path):
    suite = _suite(tmp_path, "import time; time.sleep(300)\n")
    suite.modules[0].timeout_sec = 2
    repo = tmp_path / "repo"
    repo.mkdir()
    lines: list[str] = []
    result = SuiteRunner(suite, repo, None, tmp_path / "work", lines.append).run()
    unit = result.unit("m")
    assert unit.status == "timeout"
    assert unit.duration_sec < 30, "the runner waited past the deadline"


def test_reap_group_is_quiet_about_a_group_that_is_already_gone():
    assert reap_group(999_999_999) == 0


# -- the reason contract.py is its own module ----------------------------------

def test_the_contract_module_depends_on_nothing_but_the_standard_library():
    """A task's lib/ imports this module, so its import list is a contract.

    ``swerefactor.behavioural`` reads suite.toml and so reaches a TOML parser.  Two
    of the twenty behavioural images have never proven that import at build time,
    and a scrub that is only present where a TOML reader happens to be installed
    is a scrub that silently is not present.  Anything added to contract.py that
    is not stdlib takes that guarantee away from every task at once.
    """
    import ast

    src = (Path(__file__).resolve().parents[1] / "swerefactor" / "contract.py")
    tree = ast.parse(src.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                pytest.fail("contract.py must not import from the package")
            if node.module:
                imported.add(node.module.split(".")[0])
    assert imported <= {"__future__", "os", "signal", "time"}, (
        f"contract.py grew a dependency: {sorted(imported)}")


def test_every_behavioural_suite_that_copies_the_environment_scrubs_it():
    """The property, checked against the tree rather than against a list.

    A new task that writes ``env = dict(os.environ)`` and hands it to a build has
    put the whole ambient environment back into it.  The check is deliberately
    shallow -- it asks whether the file scrubs at all, not whether it scrubs at
    the right line -- because a deep check here would be a second implementation
    of the thing being tested.
    """
    import re

    repo = Path(__file__).resolve().parents[2]
    ambient = re.compile(r"dict\(os\.environ\)|os\.environ\.copy\(\)|\*\*os\.environ")
    scrubs = re.compile(r"submission_env|startswith\(\s*[\"']SRB_")
    offenders = []
    for py in sorted((repo / "tasks").glob("*/tests/behavioural/**/*.py")):
        rel = str(py.relative_to(repo / "tasks"))
        if rel in IMAGE_BUILD_ONLY:
            continue
        text = py.read_text(encoding="utf-8", errors="replace")
        if not ambient.search(text) or scrubs.search(text):
            continue
        offenders.append(str(py.relative_to(repo)))
    assert not offenders, (
        "these copy the ambient environment without scrubbing the contract:\n  "
        + "\n  ".join(offenders))


def test_the_image_build_exemptions_are_still_only_reachable_at_build_time():
    """Each exemption is granted for one reason; check the reason still holds.

    An exempt file may copy the ambient environment because nothing graded is
    running when it does.  That stops being true the moment a module imports it
    or a suite invokes it, and the exemption has to be withdrawn rather than
    inherited.  So: it must exist, a Dockerfile must run it, and no module
    directory or suite.toml may mention it.
    """
    tasks = Path(__file__).resolve().parents[2] / "tasks"
    for rel in sorted(IMAGE_BUILD_ONLY):
        path = tasks / rel
        assert path.is_file(), f"exemption names a file that is gone: {rel}"
        suite_dir = path.parent.parent            # .../tests/behavioural
        name = path.name
        dockerfile = (suite_dir / "Dockerfile").read_text(encoding="utf-8")
        assert name in dockerfile, (
            f"{rel} is exempt as an image-build script, but its Dockerfile "
            f"never runs it")
        suite_toml = (suite_dir / "suite.toml").read_text(encoding="utf-8")
        stem = path.stem
        for line in suite_toml.splitlines():
            bare = line.split("#", 1)[0]
            assert stem not in bare, (
                f"{rel} is exempt as an image-build script, but suite.toml "
                f"refers to it: {line.strip()!r}")
        importers = [
            py for py in sorted((suite_dir / "modules").rglob("*.py"))
            if f"import {stem}" in py.read_text(encoding="utf-8",
                                               errors="replace")
        ]
        assert not importers, (
            f"{rel} is exempt as an image-build script, but it is imported at "
            f"grade time by: {importers}")
