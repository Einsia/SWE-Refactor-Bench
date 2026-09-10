"""Fixtures shared by every behavioural module, loaded as a pytest plugin.

Each module in ``modules/`` is its own process with its own pytest run, so the
fixtures cannot live in a conftest.py that only one of them can see.  They are a
plugin on ``PYTHONPATH`` instead, and every module's ``run.sh`` loads it::

    pytest -p srbfixtures -p swerefactor.pytest_module ...

What a module gets
------------------
``server``        the submission running behind its own production entry point
``http``          an httpx client
``base_url``      where that server answers
``replay``        one pass of the whole request corpus, recorded
``reference``     the frozen State A responses
``expected_spec`` the frozen State A OpenAPI document

There is deliberately no ``repo`` fixture.  This stage builds the submission and
compares what the running service answers, so a test in it that opened a source
file would be asserting on an implementation rather than on a behaviour.  Reading
both trees belongs to stage 1, which can do it and cannot start either.  The
absence is the mechanism: a test that wants the tree has to go and find it, and
will notice which stage it is in while doing so.

The server is started through a *published* entry point rather than by importing
the app, because "the new implementation is on the default production path" is
part of what is measured: a service that only works when uvicorn is invoked by
hand has not finished migrating.  Which published entry point is not prescribed
-- ``httpbin.bash``, a console script from the built distribution and the
Procfile's ``web:`` line are each tried in turn.  The last resort is the
harness's own uvicorn line, and reaching it is the loss the ``entrypoint`` module
records.

Session scope is per module, so ``corpus`` replays once and asserts three times
while ``concurrency`` gets a server nobody else has touched.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from harness import client as hclient
from harness import corpus, normalize

REPO = Path(os.environ.get("SRB_REPO", "/workspace/repo"))
SUITE = Path(os.environ.get("SRB_SUITE_DIR", "/tests/behavioural"))
DATA = SUITE / "data"
WORK = Path(os.environ.get("SRB_WORK", "/tmp/srb"))
SERVER_HOST = "127.0.0.1"


# --------------------------------------------------------------------------- #
# Frozen State A data
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="session")
def reference():
    path = DATA / "responses.json"
    if not path.exists():
        pytest.fail(f"reference capture missing at {path}")
    return json.loads(path.read_text())["responses"]


@pytest.fixture(scope="session")
def expected_spec():
    path = DATA / "expected-openapi.json"
    if not path.exists():
        pytest.fail(f"reference spec missing at {path}")
    return json.loads(path.read_text())


# The names the suites were written against, kept so that several thousand
# existing assertions did not have to be edited to say the same thing.
golden = reference
golden_spec = expected_spec


# --------------------------------------------------------------------------- #
# The server under test
# --------------------------------------------------------------------------- #

def _free_port() -> int:
    with socket.socket() as s:
        s.bind((SERVER_HOST, 0))
        return s.getsockname()[1]


class ServerHandle:
    def __init__(self, proc, port, log_path, how):
        self.proc = proc
        self.port = port
        self.log_path = log_path
        self.how = how
        #: Candidates tried before this one, with why each failed.
        self.attempts: list[str] = []

    @property
    def base_url(self) -> str:
        return f"http://{SERVER_HOST}:{self.port}"

    def log_tail(self, lines: int = 60) -> str:
        try:
            return "\n".join(
                self.log_path.read_text(errors="replace").splitlines()[-lines:]
            )
        except OSError:
            return "(no server log)"

    def stop(self):
        if self.proc is None or self.proc.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return
        try:
            self.proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass


#: The fallback's ``how``, recognised by name in one place: the `entrypoint`
#: module, which is the only test that cares which candidate answered.
FALLBACK = "harness fallback: uvicorn httpbin:app"


def entry_point_candidates(port: int) -> list[tuple[str, list[str]]]:
    """Ways to start the service, most published first.

    A launcher that insisted on ``httpbin.bash`` measured the name of a file. It
    is one of the names upstream publishes, and it is the one State A uses, but a
    submission that moves its start-up to the console script its own
    ``pyproject.toml`` declares has published an entry point too, and the old
    fixture launched that submission through the harness's private uvicorn line
    and then docked it points for needing the fallback.

    So each candidate is tried in turn and the first one that answers HTTP is the
    one the module ran against. Only the last is the harness's own invention, and
    reaching it means the project publishes no working way to start itself.
    """
    out: list[tuple[str, list[str]]] = []

    script = REPO / "httpbin.bash"
    if script.exists():
        out.append(("httpbin.bash", ["bash", str(script)]))

    # Whatever the built distribution installed as a console script. Read from
    # the venv's bin/, which is the build's own output rather than the tree's.
    bindir = Path(sys.executable).parent
    for name in ("httpbin", "httpbin-server", "serve-httpbin"):
        exe = bindir / name
        if exe.exists() and os.access(exe, os.X_OK):
            out.append((f"console script: {name}", [str(exe)]))

    procfile = REPO / "Procfile"
    if procfile.exists():
        for line in procfile.read_text(errors="replace").splitlines():
            if line.strip().startswith("web:"):
                argv = line.split(":", 1)[1].strip()
                if argv:
                    # `bash -c`, not `bash -lc`.  A login shell re-reads
                    # /etc/profile, which resets PATH to the system default and
                    # drops the venv the submission was installed into -- so
                    # every Procfile naming a console script died with 127,
                    # whatever the submission was.  Procfile semantics are a
                    # plain shell anyway; the -l was doing nothing but harm.
                    out.append(("Procfile web:", ["bash", "-c", argv]))
                break

    out.append((FALLBACK, [
        sys.executable, "-m", "uvicorn", "httpbin:app",
        "--host", SERVER_HOST, "--port", str(port),
    ]))
    return out


def launch(port: int | None = None, extra_env=None, argv=None):
    """Start the submitted service through a published entry point.

    Returns a handle whose ``how`` names the candidate that worked and whose
    ``attempts`` lists the ones that did not, with the reason.
    """
    port = port or _free_port()
    WORK.mkdir(parents=True, exist_ok=True)

    # The venv the `install` module built from the submission.  Its bin/ goes on
    # the front of PATH so that a published entry point naming a command --
    # `gunicorn`, `uvicorn`, a console script of its own -- resolves to the
    # submission's build rather than to the runner venv that is grading it.
    # Inherited PATH starts at the runner's, which has the target stack but no
    # submission, so `uvicorn httpbin:app` from a Procfile would have started
    # the grader's uvicorn and failed to import the app.
    submission_bin = str(Path(sys.executable).parent)

    env = dict(
        os.environ,
        PATH=submission_bin + os.pathsep + os.environ.get("PATH", ""),
        VIRTUAL_ENV=str(Path(sys.executable).parent.parent),
        HTTPBIN_HOST=SERVER_HOST,
        HTTPBIN_PORT=str(port),
        # Some published entry points read the generic pair instead.  gunicorn
        # reads PORT itself, as its own default bind, which is why the upstream
        # Procfile has no -b and still lands where it is told.
        HOST=SERVER_HOST,
        PORT=str(port),
        PYTHONUNBUFFERED="1",
    )
    if extra_env:
        env.update(extra_env)

    if argv is not None:
        candidates = [("explicit argv", list(argv))]
    else:
        candidates = entry_point_candidates(port)

    attempts: list[str] = []
    for index, (how, cmd) in enumerate(candidates):
        log_path = WORK / f"server-{port}-{index}.log"
        log = open(log_path, "wb")
        proc = subprocess.Popen(
            cmd, cwd=str(REPO), env=env,
            stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        handle = ServerHandle(proc, port, log_path, how)
        handle.attempts = attempts

        deadline = time.time() + 90
        while time.time() < deadline:
            if proc.poll() is not None:
                attempts.append(
                    f"{how}: exited with code {proc.returncode} before serving")
                break
            try:
                hclient.wait_for_http(handle.base_url, timeout=2.0)
                return handle
            except Exception:
                time.sleep(0.3)
        else:
            attempts.append(f"{how}: never answered HTTP within 90s")

        handle.stop()

    raise RuntimeError(
        "no way of starting the service answered HTTP on port "
        f"{port}:\n  " + "\n  ".join(attempts) +
        f"\n--- last server log ---\n{handle.log_tail()}"
    )


_launch = launch          # the name the migrated suites call


@pytest.fixture(scope="session")
def server():
    """The long-lived server this module's tests talk to."""
    handle = launch()
    yield handle
    handle.stop()


