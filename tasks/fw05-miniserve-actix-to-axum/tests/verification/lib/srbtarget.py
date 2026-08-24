"""Booting the target and asking it questions, for an verification candidate.

A candidate imports this, boots the target with whatever command line it wants,
and asks it whatever it wants.  It never learns which of the two trees it is
talking to, and the two trees are driven through exactly this file, so a
difference a candidate observes is a difference between the *artifacts* rather
than between two ways of starting them.

Why a launcher at all, when every other task in this benchmark hands its
candidate a base URL and stops
---------------------------------------------------------------------------
Because miniserve has no single configuration.  Its whole surface is flags:
whether uploads exist, whether an archive can be requested, whether the listing
shows hidden files, whether the thing is even HTTP rather than HTTPS.  The
recorded corpus stage 2 grades against needs 57 distinct command lines to reach
612 cases, and no one process can be more than one of them -- ``--tls-cert``
makes the port HTTPS and ``--index`` changes what ``/`` is.  A pre-booted server
would hand a candidate about a fifth of the migration and silently declare the
rest out of reach, and the parts out of reach would be the interesting ones: the
static asset routes, the multipart parser, the auth extractor, the TLS
acceptor -- everything the retired framework used to supply.

So the input format here is stage 2's input format, which is what the task's
verification contract asks for: a command line, then a sequence of requests
against the server it produced.  :func:`serve` is stage 2's ``Target.start``
with the session replaced by the candidate's own argument list, and
:func:`run_cli` is stage 2's ``harness.cli.run``.  Where behaviour is shared with
stage 2 it is shared deliberately, so that a candidate's finding is expressible
in the same terms as a corpus case and can be checked against the recording.

What is deliberately not here
-----------------------------
No assertion helpers, no response normalisation, no comparison of any kind.  A
candidate asserts against what it believes the *original* does, and its being
right about that is what the adjudicator checks by running it on both trees.  A
helper that said "these two responses match" would need both trees at once, and
a candidate that can see both trees at once is not measuring anything.

Nothing here scrubs paths, either.  Stage 2 scrubs because it compares text
recorded in two different containers; a candidate runs in one container and can
read ``t.root`` if it needs the path a message will contain.
"""

from __future__ import annotations

import contextlib
import http.client
import os
import shutil
import signal
import socket
import ssl
import subprocess
import time
from pathlib import Path

__all__ = ["serve", "run_cli", "Server", "CliResult", "TLS_DIR", "AUTH_FILE",
           "TREE", "FIXED_MTIME", "build_tree"]

#: The binary under test.  Set by run-candidate.sh, which is the only thing that
#: knows which tree it belongs to.
#:
#: A candidate that reads this, or opens it, or runs `strings` on it, has stopped
#: testing behaviour and started identifying its opponent -- which probe.toml's
#: scope rejects by name, and which is visible in the candidate's own source to
#: the adjudicator that reads it.  It is exported as a private detail rather than
#: hidden because hiding it from a test running in the same process is not
#: something a library can do; the boundary here is the contract, not a sandbox.
_BINARY = os.environ.get("SRB_TARGET_BINARY", "")

#: TLS material and the credentials file, shipped by the image.  The same files
#: stage 2 uses, so a candidate can reuse a corpus command line verbatim.  Both
#: trees see the same ones: a submission cannot choose what its TLS sessions are
#: measured against, and neither can a candidate.
TLS_DIR = Path(os.environ.get("SRB_TLS_DIR", "/opt/srb/tls"))
AUTH_FILE = Path(os.environ.get("SRB_AUTH_FILE", "/opt/srb/auth/auth-file.txt"))

#: A read-only reference copy of the sample tree, for computing expectations
#: from.  The tree a server actually serves is a fresh private copy, so an
#: upload in one candidate cannot be seen by the next; this copy is what to read
#: when a test needs to know how big a file is or what a directory contains.
TREE = Path(os.environ.get("SRB_TREE_REFERENCE", "/opt/srb/tree-reference"))

#: The spec both images materialise the tree from.
TREE_SPEC = Path(os.environ.get("SRB_TREE_SPEC", "/opt/srb/tree-spec.json"))

#: 2021-06-15T12:34:56Z, on every entry of every copy.  The listing renders
#: mtimes, so a tree whose timestamps drifted would make the listing untestable;
#: this is the value stage 2 pins and the value ``srb-sample-tree`` writes.
FIXED_MTIME = 1623760496

