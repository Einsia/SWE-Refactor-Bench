"""Fixtures shared by every behavioural module, loaded as a pytest plugin.

Each module in ``modules/`` is its own process with its own pytest run, so the
fixtures cannot live in a conftest.py that only one of them can see. They are a
plugin on ``PYTHONPATH`` instead, and every module's ``run.sh`` loads it::

    pytest -p srbfixtures -p swerefactor.pytest_module ...

What a module gets
------------------
``binary``        the submission, rebuilt from its own source by the build module
``reference``     the frozen State A recording
``replayed``      one pass of the whole request corpus against the binary
``pairs``/``pair``      expected/actual for one recorded HTTP case
``cli_pairs``/``cli_pair``  the same for one recorded CLI invocation
``probed``        probe launches, run on first use

There is deliberately no ``repo`` fixture. This stage builds the submission and
measures what the binary does; a test in it that opened a source file would be
asserting on an implementation rather than on behaviour, and that question belongs
to stage 1, which reads both trees and executes neither. The absent fixture is what
stops one being written here: a test that wants the tree has to go and find it, and
will notice it is in the wrong stage while doing so.

Session scope is per module. The three modules that compare recordings each
replay the corpus once; the probe launches only what the tests collected in that
module actually asked for.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from harness import cli as cli_corpus
from harness import clirun, corpus, probe, replay
from harness.profiles import case_key

SUITE = Path(os.environ.get("SRB_SUITE_DIR", "/tests/behavioural"))
DATA = SUITE / "data"
SHARED = Path(os.environ.get("SRB_SUITE_WORK", "/tmp/srb-shared"))
WORK = Path(os.environ.get("SRB_WORK", "/tmp/srb-module"))

#: Where the build module leaves the binary every other module measures. It is
#: built from the submission's own source against the offline mirror and is never
#: taken pre-built out of the submitted tree: a committed binary would let a
#: submission ship State A's linked artifact while presenting migrated source,
#: which is the substitution the linked-module check exists to catch.
BINARY = SHARED / "chartmuseum"


def _fail_hard(message: str) -> None:
    """Abort the session with a verifier-side complaint, not a test failure.

    A missing recording or an unreadable binary is a broken grader. Reporting it
    as thousands of failed assertions would score the submission zero for the
    verifier's own fault, so collection stops instead.
    """
    raise pytest.UsageError(message)


def pytest_configure(config) -> None:
    """Refuse to start when there is no binary to measure.

    Raised here rather than left to the ``binary`` fixture, and the difference is
    not cosmetic. From a fixture the same error is re-raised once per test: a
    submission that failed to build produced 10223 identical tracebacks, a 13 MB
    console log and a 14 MB JUnit file, all saying "the binary is missing".
    Measured on State A, which cannot build here by design.

    The module's weight is lost either way -- a submission that does not build
    has demonstrated no behaviour -- so this only buys a legible failure. It says
    so in one line, and the build module's own result says why the build failed.

    Exempt under ``--collect-only``, and not as a convenience: the image build
    collects every module to prove there is no import or parametrisation error,
    and it does that long before any submission exists. Nothing runs during
    collection, so there are no repeated tracebacks here to prevent.
    """
    if getattr(config.option, "collectonly", False):
        return
    if not BINARY.is_file():
        _fail_hard(
            f"{BINARY} does not exist, so the submission did not build. Nothing "
            f"can be measured against it; see the build module's result and its "
            f"build log for why.")
    if not os.access(BINARY, os.X_OK):
        _fail_hard(f"{BINARY} is not executable")


# --------------------------------------------------------------------------- #
# The binary, and the frozen State A recording
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="session")
def binary() -> str:
    """The built submission.

    Already validated by :func:`pytest_configure`, which is why there is nothing
    to check here: the session does not start at all if the binary is missing.
    """
    return str(BINARY)


@pytest.fixture(scope="session")
def reference() -> dict:
    path = DATA / "responses.json"
    if not path.is_file():
        _fail_hard(f"reference recording missing at {path}")
    return json.loads(path.read_text())


#: The names the assertions were written against, kept so that several thousand
#: existing comparisons did not have to be edited to say the same thing.
golden = reference
submission_binary = binary


# --------------------------------------------------------------------------- #
# One replay of the corpus
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="session")
def replayed(binary, reference) -> dict:
    """Play every profile against the submission, once.

    The corpus fingerprint is checked first. The corpus and the recording are
    both committed to this image, so a mismatch means the grader was assembled
    from two different construction states -- comparing them would produce
    failures that describe the verifier rather than the submission.
    """
    want = reference.get("corpus_fingerprint")
    have = corpus.fingerprint()
    if want != have:
        _fail_hard(
            f"corpus fingerprint {have} does not match the recording's {want}: "
            f"the committed corpus and recording disagree, so no comparison "
            f"between them is meaningful")
    workdir = tempfile.mkdtemp(prefix="srb-fw04-replay-", dir=_scratch())
    return replay.play_all(binary, corpus.PROFILES, workdir)


@pytest.fixture(scope="session")
def cli_replayed(binary, reference) -> dict:
    """Run the CLI corpus once. Same fingerprint reasoning as ``replayed``.

    The raw streams are kept on each record here, unlike in the frozen recording:
    capture drops them because raw output carries timestamps and reflowing help
    text, but the graded side needs them live -- a needle is asserted against the
    streams themselves, and the recording stores only which stream carried it.

    A case that cannot be run at all is recorded as ``__failed__`` rather than
    raised, so one broken invocation costs its own tests and not the module.
    """
    want = reference.get("cli_fingerprint")
    have = cli_corpus.fingerprint()
    if want != have:
        _fail_hard(
            f"cli fingerprint {have} does not match the recording's {want}: the "
            f"committed CLI corpus and recording disagree")
    workdir = tempfile.mkdtemp(prefix="srb-fw04-cli-", dir=_scratch())
    out: dict[str, dict] = {}
    for case in cli_corpus.CLI_CASES:
        rundir = os.path.join(workdir, case.id)
        try:
            out[case.id] = clirun.run_case(case, binary, rundir)
        except (OSError, ValueError) as exc:
            out[case.id] = {"__failed__": f"{type(exc).__name__}: {exc}",
                            "id": case.id}
    return out


def _scratch() -> str:
    """The module's own work directory, so its artefacts land beside its log."""
    WORK.mkdir(parents=True, exist_ok=True)
    return str(WORK)


