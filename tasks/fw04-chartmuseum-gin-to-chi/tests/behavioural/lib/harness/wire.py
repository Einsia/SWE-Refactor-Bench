"""Turn a :class:`~harness.profiles.Case` into an actual request.

Kept apart from :mod:`harness.launch` (which owns processes) and
:mod:`harness.normalize` (which owns responses) because this is the one place a
case's declarative fields become bytes, and it must do so identically during
capture and during grading.  A difference here would move the recorded
expectation and the graded observation together, and the suite would pass while
measuring nothing.

Two decisions worth stating, because both look like sloppiness and are not:

*The multipart body is assembled by hand.*  ``email.mime`` and friends fold long
lines, reorder parameters and choose their own boundaries.  ChartMuseum reads
these with Go's ``mime/multipart``, and the corpus includes cases about custom
form-field names and about a provenance part arriving with no chart part -- so the
exact framing is part of the test.  Hand-assembly makes the bytes explicit and
byte-stable across runs.

*The boundary is a constant.*  A random boundary would put a fresh string in
every request, and any response that echoed it -- an error naming the part it
could not parse -- would become spuriously volatile and get masked away, taking
real evidence with it.
"""

from __future__ import annotations

import base64

import io
import os

from harness.profiles import Case

FIXTURES = os.environ.get("CM_ORACLE_CHARTS", "/opt/testdata")

#: Fixed, for the reason in the module docstring.
BOUNDARY = "srbfw04boundary000000000000000000"

def _fixture_bytes(rel: str) -> bytes:
    path = os.path.join(FIXTURES, rel)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"no such frozen fixture: {rel} (in {FIXTURES})")
    with open(path, "rb") as fh:
        return fh.read()


def body_for(case: Case) -> bytes | None:
    """The request body, or None for a bodiless request.

    ``Case`` already guarantees at most one of data/multipart/upload is set, so
    the order below is presentational rather than a precedence rule.  Malformed
    bodies are spelled out as ``bytes`` literals in the corpus itself -- a
    truncated gzip header, a gzip carrying no tar, sixteen bytes of ``x`` -- rather
    than named here, so that reading a case tells you exactly what went on the
    wire without a second lookup.
    """
    if case.multipart:
        return _multipart_body(case.multipart)
    if case.upload:
        return _fixture_bytes(case.upload)
    return case.data


def _multipart_body(parts: tuple[tuple[str, str], ...]) -> bytes:
    """Assemble a multipart/form-data body from (field-name, fixture-path) pairs.

    The filename sent is the fixture's basename.  ChartMuseum does not trust it --
    it reads the chart metadata out of the archive -- but it does appear in some
    error paths, so it is derived rather than invented.
    """
    out = io.BytesIO()
    for field, rel in parts:
        payload = _fixture_bytes(rel)
        filename = os.path.basename(rel)
        out.write(f"--{BOUNDARY}\r\n".encode())
        out.write(
            f'Content-Disposition: form-data; name="{field}"; '
            f'filename="{filename}"\r\n'.encode())
        out.write(b"Content-Type: application/octet-stream\r\n\r\n")
        out.write(payload)
        out.write(b"\r\n")
    out.write(f"--{BOUNDARY}--\r\n".encode())
    return out.getvalue()


def headers_for(case: Case) -> list[tuple[str, str]]:
    """The request headers, in the order they go on the wire.

    Content-Length is not set here; :meth:`harness.launch.Server.request` derives
    it from the body so the two can never disagree.
    """
    headers: list[tuple[str, str]] = []
    if case.multipart:
        headers.append(
            ("Content-Type", f"multipart/form-data; boundary={BOUNDARY}"))
    if case.auth:
        # Basic auth spelled out rather than delegated, so a case can carry a
        # deliberately malformed credential and have it arrive malformed.
        token = base64.b64encode(case.auth.encode()).decode()
        headers.append(("Authorization", f"Basic {token}"))
    headers.extend(case.headers)
    return headers


def send(server, case: Case):
    """Perform ``case`` against ``server``; return (status, headers, body)."""
    return server.request(
        case.method, case.path,
        body=body_for(case),
        headers=headers_for(case),
    )
