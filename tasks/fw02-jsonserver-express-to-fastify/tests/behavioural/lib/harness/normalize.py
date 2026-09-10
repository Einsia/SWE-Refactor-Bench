"""Turning a live response into something two runs can be compared on.

Three classes of variation have to be removed before State A and State B can be
compared at all, and each is removed for a stated reason:

1.  **Per-process facts.** ``Date`` moves, ``Connection`` is a transport
    detail, and ``Content-Length`` is a function of the body we already compare
    byte for byte. These are dropped, never compared.

2.  **Bound address.** ``Location`` and ``Link`` embed the host and port the
    server happens to be listening on. The port is assigned per boot, so both
    are rewritten to a fixed placeholder before comparison. The *structure* of
    the header -- how many rels, in what order, with what query strings -- is
    what the task grades, and that survives the rewrite.

3.  **Generated identifiers.** ``nanoid`` ids and the mtime-derived static
    ``ETag``/``Last-Modified`` differ between two runs of *State A itself*.
    Anything in this class is discovered empirically by ``capture.py``, which
    replays the corpus against two independently started oracles and records
    every field that disagreed. Those fields are then graded on shape, not
    value. Nothing is put on that list by assumption.

``X-Powered-By`` is deliberately *not* ignored: State A emits it because Express
does, State B must not, and that difference is a legitimate observable.
"""

from __future__ import annotations

import gzip
import json
import re
import zlib

#: Dropped from every comparison: transport and timing facts with no contract.
IGNORED_HEADERS = frozenset({
    "date",
    "connection",
    "keep-alive",
    "transfer-encoding",
    "content-length",
    "alt-svc",
})

#: Headers whose value is a set of case-insensitive tokens rather than a
#: sequence, so neither ordering nor token casing is part of the contract: both
#: sides are lowercased and sorted before comparison. Every member here is a
#: header whose tokens name other headers, methods or directives, all of which
#: HTTP defines case-insensitively -- ``Vary: accept-encoding`` and
#: ``Vary: Accept-Encoding`` request the same behaviour from every cache.
SET_VALUED_HEADERS = frozenset({
    "vary",
    "access-control-allow-methods",
    "access-control-allow-headers",
    "access-control-expose-headers",
    "allow",
    "cache-control",
})

PLACEHOLDER_HOST = "http://HOST:PORT"

_ADDR = re.compile(r"http://(?:127\.0\.0\.1|localhost|\[::1\]|0\.0\.0\.0)(?::\d+)?")
_TMPPATH = re.compile(r"/tmp/[A-Za-z0-9._-]+")
_WORKSPACE = re.compile(r"/workspace/[A-Za-z0-9._/-]+")
_OPT = re.compile(r"/opt/[A-Za-z0-9._/-]+")


def scrub_text(text: str, prefixes: tuple[str, ...] = ()) -> str:
    """Remove bound-address and filesystem details from a textual value.

    ``prefixes`` are absolute directories belonging to whichever side is being
    measured -- the checkout under test, and the scratch directory its databases
    live in. Two responses legitimately embed a stack trace, and a stack trace
    names the file that raised it, so without this the comparison would be
    between State A's build path and the submission's. Longest first, because one
    prefix can contain another.
    """
    for prefix in sorted(prefixes, key=len, reverse=True):
        if prefix:
            text = text.replace(prefix, "/PATH")
    text = _ADDR.sub(PLACEHOLDER_HOST, text)
    text = _TMPPATH.sub("/tmp/PATH", text)
    text = _WORKSPACE.sub("/PATH", text)
    text = _OPT.sub("/PATH", text)
    return text


def scrub_header_value(name: str, value: str,
                       prefixes: tuple[str, ...] = ()) -> str:
    lower = name.lower()
    value = scrub_text(value, prefixes)
    if lower in SET_VALUED_HEADERS:
        parts = [p.strip().lower() for p in value.split(",") if p.strip()]
        return ", ".join(sorted(parts))
    return value


def header_map(raw_headers: list[tuple[str, str]],
               prefixes: tuple[str, ...] = ()) -> dict[str, list[str]]:
    """Case-insensitive name -> list of scrubbed values, ignored names removed.

    A list, not a string: duplicate ``Set-Cookie``-style headers and repeated
    ``Vary`` are both real, and collapsing them would hide a difference.
    """
    out: dict[str, list[str]] = {}
    for name, value in raw_headers:
        lower = name.lower()
        if lower in IGNORED_HEADERS:
            continue
        out.setdefault(lower, []).append(
            scrub_header_value(lower, value, prefixes))
    for values in out.values():
        values.sort()
    return out


def decompress(body: bytes, scheme: str) -> bytes:
    if scheme == "gzip":
        return gzip.decompress(body)
    if scheme == "deflate":
        # Express's compression emits a zlib stream for `deflate`.
        try:
            return zlib.decompress(body)
        except zlib.error:
            return zlib.decompress(body, -zlib.MAX_WBITS)
    raise ValueError(f"unsupported scheme {scheme}")


def scrub_body(body: bytes, prefixes: tuple[str, ...] = ()) -> str:
    """Decode and scrub a body for comparison, keeping byte-level fidelity.

    JSON is *not* reserialised: indentation and key order are part of what the
    task reproduces (State A sets ``json spaces = 2``), so the text is compared
    as text.
    """
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return "<<binary:%d bytes>>" % len(body)
    return scrub_text(text, prefixes)


