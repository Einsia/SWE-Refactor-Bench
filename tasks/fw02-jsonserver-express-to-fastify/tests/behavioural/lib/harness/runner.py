"""Booting a target and replaying a session against it.

Both the recording of State A and every graded run go through here, so the two
sides are driven identically: same launcher, same readiness rule, same request
construction, same teardown. If this file favoured one side the comparison would
mean nothing.

The launcher is always a shell script in the repository root -- ``serve.sh`` for
the submission, the oracle helper for State A. Nothing here knows how the server
is implemented, which is the point: the task may replace every line of JavaScript
as long as the entry point still starts a server.

Two inputs are handed *in* rather than read out of the tree under test: the seed
database and the rewrite rules. ``serve.sh`` takes both from the environment, so
supplying the grader's own copies costs nothing and buys two things -- a
submission cannot improve its score by editing the data it is queried about, and
a submission that legitimately reorganised its data files is not punished for it.
Whether those files are still present and unchanged in the delivered tree is a
question about the repository, and stage 1 is where questions about the
repository are asked.
"""

from __future__ import annotations

import http.client
import json as jsonlib
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path

from .normalize import record
from .sessions import SEED_VARIANTS, Case, Session

from swerefactor.contract import submission_env

READY_TIMEOUT = float(os.environ.get("SRB_READY_TIMEOUT", "90"))
REQUEST_TIMEOUT = float(os.environ.get("SRB_REQUEST_TIMEOUT", "30"))

#: Directories whose files are served as static assets, under both the default
#: document root and ``--static``.
STATIC_ROOTS = ("public", "altpublic")


def pin_static_mtimes(repo: Path, epoch: float,
                      roots: tuple[str, ...] = STATIC_ROOTS) -> list[str]:
    """Set every static file's mtime to the one the recording was made with.

    A static file's validators are derived from its size and its mtime, not from
    its contents: Express answers ``W/"<size-hex>-<mtime-hex>"`` and a
    ``Last-Modified`` built from the same stat. The frozen State A snapshot
    carries a fixed mtime for every file, so the recording holds one specific
    pair -- and a submission whose checkout happens to have restored those files
    with today's timestamp would be marked wrong for a fact about its filesystem.

    So the mtime is pinned, from the ``Last-Modified`` the recording itself holds,
    and the size is left alone. That keeps the conditional-request cases
    reproducible while still failing a submission that changed a static file: the
    size moves, the tag moves with it, and the body differs anyway.

    Returns the paths it touched, for the report.
    """
    touched: list[str] = []
    for root in roots:
        base = repo / root
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_file():
                try:
                    os.utime(path, (epoch, epoch))
                except OSError:
                    continue
                touched.append(str(path.relative_to(repo)))
    return touched


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ServerFailed(RuntimeError):
    """The target did not become ready. Carries the log for diagnosis."""

    def __init__(self, message: str, log: str):
        super().__init__(message + ("\n--- server log ---\n" + log if log else ""))
        self.log = log