READY_TIMEOUT = float(os.environ.get("SRB_READY_TIMEOUT", "60"))
REQUEST_TIMEOUT = float(os.environ.get("SRB_REQUEST_TIMEOUT", "30"))

_WORK = Path(os.environ.get("SRB_TMP", "/tmp/srb-candidate"))


class TargetFailed(RuntimeError):
    """The target would not start.  Carries its log, which is the diagnosis.

    Raised rather than returned because a candidate that cannot boot the target
    has not found anything: it fails, and a candidate that fails on the original
    is discarded before its behaviour on the submission is even looked at.  If
    the *submission* is what would not boot, that is stage 2's finding and it has
    already been made -- stage 3 is not reached unless stage 2 passed every check.
    """

    def __init__(self, message: str, log: str = ""):
        super().__init__(message + (f"\n--- target log ---\n{log}" if log else ""))
        self.log = log


def build_tree(root: Path) -> Path:
    """Materialise a fresh sample tree at ``root``, with pinned mtimes.

    Called for you by :func:`serve`.  Exposed because a candidate testing upload
    or mkdir may want a second tree of its own, and because a candidate that
    wants to serve a *different* tree -- an empty directory, a single file, a
    directory it has no permission to read -- should build it explicitly rather
    than mutating the one being served.
    """
    root = Path(root)
    if root.exists():
        shutil.rmtree(root)
    root.parent.mkdir(parents=True, exist_ok=True)
    # cp -a from the reference rather than replaying the spec: the reference was
    # built from the spec at image build time and verified there, so this copy
    # cannot disagree with what stage 2 serves, and copying is much faster than
    # rebuilding 60-odd entries per candidate run.
    shutil.copytree(TREE, root, symlinks=True)
    _pin_times(root)
    return root


def _pin_times(root: Path) -> None:
    """Reset every mtime under ``root``, deepest first.

    Deepest first because writing a child updates its parent's mtime, so a
    shallow-first pass would undo itself.  ``follow_symlinks=False`` because a
    dangling symlink is one of the entries in the tree and following it raises.
    """
    paths = sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True)
    for path in [*paths, root]:
        with contextlib.suppress(OSError):
            os.utime(path, (FIXED_MTIME, FIXED_MTIME), follow_symlinks=False)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _clean_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """The environment both trees are started with.

    Every ``MINISERVE_*`` variable is a CLI alias, so one inherited from the
    container is an argument nobody passed -- and one inherited from *this stage*
    would apply to both trees and quietly change what is being compared.  Deleted
    rather than emptied: clap runs an env value through the flag's value parser,
    and ``MINISERVE_HIDDEN=""`` is not boolish, so blanking it makes the process
    exit with a usage error instead of ignoring it.

    ``OVERWRITE_FILES`` is the one alias without the prefix, which is a quirk of
    the original's own ``args.rs`` and is part of what a port has to preserve.

    The three ``SRB_TARGET*`` variables are removed for a different reason: they
    are the harness's, and one of them names the role.
    """
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("MINISERVE_")}
    for key in ("OVERWRITE_FILES", "SRB_TARGET_ROLE", "SRB_TARGET_NAME",
                "SRB_TARGET", "SRB_ORIGINAL"):
        env.pop(key, None)
    env.update({"TZ": "UTC", "NO_COLOR": "1", "TERM": "dumb",
                "CLICOLOR": "0", "COLUMNS": "100"})
    if extra:
        env.update(extra)
    return env


def _binary() -> str:
    if not _BINARY:
        raise TargetFailed("SRB_TARGET_BINARY is not set: this module has to be "
                           "run through run-candidate.sh, which builds the tree "
                           "and decides which binary is under test")
    return _BINARY


