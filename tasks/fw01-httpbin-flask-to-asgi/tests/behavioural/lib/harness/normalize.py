"""Normalisation and comparison rules shared by capture and grading.

The whole behavioural suite rests on one idea: run the corpus against State A,
freeze the answer, replay it against State B, and compare *after applying the
same normalisation to both sides*. Everything that legitimately differs between
two HTTP servers is removed here, in one place, so the diff that remains is a
real behavioural difference and nothing else.

What is normalised, and why:

*   **host:port** -- the oracle and the submission listen on different ports.
    Any occurrence of the live authority in a body or a Location header becomes
    ``__HOST__``. Both sides are scrubbed with their own authority, so a
    correct submission matches regardless of the port it was reached on.
*   **The peer address** -- ``/ip`` and the ``origin`` field report the client
    address, which differs between the capture container and the verifier.
*   **Framing** -- ``Content-Length`` vs ``Transfer-Encoding``, ``Server``,
    ``Date``, ``Connection``: declared out of contract in instruction.md.
*   **Header name casing and order** -- compared as a case-insensitive
    multi-map with per-name value lists.

What is deliberately *not* normalised: JSON key order and indentation (the
contract says two-space, sorted, trailing newline), header *values*, status
codes, and every response byte outside the patterns above.
"""

from __future__ import annotations

import base64
import gzip
import json
import re
import zlib

HOST_PLACEHOLDER = "__HOST__"
IP_PLACEHOLDER = "__ORIGIN__"

#: Never compared: pure server-framing artefacts.
IGNORED_HEADERS = frozenset({
    "server",
    "date",
    "connection",
    "keep-alive",
    "transfer-encoding",
    "content-length",
    "alt-svc",
    "x-runtime",          # presence/format checked separately, value never
    "strict-transport-security",
})

#: Non-UTF-8 bodies are stored base64-encoded in the golden file.
_B64_PREFIX = "b64:"


# ---------------------------------------------------------------------------
# Serialisation of a captured response
# ---------------------------------------------------------------------------

def encode_body(raw: bytes) -> str:
    """Store a body as text when it is UTF-8, else as a tagged base64 blob."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return _B64_PREFIX + base64.b64encode(raw).decode("ascii")


def decode_body(stored: str) -> bytes:
    if stored.startswith(_B64_PREFIX):
        return base64.b64decode(stored[len(_B64_PREFIX):])
    return stored.encode("utf-8")


# ---------------------------------------------------------------------------
# Scrubbing
# ---------------------------------------------------------------------------

def _authority_variants(host: str, port: int):
    """Every spelling of this authority that can appear in a URL."""
    out = [f"{host}:{port}"]
    if host in ("127.0.0.1", "localhost"):
        out.append(f"localhost:{port}")
        out.append(f"127.0.0.1:{port}")
    return sorted(set(out), key=len, reverse=True)


def scrub_text(text: str, host: str, port: int, peer_ips=()) -> str:
    """Replace this run's authority and peer address with stable placeholders."""
    for authority in _authority_variants(host, port):
        text = text.replace(authority, HOST_PLACEHOLDER)
    for ip in peer_ips:
        if ip:
            text = text.replace(ip, IP_PLACEHOLDER)
    return text


#: Docker assigns container addresses out of a private range; the capture
#: container and the verifier container will not agree. Any private-range or
#: loopback literal in a response is therefore reduced to a placeholder.
_PRIVATE_IP_RE = re.compile(
    r"\b(?:"
    r"127\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|192\.168\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
    r")\b"
)


def scrub_ips(text: str) -> str:
    return _PRIVATE_IP_RE.sub(IP_PLACEHOLDER, text)


