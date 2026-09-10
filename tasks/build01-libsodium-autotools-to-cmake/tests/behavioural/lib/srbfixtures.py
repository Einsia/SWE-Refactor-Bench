"""Fixtures shared by every pytest module in this suite, loaded as a plugin.

Each module in ``modules/`` is its own process, so these cannot live in a
conftest.py that only one of them would see.  They are a plugin on
``PYTHONPATH`` instead, and every module's ``run.sh`` loads it::

    pytest -p srbfixtures -p swerefactor.pytest_module ...

What a module gets
------------------
``registry``      the build matrix, restored from the `build` module's ledger
``b_default`` …   one fixture per configuration, for the common cases
``p_noavx512`` …  configure-only builds under capability-lacking compilers
``data``          the frozen State-A ground truth, keyed by file stem
``pre_build_snapshot`` the sources as they stood before any build ran
``prepare_report`` what the workspace preparation deleted and recorded
``stub_calls``    Autotools invocations the PATH stubs caught
``corpus_*``      the pristine corpus compiled and run against one install tree

Restored, not rebuilt
---------------------
The matrix is expensive and shared, so the `build` module runs it once and
publishes ``$SRB_SUITE_WORK/builds.json``.  ``registry`` reads that ledger; the
build trees are still on disk, so a restored Build answers ``install_entries()``,
``compile_commands()`` and the rest from the same directories the build wrote.

A module that asks for a configuration the ledger does not have gets a *failure*
naming it, not an error and not a fresh build: if the matrix did not produce that
tree, the honest report is that the check could not be made, and re-running the
build here would hide a `build` module that never finished.
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
import corpus as corpus_mod  # noqa: E402

SUITE = Path(os.environ.get("SRB_SUITE_DIR", "/tests/behavioural"))
SHARED = Path(os.environ.get("SRB_SUITE_WORK", "/tmp/srb-work"))
DATA = SUITE / "data"
REPO = os.environ.get("SRB_REPO", "/workspace/repo")
LEDGER = Path(os.environ.get("SRB_BUILD_LEDGER", str(SHARED / "builds.json")))


# ------------------------------------------------------------- data fixtures --
@pytest.fixture(scope="session")
def data():
    """All frozen State-A ground truth, keyed by file stem."""
    out = {}
    for path in sorted(DATA.glob("*.json")):
        out[path.stem] = json.loads(path.read_text())
    return out


# There is deliberately no `repo` fixture, and no `delivered` one either.
#
# This stage configures the submission eleven times across two generators, compiles
# 80 of libsodium's own test programs against the install tree, and reads the ISA
# flags off 119 translation units. Every one of those is a measurement of
# something that was built. A fixture here that hands a test the source tree
# invites the other kind of check -- walk the tree, look for `Makefile.am` -- and
# thirty of those had accumulated, scored the same as "the shared library exports
# a different symbol table". They are stage 1's now: it reads both trees, it has
# no toolchain, and its findings go to a reviewer who can open the file.
#
# Removing the fixtures is what stops the next one being written. A test that
# wants the tree now has to go and find it, and will notice it is in the wrong
# stage while doing so.
#
# `pre_build_snapshot` below is not an exception to that. It is one half of a
# before/after comparison whose other half is a build output, which is why it is
# phrased as a snapshot and not as a tree.


@pytest.fixture(scope="session")
def pre_build_snapshot():
    """The source tree as it stood *before* any build in this stage touched it.

    For exactly one kind of check: did the build write into the sources. That is a
    question about what the build did, and it needs the before-state to answer -- so
    this is a build input, not the repository handed out for inspection. A test that
    uses it for anything else is reading the tree, and belongs in stage 1.

    Fails rather than falling back to `SRB_REPO`. A fallback would be worse than the
    failure: every build copies from the same snapshot, so with the snapshot absent
    both halves of the comparison resolve to the live tree and the check passes by
    comparing it with itself. That reads as "the build left the sources clean" in the
    report, on a run where the question was never asked.
    """
    if not os.path.isdir(builder.DELIVERED):
        pytest.fail(
            f"the pre-build snapshot is not at {builder.DELIVERED}; the `build` "
            f"module writes it before the first configure, so this run either did "
            f"not reach that step or wrote it elsewhere. Refusing to fall back to "
            f"SRB_REPO: every build copies from this snapshot, so the fallback "
            f"would compare the live tree with itself and report a pass")
    return builder.DELIVERED


@pytest.fixture(scope="session")
def prepare_report():
    p = SHARED / "prepare_report.json"
    if p.is_file():
        return json.loads(p.read_text())
    return {}


@pytest.fixture(scope="session")
def stub_calls():
    """Autotools invocations recorded by the PATH stubs, as raw lines."""
    p = Path(builder.STUBS) / ".calls"
    if not p.is_file():
        return []
    with open(p, errors="replace") as fh:
        return [ln.rstrip("\n") for ln in fh if ln.strip()]


# ------------------------------------------------------------ build registry --
class Registry:
    """The matrix the `build` module produced, plus per-module one-offs.

    `get()` reads the shared matrix and never builds.  `dynamic()` builds a
    configuration nobody else needs, in the asking module's own process.
    """

    def __init__(self, builds, error=None):
        self._builds = builds
        self._error = error
        self._dynamic = {}

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

    def dynamic(self, name, **kwargs):
        """A configuration the shared matrix does not hold, built on demand.

        The shared matrix carries every configuration more than one module reads --
        the full builds and the capability-lacking probes alike, all of them from
        the one ledger the `build` module wrote.  A module
        that needs a one-off -- one option combination out of twelve, a build with
        `CC` pointed somewhere else -- asks for it here: the build runs in this
        module's own process, under its own root so it cannot be mistaken for a
        matrix tree, and is cached for the rest of the session.

        These are not restored from the ledger, so a module that asks twice pays
        once and a module that never asks pays nothing.  `get()` remains the only
        way to reach the shared matrix, and still fails rather than rebuilding: a
        missing matrix tree means the `build` module did not finish, which is a
        different fact from "this check wanted a combination nobody else needs".
        """
        if self._error:
            pytest.fail(self._error)
        key = (name, repr(sorted(kwargs.items())))
        b = self._dynamic.get(key)
        if b is None:
            root = str(SHARED / "dynamic" / os.environ.get("SRB_MODULE_ID", "mod")
                       / name)
            b = builder.Build(name, root=root, **kwargs)
            b.execute()
            self._dynamic[key] = b
        return b


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


b_default = _bf("ninja-default")
b_make = _bf("make-default")
b_minimal = _bf("ninja-minimal")
b_static_only = _bf("ninja-static-only")
b_shared_only = _bf("ninja-shared-only")
b_notests = _bf("ninja-notests")
b_debug = _bf("ninja-debug")
p_nosysrandom = _bf("probe-nosysrandom")
p_noavx512 = _bf("probe-noavx512")
p_noaes = _bf("probe-noaes")
p_nohardening = _bf("probe-nohardening")


# ----------------------------------------------------------- corpus fixtures --
def _corpus_fixture(build_fixture_name, linkage, test_list_key):
    @pytest.fixture(scope="session")
    def _fixture(request, data):
        build = request.getfixturevalue(build_fixture_name)
        names = data["tests"][test_list_key]
        runner = corpus_mod.CorpusRunner(build, linkage=linkage)
        if not build.installed:
            return {"__unavailable__": build.failure_summary(),
                    "runner": runner, "results": {}}
        if linkage == "static" and build.static_lib() is None:
            return {"__unavailable__": "no libsodium.a in the install tree",
                    "runner": runner, "results": {}}
        if linkage == "shared" and build.shared_lib() is None:
            return {"__unavailable__": "no shared libsodium in the install tree",
                    "runner": runner, "results": {}}
        return {"__unavailable__": None, "runner": runner,
                "results": runner.run_all(names)}
    return _fixture


corpus_default_shared = _corpus_fixture("b_default", "shared", "default")
corpus_default_static = _corpus_fixture("b_default", "static", "default")
corpus_minimal_shared = _corpus_fixture("b_minimal", "shared", "minimal")


# --------------------------------------------------------------------- marks --
def pytest_collection_modifyitems(session, config, items):
    """Every test in this suite is a behavioural measurement.

    The checks that judge rather than observe are stage 1's, so what is left here
    is measurement -- and a module that forgets its marker gets the only one that
    applies rather than an error.
    """
    for item in items:
        if not {m.name for m in item.iter_markers()} & {"behaviour", "migration"}:
            item.add_marker(pytest.mark.behaviour)
