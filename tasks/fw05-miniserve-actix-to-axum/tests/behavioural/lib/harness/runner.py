"""Booting a target and replaying a session against it.

Both the golden capture and the graded run go through here, so State A and State
B are driven identically: same binary invocation, same sample tree, same
readiness rule, same request construction, same teardown. If this file favoured
one side the whole comparison would be meaningless.

The target is always a **binary**: ``target/release/miniserve`` in the
submission, or the read-only oracle in the agent image. Nothing here knows how
it is implemented, which is the point -- the task may replace every line of Rust
as long as the same command line still starts the same server.

Two things this file owns rather than the corpus:

*   **The sample tree.** Materialised from the shipped spec with pinned mtimes,
    freshly for every mutating session and once per tree kind for the read-only
    ones. A session that uploads must not see another session's uploads.
*   **The scrub prefixes.** The tree lives under a temporary directory whose name
    differs between the capture and the graded run, and several responses embed
    it (a startup error names the serve path; an upload conflict names the file).
    Both the logical and the resolved form are scrubbed, because Rust's
    ``canonicalize`` prints the resolved one and the CLI echoes the logical one.
"""

from __future__ import annotations

import http.client
import os
import shutil
import signal
import socket
import ssl
import subprocess
import time
from dataclasses import replace
from pathlib import Path

from .tree import FIXED_MTIME, tree_digest
from .normalize import ARCHIVE_KIND_BY_QUERY, record
from .sessions import Case, Session, request_body

from swerefactor.contract import submission_env

READY_TIMEOUT = float(os.environ.get("SRB_READY_TIMEOUT", "90"))
REQUEST_TIMEOUT = float(os.environ.get("SRB_REQUEST_TIMEOUT", "60"))

#: Where the sample tree spec lives in both images.
TREE_SPEC = Path(os.environ.get("SRB_TREE_SPEC",
                                   "/opt/srb/tree-spec.json"))
#: The TLS material. Shipped by the image, not read out of the submission, so a
#: submission cannot change what the TLS sessions are measured against.
TLS_DIR = Path(os.environ.get("SRB_TLS_DIR", "/opt/srb/tls"))
AUTH_FILE = Path(os.environ.get("SRB_AUTH_FILE", "/opt/srb/auth/auth-file.txt"))


def _route_shape(route: str | None) -> str | None:
    """Describe a nonce route without recording it.

    ``/a1b2c3d4e5`` -> ``/<10 hex>``; ``/prefix/a1b2c3d4e5`` -> ``/prefix/<10
    hex>``. The nanoid alphabet is ``0-9a-f``, so a route of a different length,
    a different alphabet or a different depth is a different shape and shows up
    as a diff.
    """
    if route is None:
        return None
    head, _, tail = route.rpartition("/")
    alphabet = set("0123456789abcdef")
    kind = f"<{len(tail)} hex>" if tail and set(tail) <= alphabet \
        else f"<{len(tail)} other>"
    return f"{head}/{kind}"


class ServerFailed(RuntimeError):
    """The target did not become ready. Carries the log for diagnosis."""

    def __init__(self, message: str, log: str):
        super().__init__(message + ("\n--- server log ---\n" + log if log else ""))
        self.log = log


def build_tree(root: Path, spec: Path = TREE_SPEC, *,
               without: tuple[str, ...] = ()) -> Path:
    """Materialise the sample tree with pinned mtimes.

    Imported rather than shelled out to, so a broken ``srb-sample-tree`` on
    ``PATH`` cannot silently change what is being served.
    """
    import json

    from .tree import build

    doc = json.loads(spec.read_text(encoding="utf-8"))
    if without:
        doc = {**doc, "entries": [entry for entry in doc["entries"]
                                  if entry["path"] not in without]}
    build(doc, root, clean=True)
    return root