class Server:
    """A booted target: a base URL and the ways of asking it something.

    Obtain one from :func:`serve`, which is a context manager.  Reachable state
    worth knowing about:

    ``base_url``  ``http://127.0.0.1:PORT`` or ``https://...`` under TLS.
    ``port``      the port, for a client of your own.
    ``root``      the directory being served -- this run's private copy.
    ``argv``      the full command line, after placeholder expansion.
    ``log``       what the process has written to stdout and stderr so far.
    """

    def __init__(self, proc: subprocess.Popen, port: int, root: Path,
                 argv: list[str], logfile: Path, scheme: str):
        self.proc = proc
        self.port = port
        self.root = root
        self.argv = argv
        self.scheme = scheme
        self._logfile = logfile

    @property
    def base_url(self) -> str:
        return f"{self.scheme}://127.0.0.1:{self.port}"

    @property
    def log(self) -> str:
        """Whatever the process has written so far.

        The startup banner is on stdout and is part of the observable surface;
        a panic message is on stderr and is how you tell a 500 apart from a
        process that died.  Both land here, interleaved, because that is what a
        terminal would have shown.
        """
        if self._logfile.exists():
            return self._logfile.read_text(errors="replace")
        return ""

    # -- asking it things --------------------------------------------------
    def connect(self, timeout: float | None = None):
        """A fresh ``HTTPConnection``/``HTTPSConnection``.

        Fresh per call, and worth keeping that way: several things this task
        grades are per-connection.  TLS verification is off because the shipped
        certificate is self-signed for 127.0.0.1 -- the question is whether the
        port speaks TLS at all and serves the same bytes, not whether a test
        container trusts a test CA.
        """
        timeout = REQUEST_TIMEOUT if timeout is None else timeout
        if self.scheme == "https":
            ctx = ssl._create_unverified_context()               # noqa: SLF001
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return http.client.HTTPSConnection("127.0.0.1", self.port,
                                               timeout=timeout, context=ctx)
        return http.client.HTTPConnection("127.0.0.1", self.port,
                                          timeout=timeout)

    def request(self, method: str = "GET", path: str = "/", *,
                headers: dict[str, str] | None = None,
                body: bytes | None = None,
                timeout: float | None = None) -> "Response":
        """One request, one response, connection closed.

        ``path`` is sent as given.  It is not quoted, joined or normalised, and
        that is the point: ``%2F`` inside a segment, a bare ``..``, a literal
        space and a doubled slash all have to survive this function to be worth
        testing, and every HTTP client library in the index would fix at least
        one of them for you.  ``http.client`` puts the request line on the wire
        as handed over.

        For a request too malformed for ``http.client`` -- no version, a bad
        method token, absent framing -- use :meth:`raw`.
        """
        conn = self.connect(timeout)
        try:
            hdrs = {"Connection": "close"}
            hdrs.update(headers or {})
            conn.request(method, path, body=body, headers=hdrs)
            resp = conn.getresponse()
            payload = resp.read()
            return Response(status=resp.status, reason=resp.reason,
                            headers=list(resp.getheaders()), body=payload)
        finally:
            with contextlib.suppress(Exception):
                conn.close()

    def raw(self, data: bytes, *, timeout: float | None = None,
            read_bytes: int = 1 << 20) -> bytes:
        """Write bytes to the port and read what comes back.

        The escape hatch for anything ``http.client`` will not express: an
        invalid request line, a duplicated Content-Length, a header with a
        literal newline in it, a body that stops early.  Returns the raw
        response including its status line and headers -- parse it yourself, or
        assert on the prefix.

        An empty return means the target closed the connection without writing
        anything, which is itself an answer and a different one from a 400.
        """
        timeout = REQUEST_TIMEOUT if timeout is None else timeout
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=timeout)
        try:
            if self.scheme == "https":
                ctx = ssl._create_unverified_context()           # noqa: SLF001
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                sock = ctx.wrap_socket(sock, server_hostname="127.0.0.1")
            sock.sendall(data)
            chunks = []
            got = 0
            while got < read_bytes:
                try:
                    chunk = sock.recv(65536)
                except (TimeoutError, socket.timeout, ssl.SSLError, OSError):
                    break
                if not chunk:
                    break
                chunks.append(chunk)
                got += len(chunk)
            return b"".join(chunks)
        finally:
            with contextlib.suppress(Exception):
                sock.close()

    # -- lifecycle ---------------------------------------------------------
    def stop(self) -> None:
        """Terminate the process group, then insist.

        The group and not the process: the original spawns a thread per archive
        and may spawn helpers, and an orphan holding the port would surface as
        the *next* candidate mysteriously failing to boot.
        """
        if self.proc is None:
            return
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.proc.wait(timeout=10)
        self.proc = None


