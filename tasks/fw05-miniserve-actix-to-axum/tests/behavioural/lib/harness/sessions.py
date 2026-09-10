"""The session model.

miniserve is a process, not a library: everything it does is decided by the
command line it was started with. ``--hidden`` changes which entries the listing
contains, ``--index`` changes what a directory request even returns, ``--auth``
puts a 401 in front of everything, and ``--upload-files`` adds a route that did
not exist. None of that can be reached from one long-lived server, so the unit
of measurement here is a **session**: one process configuration plus an ordered
request sequence.

    Session(id, argv, cases)

*   ``argv``  -- the command line, minus the parts the harness owns. The runner
    appends the serve path, ``--interfaces 127.0.0.1`` and ``--port``.
    Everything else the session states explicitly, so the golden file records
    exactly what produced it.
*   ``port``  -- **pinned, not allocated.** Derived once per session id from
    ``PORT_BASE`` and never reused, because miniserve puts the bound address
    inside the page: the ``<title>``, the breadcrumb, the wget footer, and --
    decisively -- the QR code, whose SVG path *is* an encoding of the absolute
    URL. With a per-run port none of that could be compared beyond "a QR exists";
    with the port pinned, the QR is a byte-exact contract, which is the only way
    to grade that a port encodes the right URL without shipping a QR decoder.
    Sessions run one at a time and the ports are disjoint, so nothing contends.
*   ``tree``  -- which sample tree to serve. ``"root"`` is the whole spec tree;
    ``"file"`` serves a single file, which routes through a different handler in
    the baseline; ``"symlink"`` serves through a symlinked path.
*   ``mutating`` -- an upload, a mkdir or an overwrite changes the tree, so the
    session gets a tree of its own that nothing else replays against. Read-only
    sessions share a materialised tree, which is what keeps 40-odd sessions
    inside a sensible wall clock.
*   ``scheme`` -- ``"https"`` for the TLS sessions. The certificate is not
    verified; what is being measured is that a TLS listener exists and serves
    the same bytes.

Ordering inside a session is part of the contract. The upload sessions POST a
file and then read the listing back; if the read came first the response would
be a different one, and the golden file would be recording a different claim.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: How a case's body is compared. Set explicitly on every case; never inferred.
#:
#: ``exact``    -- byte for byte after normalisation. Most cases.
#: ``html``     -- an HTML page whose per-boot nonce routes are placeholdered.
#:                 Still compared byte for byte after that substitution: the
#:                 listing's entry order, escaping, link targets, footer and
#:                 breadcrumb trail are all contract.
#: ``archive``  -- a tar/tar.gz/zip stream. The container's own framing carries
#:                 mtimes and compression detail that no two implementations
#:                 agree on byte for byte, so the *member list* is compared
#:                 instead, plus the fact that it unpacks at all.
#: ``binary``   -- compared by length and digest only.
#: ``shape``    -- the body legitimately differs per boot beyond nonces (a
#:                 random route name in a URL). Compared by structure.
BODY_MODES = ("exact", "html", "archive", "binary", "shape")


@dataclass(frozen=True)
class Case:
    """One request, and how the response to it is compared."""

    id: str
    method: str = "GET"
    #: Request target, appended to the session's base URL verbatim. Not
    #: re-encoded: several cases exist precisely to pin how a raw or
    #: over-encoded path is handled.
    path: str = "/"
    #: Raw request body, sent verbatim.
    data: bytes | None = None
    #: Extra request headers.
    headers: tuple[tuple[str, str], ...] = ()
    #: A multipart upload, built by the runner: (field, filename, content_type,
    #: bytes). Turned into a body with a fixed boundary so the request itself is
    #: byte-identical between capture and grading.
    multipart: tuple[tuple[str, str, str, bytes], ...] = ()
    #: Response headers compared by value beyond the always-compared set.
    headers_extra: tuple[str, ...] = ()
    #: Response headers this case must not compare.
    headers_skip: tuple[str, ...] = ()
    #: Compare the decompressed body under this scheme rather than raw bytes.
    decompress: str | None = None
    body_mode: str = "exact"
    #: Echo an earlier case's ``ETag`` into this request:
    #: ``("case-id", "If-None-Match")``. Conditional requests cannot be written
    #: as literals, because actix-files derives its ETag partly from the inode,
    #: which differs between two materialisations of the same tree. Sending back
    #: whatever the server itself just issued is the only way to grade the
    #: conditional path -- and it grades it *harder* than a literal would, since
    #: a port that issues an ETag it then fails to honour is caught here.
    etag_from: tuple[str, str] | None = None
    #: Same, for ``Last-Modified`` -> ``If-Modified-Since`` / ``If-Range``.
    last_modified_from: tuple[str, str] | None = None
    #: Write the request bytes by hand over a bare socket instead of going
    #: through ``http.client``. Needed for request targets the client library
    #: refuses to send -- a literal space, a control character -- which are
    #: precisely the ones where what the *server* does is worth knowing.
    raw_request: bool = False
    #: Send no credentials even in a session that has them. The unauthenticated
    #: response is as much a contract as the authenticated one -- the status, the
    #: ``WWW-Authenticate`` challenge and the rendered 401 page all have to
    #: match -- so it needs to be reachable from inside an authenticated session.
    anonymous: bool = False
    #: Free-form note carried into the golden file.
    note: str = ""

    def __post_init__(self) -> None:
        if self.body_mode not in BODY_MODES:
            raise ValueError(f"{self.id}: unknown body_mode {self.body_mode!r}")
        if self.data is not None and self.multipart:
            raise ValueError(f"{self.id}: data and multipart are exclusive")


#: First port handed out. Well above the ephemeral range Linux uses by default
#: (32768-60999 on this image), so a pinned port cannot collide with a port the
#: kernel assigned to something else in the container.
PORT_BASE = 21000


@dataclass(frozen=True)
class Session:
    """One miniserve process and the request sequence it answers."""

    id: str
    cases: tuple[Case, ...]
    #: Command line, minus path/--interfaces/--port.
    argv: tuple[str, ...] = ()
    #: Pinned listen port. Assigned by ``corpus.py`` in declaration order; see
    #: the module docstring for why it is pinned rather than allocated.
    port: int = 0
    #: Which sample tree to serve: see module docstring.
    tree: str = "root"
    #: Extra environment for the process.
    env: tuple[tuple[str, str], ...] = ()
    mutating: bool = False
    scheme: str = "http"
    #: HTTP basic credentials the runner attaches to every case in the session
    #: that does not carry its own Authorization header.
    auth: tuple[str, str] | None = None
    #: Path the runner reads at the end of the session to prove the process is
    #: still serving. Defaults to the root, which is wrong for ``--route-prefix``
    #: and for ``--disable-indexing`` sessions, so those state their own.
    alive_path: str = "/"
    #: Path whose HTML carries the ``<link>`` tags the nonce static routes are
    #: read from. Only consulted by sessions that request ``{favicon}``/``{css}``.
    #: Defaults to the root; a session whose root is not a listing (``--index``,
    #: ``--disable-indexing``, a route prefix) has to name one that is.
    discover_path: str = "/"
    note: str = ""


#: Multipart boundary. Fixed, so the request bytes are identical every run and a
#: difference in the response can only come from the server.
BOUNDARY = "srbfw05boundary9d1f4c7a"


def multipart_body(parts: tuple[tuple[str, str, str, bytes], ...]) -> bytes:
    """Build a multipart/form-data body with the fixed boundary.

    Written out by hand rather than with ``email.mime`` so the line endings,
    the header order and the trailing boundary are all exactly what the golden
    capture saw.
    """
    chunks: list[bytes] = []
    for field, filename, content_type, content in parts:
        chunks.append(f"--{BOUNDARY}\r\n".encode())
        disposition = f'form-data; name="{field}"'
        if filename is not None:
            disposition += f'; filename="{filename}"'
        chunks.append(f"Content-Disposition: {disposition}\r\n".encode())
        if content_type:
            chunks.append(f"Content-Type: {content_type}\r\n".encode())
        chunks.append(b"\r\n")
        chunks.append(content)
        chunks.append(b"\r\n")
    chunks.append(f"--{BOUNDARY}--\r\n".encode())
    return b"".join(chunks)


def multipart_content_type() -> str:
    return f"multipart/form-data; boundary={BOUNDARY}"


def request_body(case: Case) -> tuple[bytes | None, tuple[tuple[str, str], ...]]:
    """The bytes and headers to send for a case."""
    headers = tuple(case.headers)
    if case.multipart:
        body = multipart_body(case.multipart)
        if not any(k.lower() == "content-type" for k, _ in headers):
            headers = headers + (("Content-Type", multipart_content_type()),)
        return body, headers
    return case.data, headers


def digest(value: Any) -> str:
    """Stable digest of a JSON-able value, used for corpus fingerprinting."""
    import hashlib
    import json

    payload = json.dumps(value, sort_keys=True, default=repr,
                         ensure_ascii=True).encode()
    return hashlib.sha256(payload).hexdigest()