# --------------------------------------------------------------------------- #
# Expected against actual, one recorded CLI invocation at a time
# --------------------------------------------------------------------------- #

class CliPair:
    """One expected/actual CLI recording, plus the case that produced it.

    ``volatile`` comes from the ``_volatile`` key capture wrote into the entry,
    which is why it is read off the expected record rather than a side table: the
    CLI recording is keyed by case, not grouped by profile.
    """

    __slots__ = ("case", "expected", "actual", "volatile", "failed")

    def __init__(self, case, expected, actual):
        self.case = case
        self.expected = expected
        self.actual = actual
        self.volatile = frozenset((expected or {}).get("_volatile", {}))
        self.failed = (actual or {}).get("__failed__")

    @property
    def id(self) -> str:
        return self.case.id

    def field_is_volatile(self, field: str) -> bool:
        return field in self.volatile

    def describe(self) -> str:
        return f"cli/{self.case.id}"


@pytest.fixture(scope="session")
def cli_pairs(cli_replayed, reference) -> dict:
    out: dict[str, CliPair] = {}
    expected_all = reference.get("cli", {})
    by_id = {c.id: c for c in cli_corpus.CLI_CASES}
    for case_id, expected in expected_all.items():
        case = by_id.get(case_id)
        if case is None:
            continue
        out[case_id] = CliPair(case, expected, cli_replayed.get(case_id))
    return out


@pytest.fixture
def cli_pair(request, cli_pairs) -> CliPair:
    case_id = getattr(request, "param", None)
    if case_id is None:
        case_id = request.node.callspec.params.get("case_id")
    p = cli_pairs.get(case_id)
    if p is None:
        pytest.fail(f"cli/{case_id}: not present in the recording")
    if p.failed:
        pytest.fail(f"{p.describe()}: could not be run: {p.failed}")
    if p.actual is None:
        pytest.fail(f"{p.describe()}: no recording was produced")
    return p


# --------------------------------------------------------------------------- #
# Expected against actual, one recorded HTTP case at a time
# --------------------------------------------------------------------------- #

class Pair:
    """One expected/actual recording, plus which of its fields are not contracts.

    ``volatile`` holds dotted paths that the two capture runs disagreed on for
    State A itself. A field that moves when nothing changed cannot be a
    requirement, so the accessors below let a test skip exactly those fields
    rather than the whole case.
    """

    __slots__ = ("profile_id", "case_id", "expected", "actual", "volatile",
                 "launch_error", "log")

    def __init__(self, profile_id, case_id, expected, actual, volatile,
                 launch_error=None, log=""):
        self.profile_id = profile_id
        self.case_id = case_id
        self.expected = expected
        self.actual = actual
        self.volatile = frozenset(volatile)
        self.launch_error = launch_error
        self.log = log

    @property
    def key(self) -> str:
        return case_key(self.profile_id, self.case_id)

    def _vol(self, dotted: str) -> bool:
        return dotted in self.volatile

    @property
    def status_is_volatile(self) -> bool:
        return self._vol(f"{self.case_id}.status")

    @property
    def body_is_volatile(self) -> bool:
        return any(self._vol(f"{self.case_id}.{f}") for f in
                   ("body", "body_sha256", "body_len"))

    def header_is_volatile(self, name: str) -> bool:
        return self._vol(f"{self.case_id}.headers.{name.lower()}")

    def json_is_volatile(self) -> bool:
        return self._vol(f"{self.case_id}.json")

    def field_is_volatile(self, field: str) -> bool:
        return self._vol(f"{self.case_id}.{field}")

    def describe(self) -> str:
        return f"{self.profile_id}/{self.case_id}"


