"""Fixtures shared by every behavioural module, loaded as a pytest plugin.

Each module in ``modules/`` is its own process with its own pytest run, so the
fixtures cannot live in a conftest.py that only one of them can see. They are a
plugin on ``PYTHONPATH`` instead, and every module's ``run.sh`` loads it::

    pytest -p srbfixtures -p swerefactor.pytest_module ...

What a module gets
------------------
``recorded``   State A's frozen answers, and the volatile map
``replay``     the submission's answers to the same requests, read from disk
``pairs``      ``key -> Pair``: one recorded answer, one live answer, and what
               may legitimately differ between them
``pair``       the ``Pair`` named by a parametrised test's ``key``
``boot``       a factory for modules that need to start their own server

There is deliberately no ``repo`` fixture. This stage boots the submission and
compares what it answers, so a test here that opened a source file would be
asserting on an implementation rather than on a behaviour. Reading both trees
belongs to stage 1, which can do it and cannot start either. The absence is the
mechanism: a test that wants the tree has to go and find it, and will notice
which stage it is in while doing so.

``BUILD`` stays, because ``boot`` needs a working directory and the launcher is a
file that has to be started from somewhere. What it is not is a fixture, so no
test can be handed the path.

What gets booted
----------------
Not the delivered tree. The ``install`` module copies it, drops everything a
build produces, installs offline from the mirror, and runs whatever build script
the package declares; the copy under ``$SRB_SUITE_WORK/build`` is what every
server here is started from. So a submission cannot ship a ``node_modules`` and
be graded on it, and the tree at ``$SRB_REPO`` is left exactly as submitted for
stage 3 to read.

Why the replay is read from disk
--------------------------------
json-server is stateful: a POST changes what the next GET sees. So the requests
must be replayed in order, against a freshly seeded server per mutating session,
and every assertion about a given exchange has to describe the *same* exchange --
otherwise a flaky server passes the status check and fails the body check against
two different responses.

The ``replay`` module does that once, for all sixteen sessions, and writes the
result to ``$SRB_SUITE_WORK``. The five comparison modules read that file. Doing
it per module would mean five times 16 boots and, worse, five different
recordings; doing it in the image would mean grading something the submission did
not produce.
"""

from __future__ import annotations

import email.utils
import json
import os
import tempfile
from pathlib import Path

import pytest

from harness import exchanges
from harness.runner import ServerFailed, Target, pin_static_mtimes

#: The delivered tree, as submitted. Read by `install`, and by nothing else.
SUBMISSION = Path(os.environ.get("SRB_REPO", "/workspace/repo"))
SUITE = Path(os.environ.get("SRB_SUITE_DIR", "/tests/behavioural"))
DATA = SUITE / "data"
WORK = Path(os.environ.get("SRB_WORK", "/tmp/srb"))
SHARED = Path(os.environ.get("SRB_SUITE_WORK", "/tmp/srb-shared"))
#: The installed copy `install` built. Every server here starts from this.
BUILD = SHARED / "build"
LAUNCHER = os.environ.get("SRB_LAUNCHER", "serve.sh")

#: Written by the `replay` module, read by every comparison module.
REPLAY_PATH = SHARED / "replay.json"
RECORDED_PATH = DATA / "recorded-responses.json"

#: The seed and the rewrite rules are handed to the launcher, not taken from the
#: tree under test. See ``harness.runner``.
SEED = DATA / "db.seed.json"
ROUTES = DATA / "routes.json"


# --------------------------------------------------------------------------- #
# State A's frozen answers
# --------------------------------------------------------------------------- #

def load_recorded() -> dict:
    """The recording, with the request list checked against it.

    Grading a request the recording never captured produces confident nonsense,
    so a fingerprint mismatch stops the run rather than failing a test.
    """
    if not RECORDED_PATH.exists():
        raise SystemExit(f"the recording is missing at {RECORDED_PATH}")
    data = json.loads(RECORDED_PATH.read_text())
    stored = data.get("exchanges_fingerprint")
    current = exchanges.fingerprint()
    if stored != current:
        raise SystemExit(
            f"fingerprint mismatch: the recording was made from request list "
            f"{stored}, this harness is {current}. The two describe different "
            f"requests, so nothing here can be compared. Re-record with "
            f"`python -m harness.capture --repo <state-a> --out "
            f"{RECORDED_PATH.name}`.")
    return data


