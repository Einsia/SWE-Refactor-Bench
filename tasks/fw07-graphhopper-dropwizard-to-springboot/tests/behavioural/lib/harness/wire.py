"""The HTTP client, deliberately dumb.

`http.client` from the standard library, not requests and not urllib.  Both of
those are helpful in ways that destroy the measurement: they follow redirects
(so a 302 becomes the 200 behind it and `/maps` stops grading anything), they
add an Accept-Encoding the corpus did not ask for (so gzip negotiation is no
longer under the corpus's control), they normalize the path (so `/i18n//de` and
`/i18n/de%2Fen` are silently rewritten before the server ever sees them), and
they lowercase or fold header names on the way out.

Every one of those helpfulness features is a case this corpus is trying to ask.
So: one connection, the exact request line the case names, the exact headers it
names and nothing else, no redirect following, no retry.
"""
from __future__ import annotations

import http.client
import socket


class Response:
    """A response as raw as it comes off the socket."""

    def __init__(self, status: int, reason: str, headers: list[tuple[str, str]],
                 body: bytes):
        self.status = status
        self.reason = reason
        self.headers = headers      # ordered, original case, repeats preserved
        self.body = body

    def __repr__(self):
        return f"<Response {self.status} {len(self.body)}B>"


class Transport:
    """A one-shot request against one port.

    A fresh connection per request, on purpose.  Connection reuse would let one
    case's state — a half-read body, a server-side keep-alive timeout — decide
    the next case's answer, and the two sides do not have to agree about
    persistent connections for the comparison to be valid.
    """

    def __init__(self, host: str, port: int, timeout: float = 120.0):
        self.host = host
        self.port = port
        self.timeout = timeout

    def send(self, method: str, target: str, headers: dict, body) -> Response:
        conn = http.client.HTTPConnection(self.host, self.port,
                                          timeout=self.timeout)
        try:
            # skip_accept_encoding and skip_host: http.client adds both unless
            # told not to.  An injected Accept-Encoding would make every gzip
            # case a lie, and an injected Host would mask a submission that
            # depends on one.  Host is supplied explicitly instead.
            conn.putrequest(method, target, skip_accept_encoding=True,
                            skip_host=True)
            conn.putheader("Host", f"{self.host}:{self.port}")
            sent = {"host"}
            for name, value in headers.items():
                conn.putheader(name, value)
                sent.add(name.lower())
            if body is not None and "content-length" not in sent:
                conn.putheader("Content-Length", str(len(body)))
            conn.endheaders()
            if body:
                conn.send(body)
            raw = conn.getresponse()
            # read() before the connection closes, and never getheader() —
            # getheader() joins repeats with ", " and loses the boundary
            # between two Access-Control-Allow-Methods values.
            payload = raw.read()
            return Response(raw.status, raw.reason, raw.getheaders(), payload)
        finally:
            conn.close()

    def wait_ready(self, path: str, deadline: float) -> tuple[bool, str]:
        """Poll one path until the server answers anything at all.

        Any HTTP status counts as ready, including 404 and 500.  Readiness here
        means "the socket is accepting and something is speaking HTTP on it" —
        deciding whether the ANSWER is right is what the graded cases do, and a
        readiness probe that demanded a 200 would turn a submission whose health
        endpoint moved into a startup timeout, reported as a build failure
        instead of as the one case it actually breaks.
        """
        import time
        last = "no attempt made"
        while time.monotonic() < deadline:
            try:
                r = self.send("GET", path, {}, None)
                return True, f"HTTP {r.status} on {path}"
            except (OSError, socket.timeout, http.client.HTTPException) as exc:
                last = f"{type(exc).__name__}: {exc}"
                time.sleep(0.5)
        return False, last
