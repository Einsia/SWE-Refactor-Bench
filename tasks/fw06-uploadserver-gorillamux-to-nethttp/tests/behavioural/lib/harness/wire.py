"""Raw HTTP/1.1 over a socket, because every convenience client normalises.

WHY THIS EXISTS.  The single most informative case in this corpus is

    GET /files/sub%2Fb.txt

which gorilla/mux answers 200 (it routes on the DECODED r.URL.Path, so %2F is a
real separator) and a naive stdlib port answers 404 (ServeMux matches on the
ESCAPED path).  Any client that helpfully rewrites the target -- urllib, requests,
httpx, curl --path-as-is notwithstanding -- destroys the case before it reaches
the server.  Likewise `/files/../a.txt`, `/files//a.txt` and `/files/./a.txt`
must arrive at the server exactly as written, because the 301 they provoke is
part of the graded contract.

So the request line is assembled byte for byte and written to a socket, and the
response is parsed only as far as HTTP/1.1 framing requires.  Nothing here
interprets a path.
"""
from __future__ import annotations

import socket


class Response:
    __slots__ = ("status", "reason", "headers", "header_order", "body", "raw_head")

    def __init__(self, status, reason, headers, header_order, body, raw_head):
        self.status = status
        self.reason = reason
        self.headers = headers          # lower-cased name -> list of values
        self.header_order = header_order  # [(original-case name, value)]
        self.body = body                # bytes, exactly as framed
        self.raw_head = raw_head        # bytes of the status line + headers

    def get(self, name: str, default: str = "") -> str:
        vals = self.headers.get(name.lower())
        return vals[0] if vals else default

    def all(self, name: str) -> list[str]:
        return list(self.headers.get(name.lower(), []))


def _read_until(sock, marker: bytes, limit: int = 1 << 20) -> bytes:
    buf = b""
    while marker not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
        if len(buf) > limit:
            raise RuntimeError("header section exceeded limit")
    return buf


def _recv_exact(sock, n: int, prefix: bytes) -> bytes:
    buf = prefix
    while len(buf) < n:
        chunk = sock.recv(min(65536, n - len(buf)))
        if not chunk:
            break
        buf += chunk
    return buf[:n]


def _recv_chunked(sock, prefix: bytes) -> bytes:
    """Decode chunked transfer coding into the entity body."""
    buf = prefix
    out = b""
    while True:
        while b"\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                return out
            buf += chunk
        line, buf = buf.split(b"\r\n", 1)
        size = int(line.split(b";", 1)[0].strip() or b"0", 16)
        if size == 0:
            return out
        while len(buf) < size + 2:
            chunk = sock.recv(65536)
            if not chunk:
                return out + buf[:size]
            buf += chunk
        out += buf[:size]
        buf = buf[size + 2:]


def _recv_to_close(sock, prefix: bytes) -> bytes:
    out = prefix
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            return out
        out += chunk


def request(host: str, port: int, method: str, target: str, *,
            headers: dict | None = None, body: bytes | None = None,
            timeout: float = 15.0, http_version: str = "HTTP/1.1") -> Response:
    """Send one request with `target` used verbatim as the request-target."""
    hdrs = dict(headers or {})
    hdrs.setdefault("Host", f"{host}:{port}")
    hdrs.setdefault("Connection", "close")
    if body is not None and not any(k.lower() == "content-length" for k in hdrs):
        hdrs["Content-Length"] = str(len(body))

    head = f"{method} {target} {http_version}\r\n".encode()
    for k, v in hdrs.items():
        head += f"{k}: {v}\r\n".encode()
    head += b"\r\n"

    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(head + (body or b""))
        buf = _read_until(sock, b"\r\n\r\n")
        if b"\r\n\r\n" not in buf:
            raise RuntimeError(f"no complete response head; got {buf[:200]!r}")
        raw_head, rest = buf.split(b"\r\n\r\n", 1)
        lines = raw_head.split(b"\r\n")
        parts = lines[0].decode("latin-1").split(" ", 2)
        status = int(parts[1])
        reason = parts[2] if len(parts) > 2 else ""

        headers_map: dict[str, list[str]] = {}
        order: list[tuple[str, str]] = []
        for ln in lines[1:]:
            if not ln or b":" not in ln:
                continue
            name, _, value = ln.decode("latin-1").partition(":")
            value = value.strip()
            order.append((name, value))
            headers_map.setdefault(name.lower(), []).append(value)

        # Framing, in the order RFC 7230 requires it be considered.
        if method == "HEAD" or status in (204, 304) or 100 <= status < 200:
            payload = b""
        elif any(v.lower() == "chunked"
                 for v in headers_map.get("transfer-encoding", [])):
            payload = _recv_chunked(sock, rest)
        elif "content-length" in headers_map:
            payload = _recv_exact(sock, int(headers_map["content-length"][0]), rest)
        else:
            payload = _recv_to_close(sock, rest)

    return Response(status, reason, headers_map, order, payload, raw_head)