@pytest.fixture(scope="session")
def base_url(server):
    return server.base_url


@pytest.fixture(scope="session")
def http(server):
    with hclient.make_client() as c:
        yield c


# --------------------------------------------------------------------------- #
# One replay of the whole corpus, shared by every test in the module
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="session")
def replay(server, http):
    """Play every corpus case once; hand each test its own recorded response.

    Playing the corpus once and asserting many times keeps a 400-case corpus
    from turning into 1300 HTTP round trips, and -- more importantly -- means
    the status, header and body assertions for a case all describe the *same*
    response rather than three separate ones.
    """
    out = {}
    errors = {}
    raw = {}
    for case in corpus.CASES:
        try:
            resp = hclient.send_case(http, case, server.base_url)
        except Exception as exc:
            errors[case["id"]] = f"{type(exc).__name__}: {exc}"
            continue
        # Exactly the same construction the reference capture used, so a
        # difference can never come from the recording step.
        out[case["id"]] = normalize.record(
            case, resp.status_code, list(resp.headers.multi_items()),
            resp.content, SERVER_HOST, server.port,
        )
        raw[case["id"]] = resp.content
    return {"responses": out, "errors": errors, "raw": raw}


# `REPO` itself stays: `launch` needs a working directory, and the candidate
# entry points are files in the tree that have to be started from somewhere.
# What it is not is a fixture, so no test can be handed the path.  See the module
# docstring: a test in this stage compares responses, and 437 corpus cases plus a
# concurrency schedule cover more ground than a grep of the source can.
#
# The corpus fixture below is unchanged; `corpus_cases` in suite.toml is the
# number to keep in step with it.
