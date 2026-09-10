"""Fixtures shared by every pytest module in this suite, loaded as a plugin.

Each module in ``modules/`` is its own process, so these cannot live in a
conftest.py that only one of them would see.  They are a plugin on
``PYTHONPATH`` instead, and every module's ``run.sh`` loads it::

    pytest -p srbfixtures -p swerefactor.pytest_module ...

What a module gets
------------------
``registry``      the build matrix, restored from the `build` module's ledger
``b_assemble`` …  one fixture per configuration, for the common cases
``jars``          the four jars of one configuration, opened
``gson_jar``      the one every other project depends on, or a failure
``runtime``       the five consumer programs, compiled and run against the jars
``test_report``   the JUnit XML the `full` configuration produced, aggregated
``data``          the frozen State-A ground truth, keyed by file stem
``prepare_report`` what the workspace preparation deleted and recorded
``stub_calls``    forbidden build tools the PATH stubs caught

Restored, not rebuilt
---------------------
The matrix is eight Gradle invocations over a tree whose test task alone runs
1328 cases.  The `build` module runs it once and publishes
``$SRB_SUITE_WORK/builds.json``; ``registry`` reads that ledger.  The build trees
are still on disk, so a restored Build answers ``primary_jar()``,
``tasks_executed()`` and ``publish_root()`` from the same directories and the same
recorded log the build produced.

A module that asks for a configuration the ledger does not have gets a *failure*
naming it, not an error and not a fresh build: if the matrix did not produce that
tree, the honest report is that the check could not be made, and rebuilding here
would hide a `build` module that never finished.

There is no fixture here that hands out a source tree
-----------------------------------------------------
Not ``repo``, not ``original``, not one under another name.  This stage answers
"what did the build produce and what does it do"; "is this file the one State A
shipped" is a question about a repository, and stage 1 is where a repository is
read, with State A mounted beside it.  Two things here do touch a tree, and both
touch it as *build input* rather than as evidence -- ``builder._prepare()`` copies
the sources before each configuration, and ``prepare_workspace`` deletes stale
output from them.  Both take their path from ``builder`` directly, so there is
nothing for a check to ask for by accident.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import builder  # noqa: E402
import gsonrun  # noqa: E402
import jarinspect  # noqa: E402

SUITE = Path(os.environ.get("SRB_SUITE_DIR", "/tests/behavioural"))
SHARED = Path(os.environ.get("SRB_SUITE_WORK", "/tmp/srb-work"))
WORK = Path(os.environ.get("SRB_WORK", "/tmp/srb-module"))
DATA = SUITE / "data"
LEDGER = Path(os.environ.get("SRB_BUILD_LEDGER", str(SHARED / "builds.json")))

PROJECTS = builder.PROJECTS


# ------------------------------------------------------------- data fixtures --
@pytest.fixture(scope="session")
def data():
    """All frozen State-A ground truth, keyed by file stem."""
    out = {}
    for path in sorted(DATA.glob("*.json")):
        out[path.stem] = json.loads(path.read_text())
    return out


@pytest.fixture(scope="session")
def prepare_report():
    p = SHARED / "prepare_report.json"
    if p.is_file():
        return json.loads(p.read_text())
    return {}


@pytest.fixture(scope="session")
def stub_calls():
    """Every forbidden build tool the delivered build tried to run."""
    import make_stubs
    return make_stubs.any_stub_called(builder.STUBS)


@pytest.fixture(scope="session")
def scratch(tmp_path_factory):
    """A per-module scratch directory for probes that compile something."""
    return str(tmp_path_factory.mktemp("scratch"))


# ------------------------------------------------------------ build registry --
class Registry:
    """Read-only view of the matrix the `build` module produced."""

    def __init__(self, builds, error=None):
        self._builds = builds
        self._error = error

    def get(self, name):
        if self._error:
            pytest.fail(self._error)
        b = self._builds.get(name)
        if b is None:
            pytest.fail(
                "the build ledger has no configuration %r; the `build` module "
                "produced %s. This check measures a tree that was never built."
                % (name, ", ".join(sorted(self._builds)) or "nothing"))
        return b

    def names(self):
        return sorted(self._builds)


@pytest.fixture(scope="session")
def registry():
    if not LEDGER.is_file():
        return Registry({}, error=(
            "no build ledger at %s -- the `build` module did not run or did not "
            "finish, so there is no build tree to measure." % LEDGER))
    try:
        return Registry(builder.restore(json.loads(LEDGER.read_text())))
    except (ValueError, KeyError, OSError) as exc:
        return Registry({}, error="the build ledger at %s is unusable: %s"
                                  % (LEDGER, exc))


def _bf(name):
    """Build a session fixture that yields one matrix configuration."""
    @pytest.fixture(scope="session")
    def _fixture(registry):
        return registry.get(name)
    return _fixture


b_assemble = _bf("assemble")
b_full = _bf("full")
b_publish = _bf("publish")
b_version = _bf("version")
b_rebuild = _bf("rebuild")
b_relocated = _bf("relocated")
b_add = _bf("m_add")
b_edit = _bf("m_edit")


# ---------------------------------------------------------------- jar access --
#: The frozen ground truth in ``data/`` keys its four jars by artifact id
#: (``gson-extras``), and the Gradle projects that produce them are named for
#: their directories (``extras``).  Both spellings resolve to the same jar, so a
#: check can iterate ``sorted(data["jars"])`` without translating.
ALIASES = {"gson-extras": "extras", "gson-metrics": "metrics",
           "gson-proto": "proto", "gson": "gson"}


class Jars(dict):
    """project -> jarinspect.Jar, or None when the build did not produce it."""

    def __init__(self, build):
        super().__init__()
        self.build = build
        self.reasons = {}
        self.paths = {}
        for name in PROJECTS:
            path = build.primary_jar(name)
            self.paths[name] = path
            if path is None:
                self[name] = None
                self.reasons[name] = (
                    "the %s project produced no jar this check could read; the "
                    "jars found under its build directory were: %s"
                    % (name, ", ".join(os.path.basename(p)
                                       for p in build.libs(name)) or "none"))
                continue
            try:
                self[name] = jarinspect.Jar(path)
            except Exception as exc:               # noqa: BLE001
                self[name] = None
                self.reasons[name] = "%s unreadable: %r" % (path, exc)

    def require(self, name):
        key = ALIASES.get(name, name)
        jar = self.get(key)
        if jar is None:
            reason = self.reasons.get(key, "no jar named %r" % name)
            if not self.build.ok:
                reason = "%s\n\n%s" % (reason, self.build.failure_summary())
            raise AssertionError(reason)
        return jar

    def path_of(self, name):
        return self.paths.get(ALIASES.get(name, name))

    def close(self):
        for j in self.values():
            if j is not None:
                j.close()


def _jf(build_fixture_name):
    @pytest.fixture(scope="session")
    def _fixture(request):
        j = Jars(request.getfixturevalue(build_fixture_name))
        yield j
        j.close()
    return _fixture


jars = _jf("b_assemble")
full_jars = _jf("b_full")
version_jars = _jf("b_version")
rebuild_jars = _jf("b_rebuild")
relocated_jars = _jf("b_relocated")
add_jars = _jf("b_add")
edit_jars = _jf("b_edit")


@pytest.fixture(scope="session")
def gson_jar(jars):
    """The jar the other three depend on; a failure naming it if absent."""
    return jars.require("gson")


# ------------------------------------------------------------ runtime probes --
@pytest.fixture(scope="session")
def runtime(b_assemble):
    """The five consumer programs, compiled against the delivered jars and run.

    Compiled once per module process that asks for it.  Only the `runtime` module
    does, and it is the whole of that module's evidence: five javac invocations
    and five java invocations, about twenty seconds, against a tree the `build`
    module already produced.
    """
    return gsonrun.run_all(b_assemble, str(WORK / "runtime"))


def probe_value(probes, probe, key):
    """One observation, or an AssertionError carrying the probe's own output.

    Every runtime check goes through this rather than indexing the dict, so a
    probe that did not compile reports javac's message once per check instead of
    a KeyError that says nothing about why.
    """
    p = probes[probe]
    if key in p.values:
        return p.values[key]
    raise AssertionError(
        "the %s probe printed no %r line.\n\n%s"
        % (probe, key, p.failure_summary()))


# --------------------------------------------------------------- test report --
@pytest.fixture(scope="session")
def test_report(b_full):
    """The JUnit XML of the `full` configuration, aggregated per project.

    Returns ``{project: {"total": n, "passed": n, "failed": [...],
    "skipped": [...], "cases": {classname#name: outcome}}}`` -- a project whose
    test task never ran is present with zero totals rather than absent, so a
    check can say "the metrics tests did not run" instead of raising.
    """
    out = {}
    for project in PROJECTS:
        cases = jarinspect.parse_junit_dirs(b_full.test_result_dirs(project))
        out[project] = {
            "cases": cases,
            "total": len(cases),
            "passed": sum(1 for v in cases.values() if v == "passed"),
            "failed": sorted(k for k, v in cases.items() if v == "failed"),
            "skipped": sorted(k for k, v in cases.items() if v == "skipped"),
        }
    return out


# ------------------------------------------------------------------- markers --
def pytest_collection_modifyitems(session, config, items):
    """Weigh an `audit` check more than a parity check.

    Both markers say something true about a check: an `audit` failure means
    the artefact is not what the project shipped, a parity failure means one
    detail of it differs.  Stage 1 is where a submission is disqualified, so here
    the marker only weighs more -- three times -- rather than zeroing the task.
    """
    for item in items:
        names = {m.name for m in item.iter_markers()}
        if "srb_weight" in names:
            continue
        if "audit" in names:
            item.user_properties.append(("srb_weight", 3.0))