def prepare_tree(kind: str, workdir: Path, spec: Path = TREE_SPEC) -> Path:
    """The path handed to miniserve for a session of the given tree kind.

    ``root``    -- the spec tree itself.
    ``file``    -- a single file. The baseline routes this through a completely
                   different handler (``web::resource(["", "/"])``), so it is its
                   own tree kind rather than a case inside ``root``.
    ``symlink`` -- a symlink *to* the tree, which is what ``--no-symlinks``
                   refuses to start on.
    ``empty``   -- an empty directory: the listing's degenerate case.
    ``archivable``
                -- the tree without the dangling symlink, for the sessions that
                   download it as an archive: ``tar::Builder::append_dir_all``
                   follows symlinks and stops at the first one it cannot stat,
                   which makes the archive a function of the order the
                   filesystem returned the entries in rather than of the tree.

    ``given``   -- serve a directory the caller built and passed to ``Target``
                   as ``serve_path``. No corpus session uses this: it exists for
                   the audit suite's nonce probe, which invents its tree at
                   run time and therefore cannot describe it in a spec. Reached
                   only through the ``serve_path`` override, so ``prepare_tree``
                   never sees it.
    """
    base = workdir / f"tree-{kind}"
    if kind == "root":
        build_tree(base, spec)
        return base
    if kind == "file":
        build_tree(base, spec)
        return base / "test.txt"
    if kind == "symlink":
        build_tree(base, spec)
        link = workdir / f"link-{kind}"
        if link.is_symlink() or link.exists():
            link.unlink()
        os.symlink(base, link)
        # Pinned like every other node in the sample tree. os.symlink stamps the link
        # itself with the wall clock, and the tree digest reads it with lstat, so
        # leaving it makes this one session non-deterministic for a reason that
        # has nothing to do with what is being served.
        os.utime(link, (FIXED_MTIME, FIXED_MTIME), follow_symlinks=False)
        return link
    if kind == "archivable":
        return build_tree(base / "tree-root", spec, without=("links/dangling",))
    if kind == "empty":
        if base.exists():
            shutil.rmtree(base)
        base.mkdir(parents=True)
        os.utime(base, (FIXED_MTIME, FIXED_MTIME))
        return base
    raise ValueError(f"unknown tree kind {kind!r}")


