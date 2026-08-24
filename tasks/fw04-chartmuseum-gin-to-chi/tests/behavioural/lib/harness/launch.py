"""Launch a ChartMuseum -- the frozen oracle or the submission -- and talk to it.

The single rule this module exists to enforce: **both sides are launched by the
same code path**.  A capture run and a grading run differ only in which binary
path is handed to :func:`serve`.  If the oracle got a bespoke launcher and the
submission got another, every difference between the two launchers would be
indistinguishable from a migration defect, and the suite would be measuring
itself.

Three things make that harder than "run the binary":

*Storage is per-launch and seeded from frozen bytes.*  ChartMuseum writes into
its storage root, so a shared directory would let an upload in one profile decide
what the next profile reads.  Each launch gets a private directory populated by
copying from the read-only fixture tree -- copied with mtimes preserved, because
the local backend reports an object's mtime as the index entry's ``created:``
field and the fixtures were frozen at a fixed epoch precisely so that field is a
constant rather than a timestamp.

*Readiness is flag-dependent.*  ``/health`` is registered with an empty auth
action, so neither basic nor bearer auth hides it, but ``--context-path`` moves it
and ``--tls-cert`` changes its scheme.  A fixed ``http://host/health`` probe would
declare a perfectly working server dead on 6 of the 56 profiles.  The probe is
therefore derived from the same flag list the server was given.

*A dead process must not look like a slow one.*  If the binary exits during
startup -- a rejected flag, a port clash, a panic -- polling until timeout turns a
one-line "unknown flag" into a 10-second wait and a misleading message.  Every
poll checks liveness first and surfaces the process's own output.
"""

from __future__ import annotations

import contextlib
import http.client
import os
import shutil
import socket
import ssl
import subprocess
import time
import urllib.parse

from swerefactor.contract import submission_env

# Where the frozen fixtures live.  Same value in the environment and verifier
# images; see environment/scripts/install-testdata.sh.
FIXTURES = os.environ.get("CM_ORACLE_CHARTS", "/opt/testdata")
WEBTEMPLATES = os.environ.get("CM_WEB_TEMPLATES", "/opt/webtemplate")

# How long a server gets to answer its first probe.  Generous: the verifier may
# be running dozens of launches on a loaded 4-CPU box, and a timeout here is
# scored as a failure, so the cost of being wrong is asymmetric.
BOOT_TIMEOUT_S = 25.0
POLL_INTERVAL_S = 0.05

# How long a single request may take before it is called a failure.  Long enough
# that a cold index regeneration over a seeded repo is never mistaken for a hang.
REQUEST_TIMEOUT_S = 30.0


class LaunchError(RuntimeError):
    """A server never became ready.  Carries the process output verbatim.

    The output matters more than the message: for a submission that fails to
    boot, the Go panic or the flag-parsing error IS the diagnosis, and a grading
    log that swallowed it would make the failure unactionable.
    """

    def __init__(self, message: str, *, argv: list[str], log: str, rc: int | None):
        self.argv = argv
        self.log = log
        self.rc = rc
        detail = log.strip()[-4000:] or "(no output)"
        super().__init__(
            f"{message}\n"
            f"--- argv: {' '.join(argv)}\n"
            f"--- exit: {'still running' if rc is None else rc}\n"
            f"--- output ---\n{detail}"
        )


def free_port() -> int:
    """Ask the kernel for an unused port.

    Every launch gets its own so nothing can race for a socket.  There is an
    inherent gap between closing this socket and the server binding it; SO_REUSEADDR
    plus a fresh port per launch keeps that gap harmless in practice, and a genuine
    clash surfaces as a LaunchError naming the bind failure rather than as a silent
    wrong answer.
    """
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def seed_storage(root: str, seeds: tuple[str, ...]) -> None:
    """Populate a fresh storage directory from the frozen fixtures.

    ``seeds`` entries are ``dest=chart[,chart...]``:

    * ``"=testdata/charts/mychart/mychart-0.1.0.tgz"`` puts the chart flat in the
      storage root, which is what ``--depth=0`` serves.
    * ``"org1/repo1=..."`` puts it under that prefix, for ``--depth>=1``.

    Copies with :func:`shutil.copy2` -- mtime included, deliberately.  See the
    module docstring on ``created:``.
    """
    os.makedirs(root, exist_ok=True)
    for spec in seeds:
        dest, _, listing = spec.partition("=")
        target = os.path.join(root, dest) if dest else root
        os.makedirs(target, exist_ok=True)
        for rel in listing.split(","):
            rel = rel.strip()
            if not rel:
                continue
            src = os.path.join(FIXTURES, rel)
            if not os.path.isfile(src):
                raise FileNotFoundError(
                    f"no such frozen fixture: {rel} (looked in {FIXTURES})")
            shutil.copy2(src, os.path.join(target, os.path.basename(rel)))


def _probe_target(flags: tuple[str, ...]) -> tuple[str, str, bool]:
    """Derive (scheme, health-path, verify-tls) from the server's own flags."""
    scheme, prefix, tls = "http", "", False
    for f in flags:
        if f.startswith("--context-path="):
            prefix = f.split("=", 1)[1]
        elif f.startswith("--tls-cert="):
            scheme, tls = "https", True
    # The fixtures' TLS pair is self-signed and cannot chain to any CA, so
    # verification is off.  Nothing in this suite asserts anything about the
    # certificate; the TLS profiles exist to check that the port still speaks
    # https at all and still routes identically underneath it.
    return scheme, f"{prefix}/health", tls


