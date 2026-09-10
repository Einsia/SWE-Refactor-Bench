"""Can the thing be started, and does the process that starts say what it is.

Every other module in this stage reaches the server through the launcher, which
means every other module would keep passing if the package's own published entry
point had stopped working. That is a real regression for a package whose whole
purpose is to be installed and run, and it is invisible from the wire.

Three observations, all of the running process:

*   a **published** entry point starts it -- the ``bin`` the package declares, or
    the launcher, whichever exists. Which file that is, is the submission's
    choice; that one exists and works is not.
*   the host and port it was given are the host and port it listens on, because a
    server that ignores them is unusable behind anything.
*   neither the wire nor the startup output announces the retired framework.

No file in the tree is read for its contents. ``package.json`` is read for the
``bin`` table, which is the declaration of what may be started -- the same class
of fact the launcher contract is.
"""

from __future__ import annotations

import json as jsonlib
import os
import re
import socket
import subprocess
import time
from pathlib import Path

import pytest
from srbfixtures import BUILD, ROUTES, SEED, WORK

from swerefactor.contract import submission_env

pytestmark = pytest.mark.behaviour

RETIRED_ON_THE_WIRE = re.compile(
    r"\b(express|connect|middie|serve-static|body-parser)\b", re.I)

READY_TIMEOUT = 45.0


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def declared_bins() -> dict[str, str]:
    """The package's ``bin`` table, normalised to a mapping.

    npm accepts either a string or an object here. This is the package's own
    statement of what may be executed after an install, which is why it is the
    thing started below rather than a filename chosen by the grader.
    """
    manifest = jsonlib.loads((BUILD / "package.json").read_text())
    bin_field = manifest.get("bin")
    if isinstance(bin_field, str):
        return {manifest.get("name", "default"): bin_field}
    if isinstance(bin_field, dict):
        return {k: v for k, v in bin_field.items() if isinstance(v, str)}
    return {}


class Started:
    """A process this module started directly, without the launcher."""

    def __init__(self, proc, port: int, log: Path):
        self.proc = proc
        self.port = port
        self.logpath = log

    def log(self) -> str:
        if self.logpath.exists():
            return self.logpath.read_text(errors="replace")[-8000:]
        return ""

    def get(self, path: str) -> tuple[int, dict[str, str], bytes]:
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            conn.request("GET", path, headers={"Host": f"127.0.0.1:{self.port}"})
            resp = conn.getresponse()
            return resp.status, dict(resp.getheaders()), resp.read()
        finally:
            conn.close()

    def stop(self) -> None:
        import signal
        if self.proc.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=10)


def start_directly(argv: list[str], port: int, label: str) -> Started:
    """Start ``argv`` and wait for it to answer on ``port``.

    Deliberately not the launcher. The launcher is a contract the grader relies
    on everywhere else; this asks whether the package can be started the way an
    installed package is started, with a database path and a port on the command
    line.
    """
    work = WORK if WORK.is_dir() else Path("/tmp")
    db = work / f"entrypoint-{label}-{port}.json"
    db.write_text(SEED.read_text())
    logpath = work / f"entrypoint-{label}-{port}.log"

    env = submission_env()
    env["NODE_ENV"] = ""
    with open(logpath, "wb") as log:
        proc = subprocess.Popen(
            [*argv, str(db)],
            cwd=str(BUILD), env=env, stdout=log,
            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            start_new_session=True)
    started = Started(proc, port, logpath)

    deadline = time.monotonic() + READY_TIMEOUT
    last = ""
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(
                f"{' '.join(argv)} exited with {proc.returncode} before "
                f"answering:\n{started.log()}")
        try:
            status, _headers, _body = started.get("/posts")
            if status < 500:
                return started
            last = f"GET /posts -> {status}"
        except OSError as exc:
            last = str(exc)
        time.sleep(0.25)
    started.stop()
    raise AssertionError(
        f"{' '.join(argv)} did not answer on port {port} within "
        f"{READY_TIMEOUT:.0f}s (last: {last}):\n{started.log()}")


@pytest.fixture
def running():
    """Start something, and make sure it is stopped whatever the test does."""
    handles: list[Started] = []

    def _start(argv: list[str], port: int, label: str) -> Started:
        handle = start_directly(argv, port, label)
        handles.append(handle)
        return handle

    try:
        yield _start
    finally:
        for handle in handles:
            handle.stop()