class Target:
    """One booted miniserve process, for the lifetime of one session."""

    def __init__(self, repo: Path, binary: str, session: Session,
                 workdir: Path, spec: Path = TREE_SPEC,
                 serve_path: Path | None = None):
        self.repo = Path(repo)
        self.binary = (self.repo / binary) if not Path(binary).is_absolute() \
            else Path(binary)
        self.session = session
        self.workdir = Path(workdir)
        self.spec = Path(spec)
        #: Serve this path instead of materialising one from the spec. Used only
        #: by the audit suite's nonce probe, whose tree is invented at run
        #: time; every corpus session leaves it None and gets prepare_tree.
        self.given_path = Path(serve_path) if serve_path else None
        self.port = session.port
        self.proc: subprocess.Popen | None = None
        self.logfile: Path | None = None
        self.serve_path: Path | None = None
        self.random_route: str | None = None
        #: case id -> raw response headers, for conditional requests.
        self.raw_headers: dict[str, dict[str, str]] = {}

    def _prefixes(self) -> tuple[str, ...]:
        prefixes = set()
        for base in (self.repo, self.workdir, self.serve_path):
            if base is None:
                continue
            prefixes.add(str(Path(base).absolute()))
            try:
                prefixes.add(str(Path(base).resolve()))
            except OSError:
                pass
        return tuple(sorted(prefixes, key=len, reverse=True))

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.serve_path = self.given_path or prepare_tree(
            self.session.tree, self.workdir, self.spec)
        self.logfile = self.workdir / f"server-{self.session.id}.log"

        if not self.binary.exists():
            raise ServerFailed(f"{self.binary} does not exist -- the "
                               "submission produced no release binary", "")

        argv = [str(self.binary), str(self.serve_path),
                "--interfaces", "127.0.0.1", "--port", str(self.port)]
        argv.extend(self._expand_argv(self.session.argv))

        env = submission_env()
        env.update({"TZ": "UTC", "NO_COLOR": "1", "TERM": "dumb"})
        # Every MINISERVE_* variable is a CLI alias, so one inherited from the
        # container would change what is measured. Removed rather than emptied:
        # clap parses an env value through the same value parser as the flag, and
        # an empty string is not boolish, so ``MINISERVE_HIDDEN=""`` would make
        # the process exit with a usage error instead of ignoring the variable.
        for key in list(env):
            if key.startswith("MINISERVE_"):
                del env[key]
        env.pop("OVERWRITE_FILES", None)
        for key, value in self.session.env:
            env[key] = value

        with open(self.logfile, "wb") as log:
            self.proc = subprocess.Popen(
                argv,
                cwd=str(self.workdir),
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        self._await_ready()

    def _expand_argv(self, argv: tuple[str, ...]) -> list[str]:
        """Substitute the harness-owned placeholders in a session's command line.

        ``{tls}/x`` is the shipped certificate directory, ``{authfile}`` the
        shipped credentials file, and ``{tree}`` the materialised serve path.
        Sessions cannot name absolute paths themselves, because those differ
        between the capture and the graded run.
        """
        out = []
        for item in argv:
            out.append(item
                       .replace("{tls}", str(TLS_DIR))
                       .replace("{authfile}", str(AUTH_FILE))
                       .replace("{tree}", str(self.serve_path)))
        return out

    def _connect(self, timeout: float):
        if self.session.scheme == "https":
            ctx = ssl._create_unverified_context()               # noqa: SLF001
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return http.client.HTTPSConnection("127.0.0.1", self.port,
                                               timeout=timeout, context=ctx)
        return http.client.HTTPConnection("127.0.0.1", self.port,
                                          timeout=timeout)

    def _await_ready(self) -> None:
        """Wait until the server answers anything at all.

        Any status counts, including 401 and 403: several sessions are configured
        so that ``/`` is *supposed* to be refused (``--auth``,
        ``--disable-indexing``, ``--random-route``), and requiring a 2xx here
        would make those sessions unbootable. What is being waited for is a
        listener that speaks HTTP, not a particular answer.
        """
        deadline = time.monotonic() + READY_TIMEOUT
        last = ""
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise ServerFailed(
                    f"miniserve exited with {self.proc.returncode} before "
                    "becoming ready", self._log())
            try:
                conn = self._connect(5)
                conn.request("GET", "/", headers={"Connection": "close"})
                resp = conn.getresponse()
                resp.read()
                conn.close()
                return
            except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
                last = f"{type(exc).__name__}: {exc}"
            time.sleep(0.2)
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
    def _auth_header(self, case: Case) -> tuple[tuple[str, str], ...]:
        if not self.session.auth or case.anonymous:
            return ()
        if any(k.lower() == "authorization" for k, _ in case.headers):
            return ()
        import base64
        user, password = self.session.auth
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        return (("Authorization", f"Basic {token}"),)

    def _derived_headers(self, case: Case) -> tuple[tuple[str, str], ...]:
        """Headers whose value comes from a response earlier in this session.

        A missing source header is not an error here: if a port fails to emit an
        ``ETag`` at all, the conditional request goes out without the condition
        and the response it gets back differs from the golden one, which is the
        finding. Substituting a placeholder would hide it.
        """
        out: list[tuple[str, str]] = []
        for spec, source in ((case.etag_from, "etag"),
                             (case.last_modified_from, "last-modified")):
            if not spec:
                continue
            case_id, header_name = spec
            value = self.raw_headers.get(case_id, {}).get(source)
            if value is not None:
                out.append((header_name, value))
        return tuple(out)

    def send(self, case: Case) -> tuple[int, list[tuple[str, str]], bytes]:
        """Issue one request and return the response untouched.

        Separate from ``request`` because two callers need the raw bytes: the
        recorder, and route discovery -- which cannot use a normalised body,
        since normalisation is precisely what replaces the routes it is looking
        for.

        A transport-level failure is recorded, not raised. "The server closed the
        connection without answering" and "the server refused the connection" are
        real, observable outcomes -- actix-web does the first for a request line
        it will not parse -- and they have to be comparable like any other
        response, so they come back as status 0 with a marker header. Raising
        instead would abandon the rest of the session and score a submission on
        how early it first hung up.
        """
        body, case_headers = request_body(case)
        headers = {"Host": f"127.0.0.1:{self.port}", "Connection": "close"}
        for name, value in (self._auth_header(case) + self._derived_headers(case)
                            + case_headers):
            headers[name] = value

        try:
            if case.raw_request:
                return self._send_raw(case, body, headers)
            conn = self._connect(REQUEST_TIMEOUT)
            try:
                conn.request(case.method, case.path, body=body, headers=headers)
                resp = conn.getresponse()
                raw = resp.read()
                return resp.status, list(resp.getheaders()), raw
            finally:
                conn.close()
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            return 0, [("x-srb-transport", type(exc).__name__)], b""

    def _send_raw(self, case: Case, body: bytes | None,
                  headers: dict[str, str]) -> tuple[int, list[tuple[str, str]], bytes]:
        """Write the request bytes by hand, over a bare socket.

        ``http.client`` validates the request target before sending it and
        raises on a literal space or control character. Those targets are exactly
        what some cases are for: what a server does with a request line it should
        never have received is part of its contract, and the only way to ask is
        to put the bytes on the wire ourselves.

        The response parser here is deliberately minimal -- status line, headers,
        read to EOF -- which is sufficient because every raw case sends
        ``Connection: close``.
        """
        lines = [f"{case.method} {case.path} HTTP/1.1"]
        lines += [f"{name}: {value}" for name, value in headers.items()]
        if body is not None:
            lines.append(f"Content-Length: {len(body)}")
        blob = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1", "replace")
        if body is not None:
            blob += body

        sock = socket.create_connection(("127.0.0.1", self.port),
                                        timeout=REQUEST_TIMEOUT)
        try:
            if self.session.scheme == "https":
                ctx = ssl._create_unverified_context()            # noqa: SLF001
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                sock = ctx.wrap_socket(sock, server_hostname="127.0.0.1")
            sock.sendall(blob)
            chunks = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            try:
                sock.close()
            except OSError:
                pass

        data = b"".join(chunks)
        if not data:
            return 0, [("x-srb-transport", "EmptyResponse")], b""
        head, _, rest = data.partition(b"\r\n\r\n")
        head_lines = head.split(b"\r\n")
        status_line = head_lines[0].decode("latin-1", "replace")
        parts = status_line.split(" ", 2)
        try:
            status = int(parts[1])
        except (IndexError, ValueError):
            return 0, [("x-srb-transport", "UnparseableStatusLine")], data
        raw_headers = []
        for line in head_lines[1:]:
            name, sep, value = line.decode("latin-1", "replace").partition(":")
            if sep:
                raw_headers.append((name.strip(), value.strip()))
        return status, raw_headers, rest

    def request(self, case: Case) -> dict:
        archive_kind = None
        for query, kind in ARCHIVE_KIND_BY_QUERY.items():
            if f"download={query}" in case.path:
                archive_kind = kind

        status, raw_headers, raw = self.send(case)

        # Kept unscrubbed and out of the golden file: the only consumer is a
        # later conditional request in the same session.
        self.raw_headers[case.id] = {k.lower(): v for k, v in raw_headers}

        random_route = self.random_route
        return {
            **record(case.id, status, raw_headers, raw,
                     body_mode=case.body_mode,
                     decompress_scheme=case.decompress,
                     archive_kind=archive_kind if case.body_mode == "archive"
                     else None,
                     random_route=random_route,
                     prefixes=self._prefixes()),
            "note": case.note,
            "headers_extra": list(case.headers_extra),
            "headers_skip": list(case.headers_skip),
        }

    def discover_static_routes(self, root: str) -> dict[str, str | None]:
        """Read the favicon and stylesheet routes off the page that links them.

        They are nanoid-generated per process, so they cannot be written down in
        the corpus. Recovering them from the page is also a stronger check than a
        literal would be: it grades that the routes the page *advertises* are the
        routes the server actually answers on, which is a thing a port can get
        wrong in a way no fixed path would reveal.
        """
        import re
        try:
            _, _, raw = self.send(Case(id="__discover__", path=root))
        except (OSError, ssl.SSLError, http.client.HTTPException):
            return {"favicon": None, "css": None}
        try:
            body = raw.decode("utf-8")
        except UnicodeDecodeError:
            return {"favicon": None, "css": None}
        icon = re.search(r'<link\s+rel="icon"[^>]*\bhref="([^"]*)"', body, re.I)
        css = re.search(r'<link\s+rel="stylesheet"[^>]*\bhref="([^"]*)"',
                        body, re.I)
        return {
            "favicon": icon.group(1) if icon else None,
            "css": css.group(1) if css else None,
        }

    def discover_random_route(self) -> str | None:
        """Read the ``--random-route`` prefix off the server's own stdout.

        The prefix is generated per boot, so it cannot be in the corpus. It is
        recovered rather than guessed: miniserve prints the URLs it is available
        at, and the path component of one of them is the prefix. Recovering it
        is what makes the ``--random-route`` session gradeable at all -- without
        it every request would be a 404 and the session would prove nothing
        beyond "some route exists".
        """
        import re
        log = self._log()
        match = re.search(r"http://127\.0\.0\.1:\d+(/[0-9a-f]{6})\b", log)
        return match.group(1) if match else None


def play_session(repo: Path, binary: str, session: Session, workdir: Path,
                 spec: Path = TREE_SPEC) -> dict[str, dict]:
    """Boot a server, replay one session in order, return its responses.

    ``__meta__`` records the facts about the boot itself that no single response
    carries -- whether a ``--random-route`` prefix was generated and what shape
    it had -- and ``__tree__`` the digest of what was served, so a capture and a
    graded run that disagree about the tree say so instead of producing a
    thousand meaningless diffs.
    """
    out: dict[str, dict] = {}
    with Target(repo, binary, session, workdir, spec) as target:
        if "--random-route" in session.argv:
            target.random_route = target.discover_random_route()
            out["__meta__"] = {
                "random_route_found": target.random_route is not None,
                "random_route_len": (len(target.random_route) - 1
                                     if target.random_route else None),
                "random_route_hex": bool(
                    target.random_route
                    and all(c in "0123456789abcdef"
                            for c in target.random_route[1:])),
            }
        wants_static = any("{favicon}" in c.path or "{css}" in c.path
                           for c in session.cases)
        static: dict[str, str | None] = {"favicon": None, "css": None}
        if wants_static:
            static = target.discover_static_routes(
                session.discover_path.replace(
                    "{route}", target.random_route or "/000000"))
            out["__static__"] = {
                "favicon_found": static["favicon"] is not None,
                "css_found": static["css"] is not None,
                # Shape only. The value is a nanoid; what is contract is that it
                # is ten characters of the documented alphabet under the
                # documented prefix, which the golden file can hold and the
                # value cannot.
                "favicon_shape": _route_shape(static["favicon"]),
                "css_shape": _route_shape(static["css"]),
                "distinct": (static["favicon"] != static["css"]
                             if static["favicon"] and static["css"] else None),
            }

        for case in session.cases:
            probe = case
            path = case.path
            if "{route}" in path:
                path = path.replace("{route}", target.random_route or "/000000")
            if "{favicon}" in path:
                path = path.replace("{favicon}", static["favicon"] or "/missing")
            if "{css}" in path:
                path = path.replace("{css}", static["css"] or "/missing")
            if path != case.path:
                probe = replace(case, path=path)
            out[case.id] = target.request(probe)

        # A final read confirms the server survived the session rather than
        # dying on its last request, which a per-case comparison cannot tell
        # apart from an expected error response.
        out["__alive__"] = target.request(
            Case(id="__alive__", path=session.alive_path.replace(
                "{route}", target.random_route or "/000000"),
                body_mode="shape"))
        tree_root = target.serve_path if session.tree != "file" \
            else target.serve_path.parent
        out["__tree__"] = {
            "digest": tree_digest(tree_root),
            # Both forms, because they are compared under different conditions:
            # a read-only session must match on the timed digest (its tree is the
            # pinned sample tree and nothing touched it), while a session that
            # uploads can only be held to the untimed one.
            "untimed": tree_digest(tree_root, times=False),
        }
    return out


def play_all(repo: Path, binary: str, sessions, workdir: Path,
             spec: Path = TREE_SPEC,
             on_session=None) -> dict[str, dict[str, dict]]:
    result: dict[str, dict[str, dict]] = {}
    for session in sessions:
        if on_session:
            on_session(session)
        result[session.id] = play_session(repo, binary, session,
                                          workdir / session.id, spec)
    return result