class Response:
    """What came back.  Headers are kept as a list, in order, with duplicates.

    A dict would lose both, and both are things this task grades: a listing
    response carries more than one ``Set-Cookie``-shaped header only if the port
    invented one, and ``--header`` given twice is a documented flag whose whole
    contract is that both values arrive.  :meth:`get` is there for the common
    case of wanting one value.
    """

    def __init__(self, status: int, reason: str,
                 headers: list[tuple[str, str]], body: bytes):
        self.status = status
        self.reason = reason
        self.headers = headers
        self.body = body

    def get(self, name: str, default: str | None = None) -> str | None:
        """The first value of ``name``, case-insensitively.

        Case-insensitively because header *name* casing is out of scope: a port
        that emits ``etag`` where the original emitted ``ETag`` has not changed
        anything a client can observe, HTTP/1.1 names being case-insensitive by
        specification.  Whether the *value* differs is very much in scope.
        """
        low = name.lower()
        for key, value in self.headers:
            if key.lower() == low:
                return value
        return default

    def all(self, name: str) -> list[str]:
        """Every value of ``name``, in the order received."""
        low = name.lower()
        return [v for k, v in self.headers if k.lower() == low]

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def __repr__(self) -> str:
        # Shown in a pytest failure, so it carries what identifies the response
        # without dumping a 40 KiB listing into the report.
        return (f"<Response {self.status} "
                f"{self.get('content-type', '-')!r} {len(self.body)}B>")


class CliResult:
    """The observable result of one invocation: status, stdout, stderr.

    The two streams are kept apart because which one a message lands on is part
    of the contract rather than a detail: ``miniserve --print-completions bash >
    _completions`` has to produce a usable file, so a port that writes the
    completion script to stderr, or the usage error to stdout, has broken a
    pipeline that worked before.
    """

    def __init__(self, argv: list[str], exit_code: int | None,
                 stdout: bytes, stderr: bytes, timed_out: bool = False):
        self.argv = argv
        self.exit_code = exit_code
        self.stdout_bytes = stdout
        self.stderr_bytes = stderr
        self.timed_out = timed_out

    @property
    def stdout(self) -> str:
        return self.stdout_bytes.decode("utf-8", "replace")

    @property
    def stderr(self) -> str:
        return self.stderr_bytes.decode("utf-8", "replace")

    @property
    def output(self) -> str:
        """Both streams, for a test that only cares that something was said."""
        return self.stdout + self.stderr

    def __repr__(self) -> str:
        return (f"<CliResult exit={self.exit_code} "
                f"timed_out={self.timed_out} "
                f"out={len(self.stdout_bytes)}B err={len(self.stderr_bytes)}B>")