def scrub_body(raw: bytes, host: str, port: int) -> bytes:
    """Scrub a body for comparison. Non-text bodies are returned untouched."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw
    text = scrub_text(text, host, port)
    text = scrub_ips(text)
    return text.encode("utf-8")


def scrub_header_value(name: str, value: str, host: str, port: int) -> str:
    value = scrub_text(value, host, port)
    if name in ("location", "content-location", "link", "refresh"):
        value = scrub_ips(value)
    return value


# ---------------------------------------------------------------------------
# Header multi-map
# ---------------------------------------------------------------------------

def canonical_header_value(name: str, value: str) -> str:
    """Canonicalise a set-valued header so member order stops mattering.

    ``Allow`` and ``Access-Control-Allow-Methods`` are built from Python sets in
    the original app, so their member order varies between runs of the *same*
    server. The members are contractual; the order is not.
    """
    from harness.corpus import SET_VALUED_HEADERS

    if name in SET_VALUED_HEADERS:
        members = sorted(p.strip() for p in value.split(",") if p.strip())
        return ", ".join(members)
    return value


def header_map(pairs, host: str, port: int) -> dict:
    """Case-insensitive name -> list-of-values, scrubbed, framing removed.

    Values are kept as an ordered list so that a route emitting two
    ``Set-Cookie`` headers is distinguishable from one emitting a single
    comma-joined header -- a difference that matters to real clients.
    """
    out: dict[str, list[str]] = {}
    for name, value in pairs:
        low = name.lower()
        if low in IGNORED_HEADERS:
            continue
        scrubbed = scrub_header_value(low, value, host, port)
        out.setdefault(low, []).append(canonical_header_value(low, scrubbed))
    return out


def compared_header_names(case) -> list[str]:
    """Which response headers this case compares by value."""
    from harness.corpus import SEMANTIC_HEADERS

    names = set(SEMANTIC_HEADERS) | set(case.get("headers_extra", ()))
    names -= set(case.get("headers_skip", ()))
    names -= IGNORED_HEADERS
    return sorted(names)


# ---------------------------------------------------------------------------
# Body decoding for the compressed endpoints
# ---------------------------------------------------------------------------

def decompress(mode: str, raw: bytes) -> bytes:
    if mode == "gzip-json":
        return gzip.decompress(raw)
    if mode == "deflate-json":
        # httpbin uses a raw zlib stream (compressobj defaults), so the header
        # is present; wbits=15 handles it, and -15 is the raw fallback.
        try:
            return zlib.decompress(raw)
        except zlib.error:
            return zlib.decompress(raw, -15)
    if mode == "br-json":
        try:
            import brotlicffi as _br
        except ImportError:  # pragma: no cover - the verifier ships brotlicffi
            import brotli as _br
        return _br.decompress(raw)
    raise ValueError(f"not a compressed mode: {mode}")


# ---------------------------------------------------------------------------
# JSON comparison
# ---------------------------------------------------------------------------

#: Fields inside an httpbin JSON payload whose value is inherently per-request.
VOLATILE_JSON_KEYS = ("origin",)


def normalise_json(obj):
    """Reduce volatile leaf values inside a parsed httpbin payload."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in VOLATILE_JSON_KEYS and isinstance(v, str):
                out[k] = IP_PLACEHOLDER
            else:
                out[k] = normalise_json(v)
        return out
    if isinstance(obj, list):
        return [normalise_json(v) for v in obj]
    return obj


def parse_json_body(raw: bytes):
    return normalise_json(json.loads(raw.decode("utf-8")))


# ---------------------------------------------------------------------------
# The JSON serialisation contract
# ---------------------------------------------------------------------------

def is_httpbin_json(raw: bytes) -> bool:
    text = raw.lstrip()
    return text[:1] in (b"{", b"[")


def json_style_violations(raw: bytes) -> list[str]:
    """Check the pretty-printing contract: 2-space indent, sorted keys, \\n end.

    Returns a list of human-readable violations; empty means conforming.
    """
    problems = []
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return ["body is not UTF-8"]

    if not text.endswith("\n"):
        problems.append("does not end with a newline")

    try:
        data = json.loads(text)
    except ValueError as exc:
        return [f"not valid JSON: {exc}"]

    expected = json.dumps(data, sort_keys=True, indent=2) + "\n"
    if text != expected:
        # Narrow the diagnosis so a failure is actionable.
        if json.dumps(data, sort_keys=True, indent=2) != text.rstrip("\n"):
            compact = json.dumps(data, sort_keys=True, separators=(",", ": "))
            if text.rstrip("\n") == compact:
                problems.append("serialised compactly, expected indent=2")
            elif json.dumps(data, indent=2) == text.rstrip("\n"):
                problems.append("keys not sorted")
            elif json.dumps(data, sort_keys=True, indent=4) == text.rstrip("\n"):
                problems.append("indent=4, expected indent=2")
            else:
                problems.append("serialisation differs from sort_keys=True, indent=2")
    return problems


# ---------------------------------------------------------------------------
# Whole-response capture record
# ---------------------------------------------------------------------------

def record(case, status, headers, raw_body, host, port) -> dict:
    """Build the frozen record for one case."""
    return {
        "id": case["id"],
        "status": status,
        "headers": header_map(headers, host, port),
        "raw_headers": [[n.lower(), v] for n, v in headers],
        "body": encode_body(scrub_body(raw_body, host, port)),
        "body_len": len(raw_body),
        "has_x_runtime": any(n.lower() == "x-runtime" for n, _ in headers),
        "has_content_length": any(
            n.lower() == "content-length" for n, _ in headers
        ),
    }
