"""The inputs every module in the behavioural suite is handed.

Loaded as a plugin (`-p srbinputs`) so a module's test file can ask for `data`,
`ledger` or `built` without importing anything.

There is deliberately no `repo` input and no `delivered` input.

That absence is the whole design of this stage. An input handing out
`$SRB_REPO` would make it a two-line change to write a check that walks the
submission looking for a filename, a macro or a string -- and such a check
measures the repository, which is stage 1's input, not this stage's. Everything
here reads a wheel, an installed tree, a compiled object or a test result: things
that exist because a build ran, and that mean the same thing whichever build
system produced them. If a question cannot be phrased against one of those, it
does not belong in this stage.

`pre_build_snapshot` is the one exception, and it is narrow: the manifest of the
delivered tree taken once, before any configuration was copied out of it, so that
"the build wrote into the source tree" is answerable. It fails rather than
falling back to a live read of `$SRB_REPO` -- a snapshot taken after a build has
already run is not a snapshot, and quietly substituting one would turn a real
finding into a pass.
"""

from __future__ import annotations

import gzip
import json
import os
from pathlib import Path

import pytest

import builder

SUITE = Path(os.environ.get("SRB_SUITE_DIR", "/tests/behavioural"))
DATA = SUITE / "data"
WORK = Path(os.environ.get("SRB_SUITE_WORK", "/tmp/srb-suite"))


class Registry:
    """The frozen ground truth, keyed by file stem.

    `data["wheel"]` is `data/wheel.json`; `data["selftest"]` transparently reads
    `selftest.json.gz`. Measured once against State A, shipped in the image, and
    never regenerated during a run -- ground truth that a run can recompute is
    not ground truth.
    """

    def __init__(self, root: Path):
        self.root = root
        self._cache: dict[str, object] = {}

    def __getitem__(self, stem: str):
        if stem not in self._cache:
            plain, gz = self.root / f"{stem}.json", self.root / f"{stem}.json.gz"
            if plain.is_file():
                self._cache[stem] = json.loads(plain.read_text(encoding="utf-8"))
            elif gz.is_file():
                with gzip.open(gz, "rt", encoding="utf-8") as fh:
                    self._cache[stem] = json.load(fh)
            else:
                raise AssertionError(
                    f"no ground truth named {stem!r} in {self.root} "
                    f"(have: {sorted(p.name for p in self.root.glob('*.json*'))})"
                )
        return self._cache[stem]

    def get(self, stem: str, default=None):
        try:
            return self[stem]
        except AssertionError:
            return default


@pytest.fixture(scope="session")
def data() -> Registry:
    return Registry(DATA)


@pytest.fixture(scope="session")
def ledger() -> builder.Ledger:
    """What the `build` module published. Empty rather than absent if it did not run."""
    return builder.Ledger.load()


@pytest.fixture(scope="session")
def built(ledger):
    """`built("default")` -> the Build record, or an AssertionError naming the log.

    Failing here rather than skipping is the point: a submission whose default
    configuration never produced a wheel has to score zero on the checks that
    needed one, and a skip would score it the same as a submission that built.
    """
    return ledger.need


# There is deliberately no `original` fixture either, and this is not because State
# A cannot be built here -- it can, the image installs both backends so that the
# recordings in `data/` are checkable against the tree they came from. It is that a
# fixture pointing at State A's sources could only be used to *read* them, and
# reading sources is stage 1's stage. Rebuilding State A inside a submission's run
# would also double every build in the matrix for a comparison already frozen.
# State A's side of every comparison here comes from `data/`, measured once from a
# real setuptools build in this image's own pinned toolchain.


@pytest.fixture(scope="session")
def pre_build_snapshot() -> dict:
    """The delivered tree's manifest, taken by the `build` module before it built.

    Written to $SRB_SUITE_WORK/pristine.json. Missing means the build module did
    not get far enough to take it, and every check that needs it fails saying so.
    """
    path = WORK / "pristine.json"
    if not path.is_file():
        raise AssertionError(
            f"no pre-build snapshot at {path} -- the build module did not take one, "
            "so whether the build wrote into the source tree cannot be answered"
        )
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def selftest_result():
    """The full suite's result for the default install, run once by the `build` module."""
    path = WORK / "selftest-default.json"
    if not path.is_file():
        raise AssertionError(
            f"no self-test result at {path} -- the build module did not run the suite"
        )
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #

def pytest_configure(config):
    config.addinivalue_line("markers", "behaviour: State A's build results must survive")
    config.addinivalue_line("markers", "migration: the new build system really replaced the old")


def pytest_collection_modifyitems(session, config, items):
    """A module that forgets its marker gets `behaviour`, which is what this stage is.

    Nothing here judges intent; every check reads an artefact. So the default is
    the marker that says so, rather than an error that would cost the submission
    a module's worth of checks for a missing decorator.
    """
    for item in items:
        if not {m.name for m in item.iter_markers()} & {"behaviour", "migration"}:
            item.add_marker(pytest.mark.behaviour)