def run_cli(argv: list[str] | tuple[str, ...], *,
            env: dict[str, str] | None = None,
            stdin: bytes | None = None,
            cwd: Path | None = None,
            timeout: float = 30.0) -> CliResult:
    """Run the target once, to completion, and collect what it said.

    For the invocations that are not a server: ``--help``, ``--version``,
    ``--print-completions``, ``--print-manpage``, and every way of getting the
    argument parser to refuse something.  Placeholders are expanded as in
    :func:`serve`, so ``--tls-cert {tls}/cert_rsa.pem`` works here too.

    ``env`` is *added* to a scrubbed environment, which is how to test the
    ``MINISERVE_*`` aliases: pass ``{"MINISERVE_HIDDEN": "true"}`` and it is the
    only one set.  Note the invocation must terminate on its own -- an argv that
    starts a server will hit ``timeout`` and come back with ``timed_out``.
    """
    argv = [_expand(str(a)) for a in argv]
    workdir = Path(cwd) if cwd else _WORK
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run([_binary(), *argv], cwd=str(workdir),
                              env=_clean_env(env), input=stdin,
                              timeout=timeout,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except subprocess.TimeoutExpired as exc:
        return CliResult(argv, None, exc.stdout or b"", exc.stderr or b"",
                         timed_out=True)
    return CliResult(argv, proc.returncode, proc.stdout, proc.stderr)


def _expand(item: str, tree: Path | None = None) -> str:
    """Substitute the image-owned placeholders in one argv element.

    The same three stage 2 expands, for the same reason: a candidate cannot name
    an absolute path that will exist, because the tree is materialised per run
    under a directory named after an opaque token.  ``{tls}``, ``{authfile}``
    and ``{tree}``.
    """
    out = item.replace("{tls}", str(TLS_DIR)).replace("{authfile}", str(AUTH_FILE))
    if tree is not None:
        out = out.replace("{tree}", str(tree))
    return out


@contextlib.contextmanager
def serve(*argv: str, serve_path: Path | str | None = None,
          env: dict[str, str] | None = None,
          tree: bool = True,
          ready: bool = True,
          scheme: str | None = None):
    """Boot the target and yield a :class:`Server`; stop it on the way out.

        with srbtarget.serve("--enable-tar", "--hidden") as t:
            r = t.request("GET", "/?download=tar")
            assert r.status == 200

    The command line is built the way stage 2 builds it, and this order matters
    for reproducing a corpus case::

        <binary> <serve-path> --interfaces 127.0.0.1 --port <port> <your argv>

    The serve path is positional and first; the interface and port are fixed
    here so a candidate cannot bind something public or collide with another
    candidate.  Everything after is yours, expanded for ``{tls}``,
    ``{authfile}`` and ``{tree}``.

    ``tree=False`` serves an empty directory instead of the sample tree, and
    ``serve_path=`` serves a path you built yourself -- for the cases where the
    interesting thing is the tree: an empty directory, a single file as the root,
    a directory with no read permission, a path that does not exist.

    ``scheme`` is inferred: passing ``--tls-cert`` makes it ``https``.  Override
    it to test a mismatch -- that plaintext HTTP to a TLS port is refused rather
    than answered, say.

    Always use it as a context manager.  ``stop()`` on the way out is not
    housekeeping: a leaked process keeps its port, and the failure lands on some
    later candidate, in a different round, as an unexplained boot timeout.
    """
    port = _free_port()
    run_dir = _WORK / f"boot-{port}"
    run_dir.mkdir(parents=True, exist_ok=True)

    if serve_path is not None:
        root = Path(serve_path)
    elif tree:
        root = build_tree(run_dir / "tree")
    else:
        root = run_dir / "empty"
        root.mkdir(parents=True, exist_ok=True)
        os.utime(root, (FIXED_MTIME, FIXED_MTIME))

    expanded = [_expand(str(a), root) for a in argv]
    full = [_binary(), str(root), "--interfaces", "127.0.0.1",
            "--port", str(port), *expanded]

    if scheme is None:
        scheme = "https" if any(a.startswith("--tls-cert") for a in expanded) \
            else "http"

    logfile = run_dir / "target.log"
    with open(logfile, "wb") as sink:
        proc = subprocess.Popen(full, cwd=str(run_dir), env=_clean_env(env),
                                stdout=sink, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL,
                                start_new_session=True)

    server = Server(proc, port, root, full, logfile, scheme)
    try:
        if ready:
            _await_ready(server)
        yield server
    finally:
        server.stop()
        # The tree is this boot's.  Removed here rather than at the next boot so
        # that a round's candidates do not each leave a copy behind;
        # a candidate that wants to inspect the served tree after the fact should
        # pass its own serve_path.
        if serve_path is None:
            shutil.rmtree(run_dir, ignore_errors=True)


def _await_ready(server: Server) -> None:
    """Wait until the port answers HTTP at all.

    Any status counts, 401 and 403 included.  Several configurations are supposed
    to refuse ``/`` -- ``--auth``, ``--disable-indexing``, ``--random-route``,
    ``--route-prefix`` -- and requiring a 2xx here would make exactly those
    unbootable, which is to say it would make the auth and routing surface
    untestable.  What is waited for is a listener that speaks the protocol.

    A process that has already exited is not going to start answering, so its
    status is reported immediately rather than after the full timeout: an
    invalid command line is a legitimate thing for a candidate to try, and it
    should come back in milliseconds with the log in the message.
    """
    deadline = time.monotonic() + READY_TIMEOUT
    last = ""
    while time.monotonic() < deadline:
        if server.proc.poll() is not None:
            raise TargetFailed(
                f"the target exited with {server.proc.returncode} before "
                f"answering on port {server.port}; argv was {server.argv[1:]}",
                server.log)
        try:
            conn = server.connect(timeout=5)
            conn.request("GET", "/", headers={"Connection": "close"})
            resp = conn.getresponse()
            resp.read()
            conn.close()
            return
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(0.15)
    raise TargetFailed(
        f"the target did not answer on port {server.port} within "
        f"{READY_TIMEOUT:.0f}s (last: {last}); argv was {server.argv[1:]}",
        server.log)