@pytest.fixture(scope="session")
def pairs(replayed, reference) -> dict:
    """Build every Pair once, keyed by ``profile/case``.

    A profile that failed to launch still yields Pairs -- one per expected case,
    each carrying the launch error -- so the tests for it fail with a diagnosis
    instead of vanishing from the run. Silently collecting fewer tests would
    quietly shrink the denominator the module's score is a ratio over.
    """
    out: dict[str, Pair] = {}
    for profile_id, expected_profile in reference["profiles"].items():
        got = replayed.get(profile_id) or {}
        launch_error = got.get("__failed__")
        log = got.get("log", "")
        volatile = reference.get("volatile", {}).get(profile_id, [])
        actual_cases = got.get("cases", {})
        for case_id, expected in expected_profile["cases"].items():
            out[case_key(profile_id, case_id)] = Pair(
                profile_id, case_id, expected, actual_cases.get(case_id),
                volatile, launch_error, log)
    return out


@pytest.fixture
def pair(request, pairs) -> Pair:
    """Resolve the Pair for a parametrised ``key``, or fail with the reason.

    Three distinct failures are separated here because they have three different
    causes and three different fixes:

    * the profile did not launch  -> submission bug, log attached
    * the case was never replayed -> corpus/recording skew, a verifier problem
    * the key is not in the recording -> a test parametrised on something
      unmeasured
    """
    key = getattr(request, "param", None)
    if key is None:
        key = request.node.callspec.params.get("key")
    p = pairs.get(key)
    if p is None:
        pytest.fail(f"{key}: not present in the recording -- the tests and the "
                    f"corpus disagree about what was measured")
    if p.launch_error:
        tail = p.log[-2500:] if p.log else "(no output)"
        pytest.fail(f"{p.describe()}: server did not launch: "
                    f"{p.launch_error}\n--- process output ---\n{tail}")
    if p.actual is None:
        pytest.fail(f"{p.describe()}: no response was recorded for this case "
                    f"although its profile launched -- capture skew")
    return p


# --------------------------------------------------------------------------- #
# Probe launches, run on demand
# --------------------------------------------------------------------------- #
#
# A probe launch starts the binary under one flag set, seeds a storage tree, and
# runs the batteries assigned to it -- generated traffic rather than recorded
# traffic. There are ten launches in ``harness.probe``, no module needs all of
# them, and each one costs a process start.
#
# So the mapping below runs a launch the first time something asks for it and
# keeps it for the rest of that module's session: a module that reads two
# launches pays for two. ``get`` is overridden as well as ``__missing__`` because
# ``probeassert`` reaches the mapping through ``get``, and dict.get does not
# consult ``__missing__``.
#
# It matters that this happens in-process rather than through a file another
# module wrote. Every battery names the resources it creates after
# ``probe.RUN_NONCE``, and the assertions compare against that same live value --
# a launch replayed out of another process's cache would carry a nonce the tests
# cannot match, and would fail for a reason that has nothing to do with the
# submission.

_SPECS = {spec[0]: spec for spec in probe.PROBE_LAUNCHES}


class _Launches(dict):
    """Launch id -> launch record, performing the launch on first access."""

    def __init__(self, binary: str, workdir: str):
        super().__init__()
        self._binary = binary
        self._workdir = workdir

    def __missing__(self, launch_id: str) -> dict:
        rec = self._run(launch_id)
        self[launch_id] = rec
        return rec

    def get(self, launch_id, default=None):          # noqa: D102 - see above
        if launch_id in _SPECS or launch_id in self:
            return self[launch_id]
        return default

    def _run(self, launch_id: str) -> dict:
        from harness.launch import LaunchError

        spec = _SPECS.get(launch_id)
        if spec is None:
            # A grader error, not a submission failure: the test asked for a
            # launch that does not exist. Reported as a failed launch so it lands
            # on the tests that wanted it, with the available ids to fix it by.
            return {"id": launch_id, "batteries": {},
                    "__failed__": f"no such probe launch; the suite declares "
                                  f"{sorted(_SPECS)}"}
        rundir = os.path.join(self._workdir, "probe", launch_id)
        try:
            return probe.run_launch(self._binary, spec, rundir=rundir)
        except LaunchError as exc:
            return {"id": launch_id, "batteries": {}, "__failed__": str(exc),
                    "log": getattr(exc, "log", "")}
        except OSError as exc:
            return {"id": launch_id, "batteries": {},
                    "__failed__": f"{type(exc).__name__}: {exc}"}


@pytest.fixture(scope="session")
def probed(binary) -> dict:
    """The probe, shaped like ``probe.run_all`` but launching only what is read."""
    return {"nonce": probe.RUN_NONCE,
            "launches": _Launches(binary, _scratch())}
