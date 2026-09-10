"""Fixtures a candidate test gets.  Loaded by name (``-p srbcandidate``).

A candidate is a black-box HTTP test.  Everything here hands it a way to ask the
running server a question and read the exact bytes back; nothing here tells it
which of the two trees it is talking to, and there is no fixture that could.

The sender is stage 2's ``harness.wire``, unmodified and imported rather than
reimplemented.  That matters for a reason beyond saving code: an adversary's
finding is only interesting if it is a divergence the behavioural suite would also
have seen had it thought to look.  Two different HTTP clients -- one that sends
``Connection: close`` and one that does not, one that percent-normalises the
request target and one that passes it verbatim -- can disagree about a response
without either server being wrong, and a stage 3 built on its own client would
report those disagreements as defects.

The one liberty taken over stage 2 is `raw`, which sends a hand-built request
line.  Stage 2's corpus is a fixed list and does not need it; an adversary
probing what a router does with a malformed target does.
"""
from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import wire  # noqa: E402


def _servers() -> dict:
    path = os.environ.get("SRB_SERVERS")
    if not path or not Path(path).is_file():
        raise RuntimeError(
            "SRB_SERVERS is not set or does not exist. This test is being run "
            "outside the verification harness, which is the only thing that "
            "starts the servers it needs.")
    return json.loads(Path(path).read_text())


#: Flag profiles a candidate may address, and what each one changes.  Named here
#: because a candidate that asks for a profile that does not exist should get a
#: sentence rather than a KeyError.
PROFILES = {
    "default": "no flags; CORS is on, auth is off, uploads are unlimited",
    "nocors": "-enable_cors=false",
    "auth": "-enable_auth with read-only token 'ro1' and read-write token 'rw1'",
    "maxsize": "-max_upload_size 16, so anything larger is refused mid-copy",
}

#: The credentials the `auth` profile was started with.
RO_TOKEN = "ro1"
RW_TOKEN = "rw1"


