"""The compressed endpoints must really compress, on the wire.

``/gzip``, ``/deflate`` and ``/brotli`` are the only routes whose contract is
about the *bytes on the connection* rather than the payload a client ends up
with. Any HTTP client worth using inflates a body whose ``Content-Encoding`` it
recognises, so the corpus replay -- which uses httpx -- never sees the compressed
form at all. That makes the corpus blind to two real regressions:

* an endpoint that sets ``Content-Encoding: gzip`` and sends plain JSON, and
* an endpoint that compresses with the wrong algorithm for its header.

Both are invisible to a data comparison and both break real clients. So this
module speaks HTTP/1.1 over a bare socket, takes the bytes exactly as sent, and
inflates them itself.

State A compresses unconditionally on these routes, ignoring ``Accept-Encoding``
entirely -- ``/gzip`` gzips even for a client that asked for ``identity``. That
is the behaviour being preserved, so it is what is asserted.
"""

from __future__ import annotations

import gzip
import json
import socket
import zlib

import pytest

pytestmark = pytest.mark.behaviour

#: path -> (declared Content-Encoding, marker key in the JSON payload)
COMPRESSED = {
    "/gzip": ("gzip", "gzipped"),
    "/deflate": ("deflate", "deflated"),
    "/brotli": ("br", "brotli"),
}


def _raw_get(host, port, path, extra_headers=(), timeout=30.0):
    """One HTTP/1.1 GET over a bare socket. Returns (status, headers, body).

    ``Connection: close`` avoids having to parse chunked framing or track
    keep-alive: the body is everything up to EOF.
    """
    request = [f"GET {path} HTTP/1.1", f"Host: {host}:{port}", "Connection: close"]
    request.extend(f"{n}: {v}" for n, v in extra_headers)
    wire = ("\r\n".join(request) + "\r\n\r\n").encode("ascii")

    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.sendall(wire)
        chunks = []
        while True:
            block = sock.recv(65536)
            if not block:
                break
            chunks.append(block)
    data = b"".join(chunks)

    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    status = int(lines[0].split(b" ")[1])
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(b":")
        headers.setdefault(name.decode("latin-1").lower().strip(), []).append(
            value.decode("latin-1").strip()
        )

    # If the response was chunked, de-chunk it; State A under gunicorn sends
    # these with a Content-Length, but a port is free to stream them.
    if "chunked" in ",".join(headers.get("transfer-encoding", [])).lower():
        body = _dechunk(body)
    return status, headers, body


def _dechunk(raw: bytes) -> bytes:
    out = bytearray()
    while raw:
        line, _, rest = raw.partition(b"\r\n")
        try:
            size = int(line.split(b";")[0], 16)
        except ValueError:
            break
        if size == 0:
            break
        out += rest[:size]
        raw = rest[size + 2:]
    return bytes(out)


def _inflate(encoding: str, raw: bytes) -> bytes:
    if encoding == "gzip":
        return gzip.decompress(raw)
    if encoding == "deflate":
        try:
            return zlib.decompress(raw)
        except zlib.error:
            return zlib.decompress(raw, -15)
    if encoding == "br":
        try:
            import brotlicffi as brotli
        except ImportError:  # pragma: no cover - the verifier ships brotlicffi
            import brotli
        return brotli.decompress(raw)
    raise AssertionError(f"unexpected encoding {encoding!r}")


@pytest.fixture(scope="module")
def wire(server):
    """Fetch the three compressed routes over a raw socket, once."""
    out = {}
    for path in COMPRESSED:
        out[path] = _raw_get("127.0.0.1", server.port, path)
    return out


@pytest.mark.parametrize("path", sorted(COMPRESSED))
def test_declares_the_encoding(path, wire):
    status, headers, _ = wire[path]
    encoding, _marker = COMPRESSED[path]
    assert status == 200, f"{path} answered {status}"
    got = headers.get("content-encoding")
    assert got == [encoding], (
        f"{path} sent Content-Encoding {got!r}, State A sends [{encoding!r}]"
    )