# ---------------------------------------------------------------------------
# It starts
# ---------------------------------------------------------------------------

def test_the_package_declares_something_that_can_be_started():
    """Either a ``bin`` entry or the launcher. Not nothing.

    Which one, and what it is called, is the submission's choice. That an
    installed copy of this package can be started at all is not.
    """
    bins = declared_bins()
    launcher = (BUILD / os.environ.get("SRB_LAUNCHER", "serve.sh"))
    assert bins or launcher.is_file(), (
        "the package declares no bin and ships no launcher, so there is no "
        "published way to start it")


def test_every_declared_bin_exists_on_disk():
    """A ``bin`` pointing at a file the package does not ship is a broken install.

    npm would have created a symlink to nothing. Nothing else in this stage
    notices, because everything else goes through the launcher.
    """
    missing = {name: rel for name, rel in declared_bins().items()
               if not (BUILD / rel).is_file()}
    assert not missing, f"declared bins that do not exist: {missing}"


def test_a_declared_bin_starts_the_server(running):
    """The published entry point, started the way the launcher starts it.

    ``<bin> <db> --host 127.0.0.1 --port <n>`` is the command line State A's
    launcher issues, so it is the one a migrated package still has to accept.
    """
    bins = declared_bins()
    if not bins:
        pytest.skip("the package declares no bin; the launcher is graded by "
                    "every other module in this stage")
    name, rel = sorted(bins.items())[0]
    port = free_port()
    handle = running(["node", str(BUILD / rel), "--host", "127.0.0.1",
                      "--port", str(port)], port, f"bin-{name}")
    status, _headers, raw = handle.get("/posts/1")
    assert status == 200, f"{name} answered {status}: {raw[:200]!r}"
    assert jsonlib.loads(raw)["id"] == 1


def test_the_launcher_starts_the_server_on_the_port_it_was_given(running):
    """The launcher's own contract, exercised on a port nothing else is using.

    Every other module gets its port through the same code path, so a launcher
    that ignored ``JSON_SERVER_PORT`` and listened on a default would have been
    caught. What this adds is the failure mode where it listens on *both*, or
    where it hard-codes the port it was given the first time.
    """
    launcher = BUILD / os.environ.get("SRB_LAUNCHER", "serve.sh")
    if not launcher.is_file():
        pytest.skip("no launcher; the declared bin is graded above")
    port = free_port()
    work = WORK if WORK.is_dir() else Path("/tmp")
    db = work / f"entrypoint-launcher-{port}.json"
    db.write_text(SEED.read_text())
    seed = work / f"entrypoint-launcher-seed-{port}.json"
    seed.write_text(SEED.read_text())

    env = submission_env()
    env.update({"JSON_SERVER_SEED": str(seed), "JSON_SERVER_DB": str(db),
                "JSON_SERVER_PORT": str(port), "JSON_SERVER_HOST": "127.0.0.1",
                "JSON_SERVER_ROUTES": str(ROUTES), "NODE_ENV": ""})
    logpath = work / f"entrypoint-launcher-{port}.log"
    with open(logpath, "wb") as log:
        proc = subprocess.Popen(
            [str(launcher)], cwd=str(BUILD), env=env, stdout=log,
            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            start_new_session=True)
    handle = Started(proc, port, logpath)
    try:
        deadline = time.monotonic() + READY_TIMEOUT
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                pytest.fail(f"the launcher exited with {proc.returncode}:\n"
                            f"{handle.log()}")
            try:
                status, _h, _b = handle.get("/posts")
                if status < 500:
                    break
            except OSError:
                pass
            time.sleep(0.25)
        else:
            pytest.fail(f"the launcher never answered on {port}:\n{handle.log()}")

        status, _headers, raw = handle.get("/posts/1")
        assert status == 200, raw[:200]
    finally:
        handle.stop()