#: A stack frame line, in either of the two forms a body can carry it: plain
#: text (``\n    at fn (file:line:col)``) or Express's HTML error page, which
#: escapes the same lines into ``<br> &nbsp; &nbsp;at ...``.
_FRAME_TEXT = re.compile(r"\n[ \t]+at [^\n]*")
_FRAME_HTML = re.compile(r"<br>(?:\s|&nbsp;)*at [^<\n]*")


def strip_stack_frames(text: str) -> tuple[str, int]:
    """Remove stack frame lines, returning the remainder and how many were cut.

    Six responses in the corpus embed a stack. The frames name Express's own
    internals and Node's, so a Fastify port cannot reproduce them and must not
    be asked to. Everything *around* the frames is contract: the status, the
    error class, the message, and -- for the four that render Express's HTML
    error page -- the page itself. Cutting only the frames leaves that
    comparable byte for byte, which is far stronger than searching the body for
    a substring. The count is kept so a submission that reports an error with no
    stack at all is still distinguishable from one that reports it with a stack.
    """
    stripped, html_n = _FRAME_HTML.subn("", text)
    stripped, text_n = _FRAME_TEXT.subn("", stripped)
    return stripped, html_n + text_n


def json_shape(value, depth: int = 0):
    """A structural fingerprint: types and keys, no leaf values.

    Used for the handful of responses whose bodies legitimately differ between
    runs (generated ids, embedded stacks). It still catches a wrong array
    length, a missing key, or a string where a number belonged.
    """
    if depth > 12:
        return "..."
    if isinstance(value, dict):
        return {k: json_shape(v, depth + 1) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [json_shape(v, depth + 1) for v in value]
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    return "string"


#: Keys whose values are generated per run and therefore compared by shape.
GENERATED_KEYS = frozenset({"id", "_id"})


def json_with_generated_masked(value, id_keys=GENERATED_KEYS, depth: int = 0):
    """Deep copy with generated identifier values replaced by their type.

    A created record's ``id`` is ``nanoid(7)`` when the collection's ids are not
    numeric, so its value cannot be compared -- but that it is a 7-character
    string, and that everything around it matches exactly, can be.
    """
    if depth > 12:
        return value
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k in id_keys and isinstance(v, str):
                out[k] = f"<<generated:str:{len(v)}>>"
            else:
                out[k] = json_with_generated_masked(v, id_keys, depth + 1)
        return out
    if isinstance(value, list):
        return [json_with_generated_masked(v, id_keys, depth + 1) for v in value]
    return value


def json_style_violations(text: str) -> list[str]:
    """Ways a JSON body can differ from State A's serialisation style.

    State A configures Express with ``json spaces = 2``. Fastify defaults to
    compact output, so this is one of the differences a migration has to
    notice, and it is checked directly rather than only implied by a byte diff.
    """
    problems = []
    stripped = text.strip()
    if not stripped or stripped[0] not in "[{":
        return problems
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        return [f"not valid JSON: {exc}"]
    if isinstance(parsed, (dict, list)) and parsed:
        if "\n" not in text:
            problems.append("serialised on one line; State A indents by 2")
        else:
            for line in text.split("\n")[1:]:
                if line.strip() and not line.startswith(" "):
                    continue
                if line.startswith(" ") and not line.startswith("  "):
                    problems.append(f"indent is not a multiple of 2: {line!r}")
                    break
            if "\t" in text:
                problems.append("indented with tabs; State A uses spaces")
    if text.endswith("\n"):
        problems.append("trailing newline; State A emits none")
    return problems


def record(case_id: str, status: int, raw_headers: list[tuple[str, str]],
           body: bytes, decompress_scheme: str | None = None,
           prefixes: tuple[str, ...] = ()) -> dict:
    """Freeze one response into the comparable form stored in the golden file.

    ``prefixes`` are absolute directories belonging to the side being measured
    (its checkout, its scratch database directory). Scrubbing them keeps a
    stack trace's file path comparable between the oracle and a submission --
    and keeps the build machine's own paths out of the shipped golden file.
    """
    raw = body
    decompressed_ok = None
    if decompress_scheme:
        try:
            body = decompress(body, decompress_scheme)
            decompressed_ok = True
        except Exception:
            decompressed_ok = False

    headers = header_map(raw_headers, prefixes)
    text = scrub_body(body, prefixes)
    entry = {
        "id": case_id,
        "status": status,
        "headers": headers,
        "header_names": sorted(headers),
        "body": text,
        "body_len": len(body),
        "raw_len": len(raw),
        "decompressed": decompressed_ok,
        "has_x_powered_by": "x-powered-by" in headers,
        "json_style_violations": json_style_violations(text),
    }
    entry["body_no_frames"], entry["stack_frames"] = strip_stack_frames(text)
    try:
        parsed = json.loads(text)
    except ValueError:
        entry["json"] = None
        entry["json_shape"] = None
        entry["json_masked"] = None
    else:
        entry["json"] = parsed
        entry["json_shape"] = json_shape(parsed)
        entry["json_masked"] = json_with_generated_masked(parsed)
    return entry