@pytest.mark.parametrize("path", sorted(COMPRESSED))
def test_body_on_the_wire_is_really_compressed(path, wire):
    """The bytes sent must be a valid stream of the declared type.

    This is the check the corpus cannot make: a route that sets the header and
    sends plain JSON passes every data comparison and fails here.
    """
    _status, _headers, body = wire[path]
    encoding, _marker = COMPRESSED[path]
    assert body, f"{path} sent an empty body"
    try:
        inflated = _inflate(encoding, body)
    except Exception as exc:
        pytest.fail(
            f"{path} declared Content-Encoding: {encoding} but its body is not "
            f"a valid {encoding} stream ({type(exc).__name__}: {exc}). "
            f"First bytes: {body[:32]!r}. A client that trusts the header "
            f"cannot read this response at all."
        )
    assert inflated.lstrip()[:1] == b"{", (
        f"{path} inflated to something that is not a JSON object: "
        f"{inflated[:120]!r}"
    )


@pytest.mark.parametrize("path", sorted(COMPRESSED))
def test_the_wire_bytes_are_not_the_payload(path, wire):
    """Something was applied to the payload before it went out.

    Deliberately not ``len(body) < len(inflated)``, which measures a compression
    *ratio* rather than the contract. The payloads on these three routes are
    230-330 byte JSON objects, and while a default-level gzip does shrink one, a
    legitimate stream need not: a gzip member written at level 0 is stored rather
    than deflated and comes out *longer* than its input, and it is still a gzip
    member that every client on earth reads correctly. A submission that chose
    level 0, or a low brotli quality on a 240-byte body, would fail a check about
    compression ratios while honouring the contract exactly.

    The regression worth catching is "declares gzip, sends plain JSON", and
    ``test_body_on_the_wire_is_really_compressed`` catches it: plain JSON is not
    a valid gzip member and the inflate raises. What is asserted here is that the
    bytes on the connection are not simply the payload, which is true of any real
    encoding including a stored one.
    """
    _status, _headers, body = wire[path]
    encoding, _marker = COMPRESSED[path]
    inflated = _inflate(encoding, body)
    assert body != inflated, (
        f"{path} sent its payload unencoded while declaring "
        f"Content-Encoding: {encoding}"
    )
    # Recorded, not asserted: a ratio is worth seeing in a report and is not a
    # defect. State A's own answers compress to roughly 60% at its default level.
    if len(body) >= len(inflated):
        print(f"note: {path} sent {len(body)}B to deliver {len(inflated)}B, so "
              f"the {encoding} stream is stored rather than compressed")


@pytest.mark.parametrize("path", sorted(COMPRESSED))
def test_payload_carries_its_marker(path, wire):
    """/gzip says gzipped:true, /deflate deflated:true, /brotli brotli:true."""
    _status, _headers, body = wire[path]
    encoding, marker = COMPRESSED[path]
    payload = json.loads(_inflate(encoding, body))
    assert payload.get(marker) is True, (
        f"{path} payload does not carry {marker!r}: keys {sorted(payload)}"
    )
    assert "headers" in payload and "origin" in payload, (
        f"{path} payload is missing the standard keys: {sorted(payload)}"
    )


@pytest.mark.parametrize("path", sorted(COMPRESSED))
def test_compresses_even_for_a_client_asking_for_identity(path, server):
    """State A ignores Accept-Encoding on these routes; so must the port.

    A port that added content negotiation would be a behaviour change, and would
    silently break the corpus replay -- which sends ``identity``.
    """
    encoding, _marker = COMPRESSED[path]
    status, headers, body = _raw_get(
        "127.0.0.1", server.port, path,
        extra_headers=[("Accept-Encoding", "identity")],
    )
    assert status == 200
    assert headers.get("content-encoding") == [encoding], (
        f"{path} stopped compressing when the client asked for identity "
        f"(Content-Encoding: {headers.get('content-encoding')!r}). State A "
        f"compresses regardless."
    )
    _inflate(encoding, body)  # raises if it is not really that encoding


@pytest.mark.parametrize("path", sorted(COMPRESSED))
def test_declared_length_matches_the_compressed_bytes(path, wire):
    """If Content-Length is sent, it must describe the *compressed* body.

    Length is otherwise out of contract, but a length taken from the payload
    before compression truncates the response for every client.
    """
    _status, headers, body = wire[path]
    declared = headers.get("content-length")
    if not declared:
        pytest.skip("this port streams the compressed body")
    assert int(declared[0]) == len(body), (
        f"{path} declared Content-Length {declared[0]} but sent {len(body)}B "
        f"of compressed data"
    )
