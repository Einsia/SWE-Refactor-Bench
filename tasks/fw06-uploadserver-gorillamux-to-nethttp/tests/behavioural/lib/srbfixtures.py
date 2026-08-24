"""Fixtures shared by every behavioural module, loaded as a pytest plugin.

Each module in ``modules/`` is its own process with its own pytest run, so the
fixtures cannot live in a conftest.py that only one of them can see.  They are a
plugin on ``PYTHONPATH`` instead, and every module's ``run.sh`` loads it::

    pytest -p srbfixtures -p swerefactor.pytest_module ...

What a module gets
------------------
``binary``        the submission, rebuilt from its own source by the build module
``reference``     the frozen State A recording
``replayed``      one pass of the request corpus against the binary
``pairs``/``pair``  expected/actual for one recorded HTTP case
``probed``        probe launches, run on first use

There is deliberately no ``repo`` fixture.  This stage builds the submission and
measures what the binary does; a test in it that opened a source file would be
asserting on an implementation rather than on behaviour, and every check that did
lives in stage 1, which reads both trees and executes neither.  Removing the
fixture is what stops the next one being written: a test that wants the tree has
to go and find it, and will notice it is in the wrong stage while doing so.

Session scope is per module, and that is a cost decision.  A module that grades
recorded cases replays only the profiles its own cases need -- `body` launches one
server, `auth` launches one, and neither pays for the other's.  The probe launches
work the same way: a module that reads one battery starts one server.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from harness import corpus, normalize, probe, replay

SUITE = Path(os.environ.get("SRB_SUITE_DIR", "/tests/behavioural"))
DATA = SUITE / "data"
SHARED = Path(os.environ.get("SRB_SUITE_WORK", "/tmp/srb-shared"))
WORK = Path(os.environ.get("SRB_WORK", "/tmp/srb-module"))

#: Published by the build module.  Named for what State A's Makefile produces.
BINARY = SHARED / "upload-server"

#: The recording every expectation comes from.
REFERENCE = DATA / "golden-statea.json"


def _fail_hard(message: str) -> None:
    """Stop the module with a harness fault rather than a wall of failed checks.

    A missing binary or a missing recording is not a property of the submission,
    and reporting it as 40 failed cases would publish a score for something that
    was never measured.  Exit 70 is the runner's "harness fault" code; it makes
    the module's result a re-run rather than a zero.
    """
    print(f"harness fault: {message}", flush=True)
    raise SystemExit(70)


def pytest_configure(config) -> None:
    """Check the module's inputs once, before anything is collected.

    Here rather than in the `binary` fixture, and the difference is not cosmetic:
    from a fixture the same error is re-raised once per test, so a submission that
    failed to build would produce several hundred identical tracebacks per module
    all saying the binary is missing.  One line, once.

    Exempt under `--collect-only`, and that exemption is load-bearing rather than a
    convenience.  The stage image collects every module at build time to prove
    there is no import or parametrisation error -- which is how six modules with a
    missing `case_id` argument were caught -- and it does that long before any
    submission exists, in an image that by design holds no binary.  Nothing runs
    during collection, so none of the inputs checked below are needed yet.
    """
    if getattr(config.option, "collectonly", False):
        return
    if not BINARY.exists():
        _fail_hard(f"no binary at {BINARY}: the build module publishes it, and it "
                   f"either did not run or did not finish")
    if not os.access(BINARY, os.X_OK):
        _fail_hard(f"{BINARY} exists but is not executable")
    if not REFERENCE.is_file():
        _fail_hard(f"no recording at {REFERENCE}")
    fixtures = Path(os.environ.get("SRB_FIXTURE_ROOT", "/opt/fixtures/docroot"))
    if not fixtures.is_dir():
        _fail_hard(f"no fixture document root at {fixtures}: every profile seeds "
                   f"from it, so no case could be replayed")


# --------------------------------------------------------------------------- #
# The binary and the recording
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="session")
def binary() -> str:
    return str(BINARY)


@pytest.fixture(scope="session")
def reference() -> dict:
    """The State A recording: 87 records, keyed by case id.

    Flat rather than nested by profile, because the corpus's case ids are already
    unique across profiles and a case's profile is a field on its record.  A test
    that wants "every auth case" filters on the field; nothing has to know the
    nesting.

    The recording carries the fingerprint of the corpus it answers, and this is
    where the two are held together.  A recording cannot notice being asked about
    different requests than the ones it recorded: change `/files/sub%2Fb.txt` to
    `/files/sub/b.txt` and all 87 comparisons still run, still pass against the
    stored response, and no longer grade the behaviour the case exists for.  So a
    mismatch stops the module as a harness fault rather than scoring anything --
    the suite is not measuring what it thinks it is, and that is not the
    submission's doing.  The stage image runs the same comparison at build time;
    this is the copy that fires when the suite is run by hand.
    """
    import json
    doc = json.loads(REFERENCE.read_text())
    records = doc.get("records") or {}
    if not records:
        _fail_hard(f"{REFERENCE} contains no records")

    from harness import corpus
    stamped = doc.get("corpus_fingerprint")
    actual = corpus.fingerprint()
    if stamped and stamped != actual:
        _fail_hard(
            f"{REFERENCE.name} was captured against corpus {stamped[:16]} and "
            f"harness/corpus.py is now {actual[:16]}.  Some request in the corpus "
            f"was edited after the recording was made, so the stored answers no "
            f"longer belong to the questions being asked.  Re-capture the "
            f"recording, or revert the corpus edit."
        )

    missing = sorted({c["id"] for c in corpus.all_cases()} - set(records))
    if missing:
        _fail_hard(f"{REFERENCE.name} has no record for {len(missing)} corpus "
                   f"case(s): {missing[:6]}")
    return records


# --------------------------------------------------------------------------- #
# Replay, restricted to what this module's tests actually asked for
# --------------------------------------------------------------------------- #

def pytest_collection_modifyitems(session, config, items) -> None:
    """Record which case ids this module collected, for _selected_profiles."""
    ids: list[str] = []
    for item in items:
        params = getattr(getattr(item, "callspec", None), "params", {}) or {}
        for key in ("case_id", "key"):
            val = params.get(key)
            if isinstance(val, str):
                ids.append(val)
    config._srb_collected = ids


@pytest.fixture(scope="session")
def replayed(request, binary, reference) -> dict:
    """One replay pass, covering only the profiles this module's cases live in.

    A launch is the expensive thing here -- a server start, a docroot seed, a
    readiness probe -- so the profile set is derived from what was collected.  On
    a module whose cases are all in one profile that is one launch.
    """
    by_id = {c["id"]: c for c in corpus.all_cases()}
    collected = getattr(request.config, "_srb_collected", []) or []
    case_ids = [cid for cid in collected if cid in by_id]
    if not case_ids:
        return {}
    profiles = sorted({by_id[cid]["profile"] for cid in case_ids})
    workdir = tempfile.mkdtemp(prefix="srb-replay-", dir=str(WORK) if WORK.is_dir()
                               else None)
    try:
        return replay.replay_all(str(binary), workdir, profiles=profiles,
                                 case_ids=case_ids, verbose=False)
    except Exception as exc:  # noqa: BLE001 -- becomes a diagnosis on every case
        return {"__failed__": f"{type(exc).__name__}: {exc}"}


# --------------------------------------------------------------------------- #
# Expected against actual, one recorded case at a time
# --------------------------------------------------------------------------- #

class Pair:
    """One expected/actual record, with accessors the assertions read through.

    There is no volatile-field map here, unlike the equivalent on tasks that
    captured State A twice and intersected the runs.  That approach dropped any
    field the two runs disagreed on, and a dropped field is a hole: a submission
    that stops emitting the header entirely compares equal to one that emits it.
    This corpus masks instead -- `harness.normalize` replaces `Date` with
    `<MASKED>` and a run-created file's `Last-Modified` with
    `<MASKED-WALLCLOCK>`, keeping the header's name and its position in the order.
    So every field in both records is graded, and the masks are visible in the
    recording rather than in a side table.
    """

    __slots__ = ("case_id", "profile_id", "suite", "method", "target", "why",
                 "expected", "actual", "launch_error")

    def __init__(self, case, expected, actual, launch_error=None):
        self.case_id = case["id"]
        self.profile_id = case["profile"]
        self.suite = case["suite"]
        # The request, for the two checks that are conditional on it: HEAD carries
        # a Content-Length describing a body it correctly does not send, and the
        # failure message for a routing case is unreadable without the target.
        self.method = case["method"]
        self.target = case["target"]
        self.why = case["why"]
        self.expected = expected
        self.actual = actual
        self.launch_error = launch_error

    def describe(self) -> str:
        return (f"{self.profile_id}/{self.case_id}  "
                f"[{self.method} {self.target}]")

    # -- the four comparisons every behavioural module makes ----------------- #

    @property
    def expected_body(self) -> bytes:
        return normalize.body_of(self.expected)

    @property
    def actual_body(self) -> bytes:
        return normalize.body_of(self.actual)

    def header(self, name: str, which: str = "actual") -> list[str]:
        rec = self.actual if which == "actual" else self.expected
        return list((rec.get("headers") or {}).get(name.lower(), []))

    def render(self) -> str:
        """Both sides, for a failure message that can be acted on."""
        return (f"{self.describe()}  ({self.why})\n"
                f"  expected: {normalize.describe(self.expected, 300)}\n"
                f"  actual:   {normalize.describe(self.actual, 300)}")


@pytest.fixture(scope="session")
def pairs(replayed, reference) -> dict:
    """Build a Pair for every case this module collected, keyed by case id.

    A profile that failed to launch still yields Pairs -- one per expected case,
    each carrying the launch error -- so the tests for it fail with a diagnosis
    instead of vanishing from the run.  Silently collecting fewer tests would
    quietly shrink the denominator the module's score is a ratio over.
    """
    launch_error = replayed.get("__failed__") if isinstance(replayed, dict) else None
    out: dict[str, Pair] = {}
    for case in corpus.all_cases():
        expected = reference.get(case["id"])
        if expected is None:
            continue
        actual = None if launch_error else (replayed or {}).get(case["id"])
        out[case["id"]] = Pair(case, expected, actual, launch_error)
    return out


@pytest.fixture
def pair(request, pairs) -> Pair:
    """Resolve the Pair for a parametrised ``case_id``, or fail with the reason.

    The calling convention, because it is not guessable: a test parametrised on
    ``case_id`` must name ``case_id`` in its signature even though it reads the
    case through ``pair``::

        @pytest.mark.parametrize("case_id", srbfixtures.cases_in("routing"))
        def test_status(pair, case_id):
            srbcheck.status(pair)

    pytest resolves a parametrised argname against the test's own fixture closure,
    and a name that appears in neither the signature nor a requested fixture is a
    collection error rather than a silent skip.  ``case_id`` is unused in the body;
    it is there so the parametrisation has something to bind to, and `pair` reads
    the bound value back off the callspec.

    Three distinct failures are separated here because they have three different
    causes and three different fixes:

    * the profile did not launch  -> submission bug, the server log says why
    * the case was never replayed -> corpus/recording skew, a verifier problem
    * the id is not in the recording -> a test parametrised on something unmeasured
    """
    case_id = getattr(request, "param", None)
    if case_id is None:
        case_id = request.node.callspec.params.get("case_id")
    p = pairs.get(case_id)
    if p is None:
        pytest.fail(f"{case_id}: not present in the recording -- the tests and the "
                    f"corpus disagree about what was measured")
    if p.launch_error:
        pytest.fail(f"{p.describe()}: the server did not launch under profile "
                    f"{p.profile_id}: {p.launch_error}")
    if p.actual is None:
        pytest.fail(f"{p.describe()}: no response was recorded for this case "
                    f"although its profile launched -- capture skew")
    return p


def cases_in(suite: str) -> list[str]:
    """The case ids for one corpus suite, in corpus order.

    Every behavioural module parametrises on this rather than on a literal list.
    A case added to the corpus is graded by the module that owns its suite without
    an edit here, and a case cannot be silently ungraded by being left out of a
    hand-kept list.
    """
    return [c["id"] for c in corpus.all_cases() if c["suite"] == suite]


# --------------------------------------------------------------------------- #
# Probe launches, run on demand
# --------------------------------------------------------------------------- #
#
# A probe launch starts the binary under one flag set, seeds a document root, and
# runs the battery assigned to it -- generated traffic rather than recorded
# traffic.  There are three launches, only the honesty module reads them, and each
# costs a process start, so they run on first use and are kept for the rest of
# that module's session.
#
# It matters that this happens in-process.  Every battery names the files it
# creates after `probe.RUN_NONCE`, and the assertions compare against that same
# live value -- a launch replayed out of another process's cache would carry a
# nonce the tests cannot match, and would fail for a reason that has nothing to do
# with the submission.

class _Launches(dict):
    """Lazily start a probe launch the first time a battery is asked for."""

    def __init__(self, binary: str):
        super().__init__()
        self._binary = binary

    def __missing__(self, battery: str):
        launch_id = probe.BATTERIES.get(battery)
        if launch_id is None:
            raise KeyError(f"no probe launch declares battery {battery!r}")
        self[battery] = self._run(battery, launch_id)
        return self[battery]

    def get(self, battery, default=None):  # noqa: A003 -- dict.get bypasses __missing__
        try:
            return self[battery]
        except KeyError:
            return default

    def _run(self, battery: str, launch_id: str) -> dict:
        spec = next((s for s in probe.PROBE_LAUNCHES if s[0] == launch_id), None)
        if spec is None:
            return {"__failed__": f"no launch spec {launch_id!r}"}
        _, flags, seed = spec
        workdir = tempfile.mkdtemp(prefix=f"srb-probe-{launch_id}-",
                                   dir=str(WORK) if WORK.is_dir() else None)
        docroot = os.path.join(workdir, "docroot")
        try:
            if seed == "empty":
                os.makedirs(docroot, exist_ok=True)
            else:
                replay.seed_docroot(docroot)
            from harness import wire
            with replay.Server(self._binary, docroot, list(flags)) as srv:
                def send(method, target, headers=None, body=None):
                    resp = wire.request("127.0.0.1", srv.port, method, target,
                                        headers=dict(headers or {}), body=body)
                    # Every probe path is created during the run, so its
                    # Last-Modified is a wall clock and is masked.  The fixture
                    # paths the `absent` battery asks for do not exist at all.
                    return normalize.record(resp, mask_last_modified=True)

                out = probe.BATTERY_FUNCS[battery](send)
            out["__docroot__"] = docroot
            return out
        except Exception as exc:  # noqa: BLE001
            return {"__failed__": f"{type(exc).__name__}: {exc}"}


@pytest.fixture(scope="session")
def probed(binary) -> _Launches:
    return _Launches(str(binary))


@pytest.fixture
def battery(request, probed) -> dict:
    """One probe battery's responses, or a failure naming the launch that broke."""
    name = getattr(request, "param", None)
    if name is None:
        name = request.node.callspec.params.get("battery_name")
    got = probed[name]
    if got.get("__failed__"):
        pytest.fail(f"probe battery {name!r} did not run: {got['__failed__']}")
    return got