def test_the_server_does_not_also_listen_on_the_default_port(running):
    """Given a port, it listens on that port and not on 3000 as well.

    A port that was never asked for is a port somebody else's process wanted, and
    two graded runs on one machine would collide on it.
    """
    bins = declared_bins()
    if not bins:
        pytest.skip("the package declares no bin")
    _name, rel = sorted(bins.items())[0]

    def occupied() -> bool:
        with socket.socket() as probe:
            probe.settimeout(2)
            return probe.connect_ex(("127.0.0.1", 3000)) == 0

    if occupied():
        pytest.skip("port 3000 was already in use before this test started, so "
                    "what is on it cannot be attributed to the submission")
    port = free_port()
    running(["node", str(BUILD / rel), "--host", "127.0.0.1",
             "--port", str(port)], port, "default-port")
    assert not occupied(), (
        "the server came up on 3000 as well as on the port it was given")


# ---------------------------------------------------------------------------
# What the process says about itself
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def direct(request) -> Started:
    """One directly-started server, shared by the observations below."""
    bins = declared_bins()
    if not bins:
        pytest.skip("the package declares no bin")
    _name, rel = sorted(bins.items())[0]
    port = free_port()
    handle = start_directly(["node", str(BUILD / rel), "--host", "127.0.0.1",
                             "--port", str(port)], port, "observed")
    request.addfinalizer(handle.stop)
    return handle


@pytest.mark.srb_weight(0.0)
@pytest.mark.migration
def test_the_wire_does_not_announce_the_retired_framework(direct):
    """No response header names Express or the connect stack.

    Asserted on header *values* as well as names: ``server: Express`` and
    ``x-powered-by: Express`` are different fields carrying the same admission.

    Recorded at weight 0.0. State A answers every one of these five paths with
    `x-powered-by: Express`, so this is an absence State A cannot produce -- a
    question about whether the migration happened rather than about behaviour a
    rewrite must preserve. Stage 1's `old_stack_retired` gate asks it over both
    trees, where a failure scores the submission zero.

    Still recorded, because it reads the header off the socket of the process this
    module started, and the eight weight-bearing checks around it establish that
    the process is the one the task's own entrypoint launches.
    """
    offences: list[str] = []
    for path in ("/posts", "/posts/1", "/", "/db", "/posts/999999"):
        _status, headers, _raw = direct.get(path)
        for name, value in headers.items():
            if RETIRED_ON_THE_WIRE.search(name) or \
                    RETIRED_ON_THE_WIRE.search(value):
                offences.append(f"{path}: {name}: {value}")
    assert not offences, ("responses named the retired stack:\n  "
                          + "\n  ".join(offences))


@pytest.mark.migration
def test_the_startup_output_does_not_announce_the_retired_framework(direct):
    """The banner json-server prints on boot, read from the process's own output.

    A port that kept the old startup message is a port that kept something that
    prints it. This is the process's output, not a file in the tree: what the
    source says is stage 1's question, what the running process says is this one's.
    """
    log = direct.log()
    hits = sorted(set(RETIRED_ON_THE_WIRE.findall(log)))
    assert not hits, (
        f"the startup output named {hits}:\n{log[:1500]}")


def test_the_startup_output_carries_no_unhandled_error(direct):
    """A server can answer requests and still have printed a stack trace on boot.

    Only the process's own output is read, and only for the shapes Node prints
    when something went wrong that nobody caught.
    """
    log = direct.log()
    for marker in ("UnhandledPromiseRejection", "DeprecationWarning: Buffer",
                   "MaxListenersExceededWarning"):
        assert marker not in log, f"the startup output contains {marker}:\n{log[:1500]}"


def test_it_shuts_down_on_sigterm_without_being_killed(direct):
    """SIGTERM alone has to be enough.

    A server that only stops under SIGKILL leaves its port held and its database
    half-written, and in a container it makes every stop wait out the full grace
    period. The escalation to SIGKILL that the fixture would do on cleanup is
    deliberately not used here -- it would make the assertion true whatever
    happened -- so this sends SIGTERM itself and waits a bounded time.

    Asked last in the file, because it ends the server the observations above
    share.
    """
    import signal
    proc = direct.proc
    if proc.poll() is not None:
        pytest.fail(f"the server had already exited with {proc.returncode} "
                    f"before it was asked to stop:\n{direct.log()}")
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        pytest.fail("the process was still running 15s after SIGTERM; it needs "
                    f"SIGKILL to stop:\n{direct.log()}")
