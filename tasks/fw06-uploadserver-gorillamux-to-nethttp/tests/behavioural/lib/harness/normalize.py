"""Turn a raw response into a comparable record.

The rule this file follows: MASK what genuinely varies between two runs of the
same binary, and grade everything else.  Every mask is a small hole in the graded
surface, so each one below names what varies and why it cannot be pinned instead.

The opposite failure is worth stating because it is the one that bites quietly: if
you DROP a varying field instead of masking it, the record no longer says the
field was present, and a submission that stops emitting the header entirely
compares equal.  So a masked field keeps its name and its shape and loses only the
part that moves.
"""
from __future__ import annotations

import base64
import re

# Headers whose value cannot be reproduced and cannot be pinned.
#   date: wall clock, emitted by net/http on every response.
VOLATILE_HEADERS = {"date"}

# Files the server itself created during the run carry a wall-clock mtime, so
# their Last-Modified moves.  Fixture files carry the stamped constant and ARE
# graded -- so this is keyed by case, not applied globally.
_BOUNDARY_RE = re.compile(rb"(multipart/byteranges; boundary=)[0-9a-fA-F]+")
_BOUNDARY_BODY_RE = re.compile(rb"--[0-9a-fA-F]{16,}")


def _mask_boundary_value(value: str) -> str:
    """multipart/byteranges boundaries are random hex per response."""
    return re.sub(r"(multipart/byteranges; boundary=)[0-9a-fA-F]+",
                  r"\1<BOUNDARY>", value)


def _mask_boundary_body(body: bytes) -> bytes:
    """The same boundary appears throughout a multi-range body."""
    return _BOUNDARY_BODY_RE.sub(b"--<BOUNDARY>", body)


def record(resp, *, mask_last_modified: bool = False) -> dict:
    """Build the comparable record for one response."""
    headers = {}
    for name, values in resp.headers.items():
        if name in VOLATILE_HEADERS:
            headers[name] = ["<MASKED>"] * len(values)
            continue
        out = []
        for v in values:
            if name == "content-type":
                v = _mask_boundary_value(v)
            elif name == "last-modified" and mask_last_modified:
                v = "<MASKED-WALLCLOCK>"
            out.append(v)
        headers[name] = out

    body = resp.body
    wire_len = len(body)
    is_multirange = any("multipart/byteranges" in v
                        for v in resp.headers.get("content-type", []))
    if is_multirange:
        body = _mask_boundary_body(body)

    # Bodies are compared byte for byte, so they travel as base64 rather than as
    # a decoded string: tiny.png and big.bin are not text, and a lossy decode
    # would silently equate different bytes.
    #
    # `body_len` is the length AFTER masking, which is what the byte comparison
    # works on.  `wire_len` is what actually arrived, and the two differ by 150
    # bytes on the two-range case: Go's byteranges boundary is 60 hex characters
    # and `<BOUNDARY>` is ten, over three occurrences.  Both are kept because two
    # different questions are asked of them.  Comparing bodies needs the masked
    # length or every run disagrees; asking whether `Content-Length` told the
    # truth needs the wire length, and asking it about the masked body reports a
    # 552-byte header over a 402-byte body on a response that was correct --
    # measured, it failed a working port on exactly this case.
    return {
        "status": resp.status,
        "headers": headers,
        "header_names_in_order": [n for n, _ in resp.header_order],
        "body_b64": base64.b64encode(body).decode("ascii"),
        "body_len": len(body),
        "wire_len": wire_len,
    }


def body_of(rec: dict) -> bytes:
    return base64.b64decode(rec["body_b64"])


def describe(rec: dict, limit: int = 200) -> str:
    """A short human rendering, for failure messages."""
    body = body_of(rec)
    try:
        shown = body.decode("utf-8")
    except UnicodeDecodeError:
        shown = repr(body[:limit])
    if len(shown) > limit:
        shown = shown[:limit] + "..."
    hdrs = ", ".join(f"{k}={v[0]!r}" for k, v in sorted(rec["headers"].items())
                     if k not in VOLATILE_HEADERS)
    return f"{rec['status']} [{hdrs}] body={shown!r}"
