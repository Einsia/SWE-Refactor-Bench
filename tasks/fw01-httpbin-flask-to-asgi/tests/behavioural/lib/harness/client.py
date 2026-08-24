"""Request construction shared by the golden capture and the grader.

Both sides build their requests here, from the same corpus, with the same
client settings. If they did not, a difference in how the *request* was framed
could masquerade as a difference in the server's behaviour.
"""

from __future__ import annotations

import base64
import socket
import time

import httpx


def encode_header_value(value: str) -> bytes:
    """Put a header value on the wire the way RFC 7230 says: latin-1 bytes.

    httpx would otherwise try ASCII and refuse anything above 0x7f, which would
    make the non-ASCII header cases unsendable.
    """
    return value.encode("latin-1")


def build_body(case) -> bytes | None:
    body = case["body"]
    if isinstance(body, dict):
        return base64.b64decode(body["b64"])
    if isinstance(body, str):
        return body.encode("utf-8")
    return None


def build_url(case, base: str) -> str:
    url = base.rstrip("/") + case["path"]
    if case["query"] is not None:
        url += "?" + case["query"]
    return url


def make_client(timeout: float = 60.0) -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(timeout),
        follow_redirects=False,
        # Never negotiate compression: /gzip and friends must be observed as
        # the exact bytes the server emitted.
        headers={"Accept-Encoding": "identity"},
        trust_env=False,
    )


def send_case(client: httpx.Client, case, base: str) -> httpx.Response:
    """Play one corpus case. The URL is passed through unparsed."""
    url = build_url(case, base)
    headers = [(n.encode("latin-1"), encode_header_value(v))
               for n, v in case["headers"]]
    content = build_body(case)
    request = client.build_request(
        case["method"], url, headers=headers, content=content
    )
    return client.send(request, stream=False)


def wait_for_port(host: str, port: int, timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return
        except OSError as exc:
            last = exc
            time.sleep(0.15)
    raise RuntimeError(f"nothing listening on {host}:{port} after {timeout}s: {last}")


def wait_for_http(base: str, timeout: float = 60.0) -> None:
    """Wait until the server answers an HTTP request, not just accepts a TCP
    connection -- uvicorn binds before the app is importable in some setups."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            with httpx.Client(timeout=3.0, trust_env=False) as c:
                c.get(base + "/status/200")
            return
        except Exception as exc:
            last = exc
            time.sleep(0.2)
    raise RuntimeError(f"{base} never answered HTTP within {timeout}s: {last}")