def static_mtime(recorded: dict) -> float | None:
    """The mtime the static files had when the recording was made.

    Read out of the recording's own ``Last-Modified``, so it is a fact about the
    recording rather than a constant somebody has to keep in step with it.
    """
    entry = recorded["responses"].get("read", {}).get("static-index")
    if not entry:
        return None
    values = entry.get("headers", {}).get("last-modified") or []
    if not values:
        return None
    try:
        return email.utils.parsedate_to_datetime(values[0]).timestamp()
    except (TypeError, ValueError):
        return None


@pytest.fixture(scope="session")
def recorded() -> dict:
    return load_recorded()


# --------------------------------------------------------------------------- #
# The submission's answers to the same requests
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="session")
def replay() -> dict:
    """What the `replay` module recorded, session id -> case id -> answer.

    A missing file is a hard stop, not a failure per test: five modules would
    otherwise report several hundred identical failures for one cause, and the
    cause -- the submission could not be replayed -- is already reported, with
    the server log, by the module that tried.
    """
    if not REPLAY_PATH.exists():
        raise SystemExit(
            f"no replay at {REPLAY_PATH}: the `replay` module did not run or "
            f"did not finish. This module has nothing to compare.")
    return json.loads(REPLAY_PATH.read_text())["sessions"]


class Pair:
    """One request's recorded answer, its live answer, and what may differ."""

    def __init__(self, session, case, expected: dict | None, actual: dict | None,
                 volatile: dict, session_failure: str | None):
        self.session = session
        self.session_id = session.id
        self.case = case
        self.key = exchanges.case_key(session.id, case.id)
        self.expected = expected
        self.actual = actual
        #: What State A itself disagreed with itself about, for this request.
        self.volatile = volatile
        self.session_failure = session_failure

    @property
    def status_is_volatile(self) -> bool:
        return "status" in self.volatile

    @property
    def body_is_volatile(self) -> bool:
        return bool(self.volatile.get("body"))

    def header_is_volatile(self, name: str) -> bool:
        return name.lower() in {h.lower()
                                for h in self.volatile.get("headers", [])}

    def waived_headers(self) -> set[str]:
        """Headers this request does not compare.

        Three sources: the per-case waivers, whatever State A disagreed with
        itself about across the capture runs, and -- for the exchanges whose body
        is a stack trace -- the validators computed over that body.

        The last one is not a concession. Express derives ETag from the bytes it
        is about to send, and those bytes contain absolute paths into
        node_modules. The comparison scrubs those paths to `/PATH/` precisely
        because they are a property of the machine, not of the code; the ETag is
        computed before the scrub, so its length prefix carries the unscrubbed
        path lengths. State A measured against its own recording failed three of
        these -- `W/"658-..."` against `W/"392-..."`, a difference of exactly 710
        bytes in all three, which is the difference between the capture tree's
        path depth and the grader's. No submission can control it and none should
        be asked to.

        What still holds for those exchanges: the body with frames removed is
        compared byte for byte, the stack must still be present, the status must
        match, and `stack_validator_shape` requires an ETag of the same form.
        """
        out = {h.lower() for h in self.case.headers_skip}
        out |= {h.lower() for h in self.volatile.get("headers", [])}
        if self.case.body_mode == "stack":
            out |= {"etag", "content-length"}
        return out

    def describe(self) -> str:
        out = [f"{self.key}: {self.case.method} {self.case.path}"]
        if self.session.argv:
            out.append(f"  server argv: {list(self.session.argv)}")
        if self.case.note:
            out.append(f"  note: {self.case.note}")
        return "\n".join(out)


@pytest.fixture(scope="session")
def pairs(replay, recorded) -> dict:
    """key -> Pair, for every request in the list."""
    out: dict[str, Pair] = {}
    volatile = recorded.get("volatile", {})
    for session in exchanges.SESSIONS:
        live = replay.get(session.id, {})
        expected = recorded["responses"].get(session.id, {})
        for case in session.cases:
            key = exchanges.case_key(session.id, case.id)
            out[key] = Pair(
                session=session,
                case=case,
                expected=expected.get(case.id),
                actual=live.get(case.id),
                volatile=volatile.get(key, {}),
                session_failure=live.get("__failed__"),
            )
    return out