class Server:
    """A running ChartMuseum, and the connection settings needed to reach it."""

    def __init__(self, *, proc, port, scheme, storage, argv, logpath):
        self.proc = proc
        self.port = port
        self.scheme = scheme
        self.storage = storage
        self.argv = argv
        self.logpath = logpath

    @property
    def log(self) -> str:
        try:
            with open(self.logpath, "r", errors="replace") as fh:
                return fh.read()
        except OSError:
            return ""

    def connection(self):
        if self.scheme == "https":
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return http.client.HTTPSConnection(
                "127.0.0.1", self.port, timeout=REQUEST_TIMEOUT_S, context=ctx)
        return http.client.HTTPConnection(
            "127.0.0.1", self.port, timeout=REQUEST_TIMEOUT_S)

    def request(self, method: str, path: str, *, body=None, headers=None):
        """One request.  Returns (status, header-list, body-bytes).

        Headers come back as a list of pairs, not a dict: a response may repeat a
        header name, and which names repeat is part of what the migration must
        preserve.  Collapsing them here would delete that evidence before it could
        be compared.

        The path is sent as given, without normalisation.  Several cases in the
        corpus depend on that -- a request for ``//index.yaml`` or one carrying
        ``..`` is testing what the server does with it, and a client that tidied
        it up first would silently convert those into different tests.
        """
        conn = self.connection()
        try:
            conn.putrequest(method, path, skip_host=False, skip_accept_encoding=True)
            for k, v in (headers or []):
                conn.putheader(k, v)
            if body is not None:
                conn.putheader("Content-Length", str(len(body)))
            conn.endheaders(body if body is not None else None)
            resp = conn.getresponse()
            payload = resp.read()
            return resp.status, resp.getheaders(), payload
        finally:
            conn.close()

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)


#: The frozen self-signed pair the TLS profiles use.  Both sides are pointed at
#: the same files, which is why ``Profile.flags`` never names a certificate path:
#: a path in the corpus would have to resolve identically in the environment image
#: and the verifier image, and that is a coupling the corpus should not carry.
TLS_CERT = os.path.join(FIXTURES, "testdata/bearerauth/server.pem")
TLS_KEY = os.path.join(FIXTURES, "testdata/bearerauth/server.key")


def serve(binary: str, *, flags=(), seeds=(), rundir: str, tls: bool = False,
          env=None) -> Server:
    """Start ``binary`` with ``flags`` over freshly seeded storage; wait for ready.

    ``flags`` is the profile's flag list verbatim.  Everything this function adds
    is infrastructure the profile cannot express: the port (assigned, so launches
    never race), the storage backend and root (private per launch), and -- when
    ``tls`` is set -- the certificate pair, pointed at the frozen fixtures so the
    corpus never has to name a path.
    """
    port = free_port()
    storage = os.path.join(rundir, "storage")
    logpath = os.path.join(rundir, "server.log")
    os.makedirs(rundir, exist_ok=True)
    if os.path.exists(storage):
        shutil.rmtree(storage)
    seed_storage(storage, tuple(seeds))

    tls_flags: list[str] = []
    if tls:
        for path in (TLS_CERT, TLS_KEY):
            if not os.path.isfile(path):
                raise FileNotFoundError(f"TLS fixture missing: {path}")
        tls_flags = [f"--tls-cert={TLS_CERT}", f"--tls-key={TLS_KEY}"]

    argv = [
        binary,
        f"--port={port}",
        "--storage=local",
        f"--storage-local-rootdir={storage}",
        *tls_flags,
        *flags,
    ]
    scheme, health, _ = _probe_target(tuple(argv[1:]))

    if not os.access(binary, os.X_OK):
        raise LaunchError(f"not executable: {binary}", argv=argv, log="", rc=None)

    runenv = submission_env()
    # Nothing may reach the server through the environment.  ChartMuseum's flags
    # are all EnvVar-backed (DEPTH, CONTEXT_PATH, BASIC_AUTH_USER, ...), so a
    # stray variable in the harness's own environment would silently reconfigure
    # one side of a differential comparison.  The profile's flag list is the only
    # channel.
    for k in list(runenv):
        if k.startswith(("CM_", "STORAGE", "BASIC_AUTH", "BEARER", "DEPTH",
                         "CONTEXT_PATH", "CHART_", "INDEX_", "TLS_", "PORT",
                         "ALLOW_", "DISABLE_", "AUTH_", "LOG_", "DEBUG",
                         "ARTIFACT_HUB", "CACHE", "MAX_", "PER_CHART",
                         "WEB_TEMPLATE", "ENFORCE_", "ANONYMOUS_", "CORS_")):
            runenv.pop(k, None)
    runenv.update(env or {})

    with open(logpath, "wb") as logfh:
        proc = subprocess.Popen(
            argv, stdout=logfh, stderr=subprocess.STDOUT,
            cwd=rundir, env=runenv, start_new_session=True)

    server = Server(proc=proc, port=port, scheme=scheme, storage=storage,
                    argv=argv, logpath=logpath)

    deadline = time.monotonic() + BOOT_TIMEOUT_S
    while time.monotonic() < deadline:
        rc = proc.poll()
        if rc is not None:
            # Died during startup.  Its own output is the diagnosis.
            raise LaunchError("server exited during startup",
                              argv=argv, log=server.log, rc=rc)
        try:
            status, _, _ = server.request("GET", health)
            if status == 200:
                return server
        except (OSError, http.client.HTTPException, ssl.SSLError):
            pass
        time.sleep(POLL_INTERVAL_S)

    server.stop()
    raise LaunchError(
        f"server never answered {scheme}://127.0.0.1:{port}{health} "
        f"within {BOOT_TIMEOUT_S:g}s",
        argv=argv, log=server.log, rc=proc.poll())


@contextlib.contextmanager
def running(binary: str, **kw):
    server = serve(binary, **kw)
    try:
        yield server
    finally:
        server.stop()