class Client:
    """One server, addressed by profile."""

    def __init__(self, profile: str, host: str, port: int, docroot: str) -> None:
        self.profile = profile
        self.host = host
        self.port = port
        #: The document root this server was started with, seeded from the frozen
        #: fixtures.  Readable because some of the contract is about what ends up
        #: on disk -- a refused upload leaves a truncated file, and the only way to
        #: assert that is to look.
        self.docroot = Path(docroot)

    # -- asking questions ----------------------------------------------------
    def send(self, method: str, target: str, *, headers: dict | None = None,
             body: bytes | None = None, timeout: float = 15.0) -> wire.Response:
        """One request.  `target` is used VERBATIM as the request-target.

        No normalisation, no percent-encoding, no collapsing of slashes: what is
        passed here is what goes on the wire.  That is the point of using a raw
        socket rather than an HTTP library, because on this task the difference
        between `/files/a%2Fb.txt` and `/files/a/b.txt` is a graded behaviour and
        a client that tidied either one would hide it.
        """
        return wire.request(self.host, self.port, method, target,
                            headers=headers, body=body, timeout=timeout)

    def get(self, target: str, **kw) -> wire.Response:
        return self.send("GET", target, **kw)

    def head(self, target: str, **kw) -> wire.Response:
        return self.send("HEAD", target, **kw)

    def options(self, target: str, **kw) -> wire.Response:
        return self.send("OPTIONS", target, **kw)

    # There is deliberately no `raw()` for sending a hand-built request line.
    # `send` interpolates the method and the target into the request line without
    # inspecting either, so a malformed line is already reachable: `send("GET",
    # "/a /b")` puts four tokens on the wire and `send("", "/x")` puts two.  A
    # second sender would have had to duplicate the response parsing, and it is
    # the parsing that has to agree with stage 2 for a finding here to mean the
    # behavioural suite could have found it too.

    # -- uploading -----------------------------------------------------------
    def multipart(self, filename: str, content: bytes,
                  field: str = "file") -> tuple[dict, bytes]:
        """Headers and body for an upload, encoded the way this server accepts one.

        Both PUT and POST reach `r.FormFile("file")`, so an upload is multipart on
        both verbs and a raw body is a 500 rather than a 201 -- which is itself
        graded behaviour, so this is a convenience and not a requirement.  Build
        the body by hand when the encoding is the thing under test.
        """
        b = "----SWERefactorBenchCandidate"
        body = (
            f"--{b}\r\n"
            f'Content-Disposition: form-data; name="{field}"; '
            f'filename="{filename}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n"
        ).encode() + content + f"\r\n--{b}--\r\n".encode()
        return {"Content-Type": f"multipart/form-data; boundary={b}"}, body

    def upload(self, method: str, target: str, content: bytes,
               filename: str = "probe.txt", *, headers: dict | None = None,
               **kw) -> wire.Response:
        """PUT or POST `content` to `target` as multipart."""
        mp_headers, body = self.multipart(filename, content)
        mp_headers.update(headers or {})
        return self.send(method, target, headers=mp_headers, body=body, **kw)

    # -- authenticating ------------------------------------------------------
    def bearer(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    def basic(self, token: str, user: str = "") -> dict:
        raw = f"{user}:{token}".encode()
        return {"Authorization": "Basic " + base64.b64encode(raw).decode()}

    def __repr__(self) -> str:
        return f"<Client {self.profile} 127.0.0.1:{self.port}>"


@pytest.fixture(scope="session")
def servers() -> dict:
    """Every profile, keyed by name.  ``servers["auth"].get("/files/a.txt")``.

    All four are already running when the test starts; there is nothing to boot
    and no ordering to respect between them.  Each has its OWN document root,
    seeded identically from the frozen fixtures, so an upload in one profile is
    not visible in another and a test does not have to clean up after itself.
    """
    out = {}
    for name, info in _servers().items():
        out[name] = Client(name, info["host"], info["port"], info["docroot"])
    return out


@pytest.fixture(scope="session")
def server(servers) -> Client:
    """The default profile: no flags, CORS on, auth off, uploads unlimited."""
    return servers["default"]


@pytest.fixture(scope="session")
def fixture_mtime() -> str:
    """The mtime every seeded fixture file carries, as `touch -d` spelled it.

    Constant, and the same constant stage 2 uses.  A file the run itself created
    carries a wall clock instead, which is why asserting on one of those is out of
    scope: it cannot agree between two runs, let alone between two trees.
    """
    return os.environ.get("SRB_FIXTURE_MTIME", "2026-01-01 00:00:00 UTC")


#: The seeded document root, read once before any test runs.  See `fixture_files`.
_SEEDED: dict[str, bytes] = {}


def pytest_configure(config) -> None:
    """Snapshot the seeded docroot before the first test can change it.

    Eagerly, and not in the fixture that hands it out.  A session fixture is
    evaluated when something first asks for it, so a test that uploaded and then
    asked would be told the upload was part of the frozen fixture set -- and a test
    that asked first would be told it was not.  The contents would then depend on
    test order, which is the one thing a candidate cannot afford: it has to answer
    identically on two trees, and pytest is under no obligation to order two runs
    the same way.
    """
    root = Path(_servers()["default"]["docroot"])
    _SEEDED.clear()
    _SEEDED.update({str(p.relative_to(root)): p.read_bytes()
                    for p in sorted(root.rglob("*")) if p.is_file()})


@pytest.fixture(scope="session")
def fixture_files() -> dict[str, bytes]:
    """The seeded document root's contents, by relative path.

    A snapshot taken before the first test ran, so it is the frozen fixture set
    however much the tests have since uploaded.  Every profile was seeded from the
    same tree, so these are the contents of all four.  Useful for asserting that a
    GET returned the file rather than something of the right length.
    """
    return dict(_SEEDED)