class Target:
    """One booted server, for the lifetime of one session."""

    def __init__(self, repo: Path, launcher: str, session: Session,
                 workdir: Path, seed_source: Path, routes: Path | None = None):
        self.repo = Path(repo)
        self.launcher = launcher
        self.session = session
        self.workdir = Path(workdir)
        self.seed_source = Path(seed_source)
        #: The rewrite rules to serve. ``None`` means the tree's own, which is
        #: what the recording of State A used before the file was shipped as an
        #: input; every graded run passes the grader's copy explicitly.
        self.routes = Path(routes) if routes is not None else self.repo / "routes.json"
        self.port = free_port()
        self.proc: subprocess.Popen | None = None
        self.logfile: Path | None = None
        self.seed_path: Path | None = None
        self.db_path: Path | None = None
        # Absolute directories belonging to *this* target. Two responses carry a
        # stack trace, and a stack trace names the file that raised it, so
        # without scrubbing these the comparison would be between State A's
        # checkout path and the submission's. Both the logical and the resolved
        # form are scrubbed: Node prints whichever the loader saw.
        prefixes = set()
        for base in (self.repo, self.workdir):
            prefixes.add(str(base.absolute()))
            try:
                prefixes.add(str(base.resolve()))
            except OSError:
                pass
        self.scrub_prefixes = tuple(sorted(prefixes, key=len, reverse=True))

    # -- lifecycle ---------------------------------------------------------
    def _prepare_seed(self) -> Path:
        """Write the seed this session boots from, applying any variant.

        The launcher copies the seed to the working database itself, so these
        must be two distinct paths; the seed stays pristine and every boot gets
        a fresh working copy.
        """
        db = jsonlib.loads(self.seed_source.read_text())
        if self.session.seed:
            transform = SEED_VARIANTS[self.session.seed]
            db = transform(db)
        path = self.workdir / f"seed-{self.session.id}-{self.port}.json"
        path.write_text(jsonlib.dumps(db, indent=2))
        return path

    def start(self) -> None:
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.seed_path = self._prepare_seed()
        self.db_path = self.workdir / f"db-{self.session.id}-{self.port}.json"
        self.logfile = self.workdir / f"server-{self.session.id}-{self.port}.log"

        env = submission_env()
        env.update({
            "JSON_SERVER_SEED": str(self.seed_path),
            "JSON_SERVER_DB": str(self.db_path),
            "JSON_SERVER_PORT": str(self.port),
            "JSON_SERVER_HOST": "127.0.0.1",
            "JSON_SERVER_ROUTES": str(self.routes),
            # Never let a submission's own NODE_ENV leak in and change behaviour.
            "NODE_ENV": "",
        })
        for key, value in self.session.env:
            env[key] = value

        launcher = self.repo / self.launcher
        if not launcher.exists():
            raise ServerFailed(f"launcher {launcher} does not exist", "")

        with open(self.logfile, "wb") as log:
            self.proc = subprocess.Popen(
                [str(launcher), *self.session.argv],
                cwd=str(self.repo),
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        self._await_ready()

    def _await_ready(self) -> None:
        deadline = time.monotonic() + READY_TIMEOUT
        last = ""
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise ServerFailed(
                    f"launcher exited with {self.proc.returncode} before "
                    f"becoming ready", self._log())
            try:
                conn = http.client.HTTPConnection("127.0.0.1", self.port,
                                                  timeout=5)
                conn.request("GET", "/posts")
                resp = conn.getresponse()
                resp.read()
                conn.close()
                if resp.status < 500:
                    return
                last = f"GET /posts -> {resp.status}"
            except OSError as exc:
                last = str(exc)
            time.sleep(0.25)
        raise ServerFailed(
            f"server on port {self.port} not ready after {READY_TIMEOUT}s "
            f"(last: {last})", self._log())

    def _log(self) -> str:
        if self.logfile and self.logfile.exists():
            return self.logfile.read_text(errors="replace")[-8000:]
        return ""

    def stop(self) -> None:
        if self.proc is None:
            return
        try:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            try:
                self.proc.terminate()
            except ProcessLookupError:
                pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                self.proc.kill()
            self.proc.wait(timeout=10)
        self.proc = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()
        return False

    # -- requests ----------------------------------------------------------
    def request(self, case: Case) -> dict:
        body: bytes | None = None
        headers = {"Host": f"127.0.0.1:{self.port}"}
        for name, value in case.headers:
            headers[name] = value
        if case.json is not None:
            body = jsonlib.dumps(case.json).encode()
            headers.setdefault("Content-Type", "application/json")
        elif case.data is not None:
            body = case.data

        conn = http.client.HTTPConnection("127.0.0.1", self.port,
                                          timeout=REQUEST_TIMEOUT)
        started = time.monotonic()
        try:
            conn.request(case.method, case.path, body=body, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            raw_headers = list(resp.getheaders())
            status = resp.status
        finally:
            conn.close()
        elapsed_ms = (time.monotonic() - started) * 1000.0

        entry = record(case.id, status, raw_headers, raw, case.decompress,
                       prefixes=self.scrub_prefixes)
        # Wall clock is not comparable between runs, but `--delay` is a real
        # observable, so the measurement is carried for the tests that want a
        # lower bound rather than an equality.
        entry["elapsed_ms"] = round(elapsed_ms, 1)
        entry["note"] = case.note
        entry["body_mode"] = case.body_mode
        entry["headers_extra"] = list(case.headers_extra)
        entry["headers_skip"] = list(case.headers_skip)
        return entry


def play_session(repo: Path, launcher: str, session: Session, workdir: Path,
                 seed_source: Path, routes: Path | None = None) -> dict[str, dict]:
    """Boot a server, replay one session in order, return its responses."""
    out: dict[str, dict] = {}
    with Target(repo, launcher, session, workdir, seed_source, routes) as target:
        for case in session.cases:
            out[case.id] = target.request(case)
        # A final free-form read confirms the server survived the session
        # rather than dying on the last mutating request.
        out["__alive__"] = target.request(Case(id="__alive__", path="/posts/1"))
    return out


def play_all(repo: Path, launcher: str, sessions, workdir: Path,
             seed_source: Path, routes: Path | None = None,
             on_session=None) -> dict[str, dict[str, dict]]:
    result: dict[str, dict[str, dict]] = {}
    for session in sessions:
        if on_session:
            on_session(session)
        result[session.id] = play_session(repo, launcher, session, workdir,
                                          seed_source, routes)
    return result