# --------------------------------------------------------------------------- #
# Which exchanges a module is about
# --------------------------------------------------------------------------- #

#: The sessions run with default flags that change the database.
WRITE_SESSIONS = ("create", "create-body", "replace", "merge", "delete",
                  "nested-write", "override")

#: The sessions that exist to exercise a launcher flag.
OPTION_SESSIONS = ("read-only", "no-cors", "no-gzip", "inert-flags",
                   "custom-id", "custom-fk", "delay", "static-alt")

#: Every exchange, in replay order.
ALL_KEYS = [exchanges.case_key(sid, case.id)
            for sid, case in exchanges.all_cases()]


def keys_for(*session_ids: str) -> list[str]:
    """The keys belonging to the named sessions, in replay order.

    The three bulk comparison modules partition the whole list between them by
    session -- ``read`` takes the default-flag session, ``write`` the seven that
    mutate, ``options`` the eight that pass a flag -- so every exchange is graded
    exactly once and no module can silently drop one. ``semantics`` deliberately
    revisits a named handful of these, asserting the specific quirk rather than
    byte equality, which is duplication with a purpose: it turns "a body differed"
    into "the weak ETag is not in Express's format".
    """
    wanted = set(session_ids)
    unknown = wanted - {s.id for s in exchanges.SESSIONS}
    if unknown:
        raise AssertionError(f"no such session(s): {sorted(unknown)}")
    return [k for k in ALL_KEYS if k.split("::")[0] in wanted]


def parametrize(*session_ids: str):
    """Decorator: run this test once per exchange in the named sessions."""
    return pytest.mark.parametrize("key", keys_for(*session_ids))


@pytest.fixture
def pair(request, pairs) -> Pair:
    """The Pair named by the test's ``key`` parameter."""
    key = request.node.callspec.params["key"]
    p = pairs[key]
    if p.session_failure:
        pytest.fail(f"session {p.session_id} did not start:\n{p.session_failure}")
    if p.expected is None:
        pytest.fail(f"{p.key} is not in the recording; the request list and the "
                    f"recording are out of step")
    if p.actual is None:
        pytest.fail(f"{p.key} was never recorded against the submission")
    return p


# --------------------------------------------------------------------------- #
# For modules that boot their own server
# --------------------------------------------------------------------------- #

class Booted:
    """A server this module started, and the scratch directory behind it."""

    def __init__(self, target: Target, tmp: Path):
        self.target = target
        self.tmp = tmp

    @property
    def port(self) -> int:
        return self.target.port

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.target.port}"

    def request(self, case):
        return self.target.request(case)

    def log(self) -> str:
        return self.target._log()          # noqa: SLF001 -- the log is the point


@pytest.fixture(scope="session")
def boot(recorded):
    """Factory: ``boot(session)`` gives a running server for that configuration.

    Used by the modules whose question cannot be answered from the shared replay
    -- invented data, overlapping requests, a packed release. Every server is
    started the same way the replay started its own, through the launcher, with
    the grader's seed and rules, and is stopped when the module ends.
    """
    if not (BUILD / "package.json").is_file():
        raise SystemExit(
            f"nothing installed at {BUILD}: the `install` module did not run or "
            f"did not finish. There is no server to start.")
    started: list[Booted] = []
    epoch = static_mtime(recorded)
    if epoch is not None:
        pin_static_mtimes(BUILD, epoch)

    def _boot(session, repo: Path | None = None, seed: Path | None = None) -> Booted:
        tmp = Path(tempfile.mkdtemp(prefix=f"srb-{session.id}-", dir=str(WORK)
                                    if WORK.is_dir() else None))
        target = Target(repo or BUILD, LAUNCHER, session, tmp,
                        seed or SEED, ROUTES)
        target.start()
        handle = Booted(target, tmp)
        started.append(handle)
        return handle

    try:
        yield _boot
    finally:
        for handle in started:
            handle.target.stop()


# Names the migrated modules use.
ServerFailed = ServerFailed
