#!/usr/bin/env python3
"""The reference half of the probe pair.

This program imports the pinned Python sqlparse 0.5.3 that ships inside the
verifier image and answers the catalog's operations.  Its Go counterpart,
probe.go, imports the submission's packages and answers the same operations.
Neither half may carry an operation the other cannot answer: the pair is a
matched pair, and an operation only one side implements is not a differential
test, it is a trick question.

Protocol.  One request per line on stdin:

    ID \t OP \t ARG \t ARG ...

One response per request on stdout, in request order:

    ID \t STATUS \t BASE64

STATUS is `ok` or `err`.  BASE64 is the answer, base64 of raw bytes with no
newlines.  Base64 because answers contain newlines, tabs, invalid UTF-8 and NUL,
and because a transport that mangles any of those would silently convert a real
difference into a pass.  Errors are answers too: a submission that raises where
the reference raises, with the same message, is correct, so `err` plus the
message is compared exactly like `ok` plus the output.

Every answer is a deliberate rendering, never repr() and never pprint().  A
rendering that leaked a Python object address, a dict ordering or a float
formatting decision would be unportable by construction, and the Go side would
be graded against something no Go program could emit.  So each op below defines
a line-oriented, ASCII-framed format, and both halves emit exactly that.
"""

from __future__ import annotations

import base64
import io
import json
import os
import sys
import traceback
from pathlib import Path

PROBE_VERSION = "swerefactor-lang03-probe-v1"

# ---------------------------------------------------------------------------
# Argument decoding.  Mirrors catalog.Catalog.{d,e,s,i,b,n}.
# ---------------------------------------------------------------------------


class Args:
    """Decoded arguments for one request."""

    def __init__(self, ctx: "Context", raw: list[str]) -> None:
        self.ctx = ctx
        self.raw = raw

    def __len__(self) -> int:
        return len(self.raw)

    def _tag(self, idx: int) -> tuple[str, str]:
        item = self.raw[idx]
        tag, _, rest = item.partition(":")
        return tag, rest

    def doc(self, idx: int = 0) -> bytes:
        """d:ID -- the document's raw bytes."""
        tag, rest = self._tag(idx)
        if tag not in ("d", "e"):
            raise ProbeError(f"arg {idx} is {tag}:, expected d: or e:")
        return self.ctx.doc_bytes(rest)

    def doc_id(self, idx: int = 0) -> str:
        return self._tag(idx)[1]

    def doc_encoding(self, idx: int = 0) -> str | None:
        """e:ID -- the declared encoding, canonicalized, or None.

        The gate is applied here rather than in each op, so no op can forget it.
        A declared encoding outside the supported set raises, and the raise is
        the answer: the reference would happily decode gbk, a stdlib-only Go
        port cannot, and grading CPython's codec registry is not grading a port.
        """
        tag, rest = self._tag(idx)
        if tag != "e":
            raise ProbeError(f"arg {idx} is {tag}:, expected e:")
        declared = self.ctx.doc_encoding(rest)
        if declared is None:
            return None
        return self.ctx.canonical_encoding(declared)

    def text(self, idx: int = 0) -> str:
        tag, rest = self._tag(idx)
        if tag != "s":
            raise ProbeError(f"arg {idx} is {tag}:, expected s:")
        return unescape(rest)

    def num(self, idx: int) -> int:
        tag, rest = self._tag(idx)
        if tag != "i":
            raise ProbeError(f"arg {idx} is {tag}:, expected i:")
        return int(rest)

    def flag(self, idx: int) -> bool:
        tag, rest = self._tag(idx)
        if tag != "b":
            raise ProbeError(f"arg {idx} is {tag}:, expected b:")
        return rest == "1"

    def name(self, idx: int = 0) -> str:
        tag, rest = self._tag(idx)
        if tag != "n":
            raise ProbeError(f"arg {idx} is {tag}:, expected n:")
        return rest


def unescape(text: str) -> str:
    """Inverse of catalog.Catalog.s."""
    out: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        i += 1
        if i >= len(text):
            raise ProbeError("trailing backslash in s: argument")
        esc = text[i]
        i += 1
        if esc == "n":
            out.append("\n")
        elif esc == "t":
            out.append("\t")
        elif esc == "r":
            out.append("\r")
        elif esc == "\\":
            out.append("\\")
        elif esc == "x":
            out.append(chr(int(text[i:i + 2], 16)))
            i += 2
        else:
            raise ProbeError(f"unknown escape \\{esc}")
    return "".join(out)


class ProbeError(Exception):
    """A defect in the request, not a difference in behavior.

    Distinguished from the reference raising: if the harness sends a malformed
    request, both halves would answer `err` with the same text and the case
    would pass while measuring nothing.  These are reported with a marker the
    executor treats as a verifier defect.
    """


class UnsupportedEncoding(LookupError):
    """A declared encoding outside the set the contract publishes.

    Derived from LookupError so it lands in `_error_category` as
    `unknown-encoding` with no special case: that is exactly the category
    CPython would produce for a codec it does not have, and it is what the Go
    side produces for a codec it was never asked to implement.  The distinction
    the pair grades is "the encoding is not known" versus "the bytes are not
    valid in it", and both halves reach it the same way.
    """


# ---------------------------------------------------------------------------
# Rendering helpers.  Every op's answer is built from these, so both halves
# have one framing to agree on rather than one per op.
# ---------------------------------------------------------------------------


def enc_field(text: str) -> str:
    """A single field: no tabs, no newlines, no ambiguity.

    Answers embed arbitrary token values, which contain tabs, newlines and lone
    surrogates.  Escaping them keeps one record on one line, and keeps the
    rendering identical on both sides: Go would otherwise have to decide how to
    print a byte that is not valid UTF-8, and Python how to print a surrogate.
    """
    out: list[str] = []
    for ch in text:
        code = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif code < 0x20 or code == 0x7F:
            out.append(f"\\x{code:02x}")
        elif 0xD800 <= code <= 0xDFFF:
            # A lone surrogate.  Python can hold one, Go cannot; rendering it
            # explicitly means both sides can say the same thing about it.
            out.append(f"\\u{code:04x}")
        else:
            out.append(ch)
    return "".join(out)


def enc_bytes(data: bytes) -> str:
    """Raw bytes as a hex-escaped field, for the encoding ops."""
    return "".join(
        chr(b) if 0x20 <= b < 0x7F and b not in (0x5C,) else f"\\x{b:02x}"
        for b in data
    )


def lines(*items: str) -> bytes:
    """Join records with LF and terminate.  Empty input is a single LF."""
    return ("\n".join(items) + "\n").encode("utf-8", "surrogatepass")


class Context:
    """Everything an op needs that is not in its arguments."""

    def __init__(self, docs_dir: Path, docs: dict, spec: dict) -> None:
        self.docs_dir = docs_dir
        self.docs = docs
        self.spec = spec
        self.doc_meta = {d["id"]: d for d in docs["documents"]}
        self._cache: dict[str, bytes] = {}

    def doc_bytes(self, doc_id: str) -> bytes:
        if doc_id not in self._cache:
            if doc_id not in self.doc_meta:
                raise ProbeError(f"unknown document: {doc_id}")
            self._cache[doc_id] = (self.docs_dir / doc_id).read_bytes()
        return self._cache[doc_id]

    def doc_encoding(self, doc_id: str) -> str | None:
        if doc_id not in self.doc_meta:
            raise ProbeError(f"unknown document: {doc_id}")
        return self.doc_meta[doc_id]["encoding"] or None

    def accessor_applies(self, label: str, kind: str) -> bool:
        """Whether a node of `kind` answers the accessor named `label`.

        The scope table is data in spec.json for the same reason the alias table
        is: the reference spreads its read surface over a class hierarchy that
        the contracted Go *Node -- one struct, every method callable -- cannot
        reproduce by asking.  Both halves consult this table and both render
        `no-method` for the pairs it excludes.
        """
        scope = self.spec["accessor_scope"].get(label)
        if scope is None:
            raise ProbeError(f"accessor {label!r} has no scope in the spec")
        if scope == "any":
            return True
        if scope == "group":
            return kind not in self.spec["non_group_kinds"]
        if scope not in self.spec["node_kinds"]:
            raise ProbeError(
                f"accessor {label!r} is scoped to unknown kind {scope!r}")
        return scope == kind

    def canonical_encoding(self, name: str) -> str:
        """Apply the published encoding gate, or raise.

        The alias table is data in spec.json, so both halves normalize with the
        same table rather than each reimplementing an alias machinery.
        """
        aliases = self.spec["encoding_aliases"]
        canonical = aliases.get(name.strip().lower(), "")
        if not canonical:
            raise UnsupportedEncoding(f"unsupported encoding: {name}")
        return canonical

    def doc_text(self, doc_id: str) -> str:
        """The document as a caller holding a str would have it.

        Strict UTF-8, deliberately.  The earlier version decoded with
        surrogateescape so that any document could reach the str entry points,
        but a lone surrogate is something Python can hold and Go cannot: encoded
        as WTF-8 it is invalid UTF-8, so Go's regexp sees replacement characters
        where Python's `re` sees word characters, and every token boundary and
        every length downstream diverges.  The pair would then fail a port that
        was correct.

        A document that is not valid UTF-8 reaches the library through the bytes
        entry points instead, where sqlparse's own UTF-8-then-unicode-escape
        fallback is the graded behavior.  The catalog is responsible for not
        routing one here; this raise is the assertion that it did not.
        """
        try:
            return self.doc_bytes(doc_id).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProbeError(
                f"document {doc_id} is not valid UTF-8 and cannot be used on "
                f"a str entry point: {exc}"
            ) from exc

    def doc_source(self, doc_id: str) -> str:
        """The document as the library itself would decode it from bytes.

        This is `Lexer.get_tokens`' own rule for bytes with no declared encoding
        (lexer.py: try utf-8, fall back to unicode-escape), reproduced here
        because the round-trip cases need the decoded source to compare the
        re-rendered statements against.  It is not a convenience: it is graded
        behavior, so the Go side implements the same two-step and the pair is
        comparing two implementations of one published rule.

        `unicode-escape` is latin-1 over the bytes plus Python's escape grammar
        -- \\xNN, \\NNN octal, \\uXXXX, \\UXXXXXXXX, \\a\\b\\f\\v\\n\\r\\t\\0,
        \\' \\" \\\\, and an unrecognized escape kept literally as backslash
        plus character.  All of that is portable.  The one construct that is not
        is \\N{UNICODE NAME}, which needs the Unicode name database; the
        contract excludes it and no docs document on this path contains it.
        """
        data = self.doc_bytes(doc_id)
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return data.decode("unicode-escape")

    def preset(self, name: str) -> dict:
        table = self.spec["format_presets"]
        if name not in table:
            raise ProbeError(f"unknown format preset: {name}")
        return decode_options(table[name])

    def validate_case(self, name: str) -> dict:
        table = self.spec["validate_cases"]
        if name not in table:
            raise ProbeError(f"unknown validate case: {name}")
        return decode_options(table[name])


def decode_option(tagged):
    """Inverse of spec.I/S/B/N/F."""
    if not isinstance(tagged, list) or len(tagged) != 2:
        raise ProbeError(f"malformed tagged option: {tagged!r}")
    tag, value = tagged
    if tag == "i":
        return int(value)
    if tag == "s":
        return str(value)
    if tag == "b":
        return bool(value)
    if tag == "n":
        return None
    if tag == "f":
        return float(value)
    raise ProbeError(f"unknown option tag: {tag!r}")


def decode_options(mapping: dict) -> dict:
    return {k: decode_option(v) for k, v in mapping.items()}


# ---------------------------------------------------------------------------
# The operations.  Grouped by tier, in the same order as the catalog, so the
# two files can be read side by side.
# ---------------------------------------------------------------------------

OPS: dict[str, object] = {}


def op(name: str):
    def register(fn):
        if name in OPS:
            raise RuntimeError(f"duplicate op: {name}")
        OPS[name] = fn
        return fn
    return register


# -- core tier --------------------------------------------------------------


@op("version")
def op_version(ctx: Context, a: Args) -> bytes:
    import sqlparse

    return lines(f"version\t{enc_field(sqlparse.__version__)}")


@op("rt")
def op_rt(ctx: Context, a: Args) -> bytes:
    """Round trip: parse then concatenate, and report whether it is lossless.

    Reported as the statement count, each statement's serialization, and the
    verdict, rather than a bare boolean.  A submission whose round trip is lossy
    should fail on the statement that differs, and a bare boolean would not say
    which one.
    """
    import sqlparse

    doc_id = a.doc_id(0)
    encoding = a.doc_encoding(0)
    # Both branches hand the library bytes, which is what a caller reading a
    # file does.  The undeclared branch deliberately does not pre-decode: the
    # library's own utf-8-then-unicode-escape fallback is the behavior under
    # test, and decoding here would hide it and would put a str the Go side
    # cannot construct on the wire.
    if encoding:
        statements = sqlparse.parse(ctx.doc_bytes(doc_id), encoding)
        source = ctx.doc_bytes(doc_id).decode(encoding)
    else:
        statements = sqlparse.parse(ctx.doc_bytes(doc_id))
        source = ctx.doc_source(doc_id)
    out = [f"count\t{len(statements)}"]
    joined = []
    for idx, stmt in enumerate(statements):
        text = str(stmt)
        joined.append(text)
        out.append(f"stmt\t{idx}\t{len(text)}\t{enc_field(text)}")
    total = "".join(joined)
    out.append(f"lossless\t{1 if total == source else 0}")
    if total != source:
        out.append(f"joined\t{enc_field(total)}")
    return lines(*out)


@op("rt-raw")
def op_rt_raw(ctx: Context, a: Args) -> bytes:
    """Round trip over bytes with no declared encoding.

    This is the fallback path: sqlparse tries UTF-8 and then unicode-escape, and
    which one it lands on is observable in the token values.
    """
    import sqlparse

    data = ctx.doc_bytes(a.doc_id(0))
    statements = sqlparse.parse(data)
    out = [f"count\t{len(statements)}"]
    for idx, stmt in enumerate(statements):
        text = str(stmt)
        out.append(f"stmt\t{idx}\t{len(text)}\t{enc_field(text)}")
    return lines(*out)


@op("rt-enc")
def op_rt_enc(ctx: Context, a: Args) -> bytes:
    """Round trip under an encoding named by the caller, not by the document.

    `rt` and `rt-raw` between them exercise the encodings the docs declares --
    cp1251, utf-8, utf-16 and the no-declaration fallback -- and nothing else.
    Five of the eight encodings the gate accepts (ascii, latin-1, utf-16-le,
    utf-16-be, utf-8-sig) are never applied to bytes by any other op, and 33 of
    the 37 published aliases are never named at all.  Both are contract: the
    published set says a port must decode these, and the alias table says
    `L1` and `iso8859_1` reach the same codec.  This op grades that.

    Two failures are distinguished, because a port can get either one wrong on
    its own: the gate rejecting a name it should accept (`unknown-encoding`) and
    the codec rejecting bytes it should reject (`undecodable`).  A single
    "it errored" answer would let a port that rejects every name it does not
    recognize pass the cases where the bytes were undecodable anyway.

    The two halves resolve the alias at different places on purpose.  This half
    resolves it through `ctx.canonical_encoding`, which is the published gate,
    and hands the library a canonical name -- because CPython's own registry
    accepts about a hundred codecs and would decode `gbk` happily, which is
    exactly what the Go half must refuse.  The Go half hands the name through
    unresolved and the *submission* resolves it.  So an accepted alias agrees
    only if the submission's table agrees with the published one, and that is
    the thing being graded.

    Three fields the reference could easily report are deliberately not
    reported: `canonical`, the decoded `source`, and `lossless`.  No contracted
    API exposes an encoding resolution or a decode entry point, so the Go half
    can produce none of the three, and a case whose answer is a line only the
    oracle can write is a case that fails every correct port.  Adding a decode
    entry point to the public surface to close the gap would be growing the API
    for the grader's convenience, which the closed-world surface exists to
    prevent.

    Nothing is lost by the trim.  What a resolution is *for* is the text it
    produces, and the per-statement text is reported: the catalog pairs every
    alias with documents whose bytes the eight declared codecs decode
    differently, so a name resolved to the wrong codec disagrees on `stmt`
    rather than on a `canonical` line.  That was measured, not assumed -- all
    eight canonical codecs are mutually distinguishable on the four documents
    the alias cases use.
    """
    import sqlparse

    doc_id = a.doc_id(0)
    name = a.text(1)
    try:
        canonical = ctx.canonical_encoding(name)
        data = ctx.doc_bytes(doc_id)
        statements = sqlparse.parse(data, canonical)
    except Exception as exc:  # noqa: BLE001 -- the failure is the answer
        return lines("status\terr", f"category\t{_error_category(exc)}")
    out = ["status\tok", f"count\t{len(statements)}"]
    joined = []
    for idx, stmt in enumerate(statements):
        text = str(stmt)
        joined.append(text)
        out.append(f"stmt\t{idx}\t{len(text)}\t{enc_field(text)}")
    out.append(f"len\t{len(''.join(joined))}")
    return lines(*out)


@op("tostr")
def op_tostr(ctx: Context, a: Args) -> bytes:
    """str() over each parsed statement, plus its length in code points.

    Separate from `rt` because the length is the part that diverges: Python
    counts code points and Go counts bytes, and a port that returns byte
    lengths passes every round-trip case and fails here.
    """
    import sqlparse

    doc_id = a.doc_id(0)
    encoding = a.doc_encoding(0)
    if encoding:
        statements = sqlparse.parse(ctx.doc_bytes(doc_id), encoding)
    else:
        # Bytes, not pre-decoded text: the library owns the fallback.
        statements = sqlparse.parse(ctx.doc_bytes(doc_id))
    out = []
    for idx, stmt in enumerate(statements):
        text = str(stmt)
        out.append(
            f"len\t{idx}\t{len(text)}\t{len(text.encode('utf-8', 'surrogatepass'))}"
        )
    return lines(*out) if out else lines("len\tnone")


@op("split")
def op_split(ctx: Context, a: Args) -> bytes:
    import sqlparse

    doc_id = a.doc_id(0)
    encoding = a.doc_encoding(0)
    if encoding:
        parts = sqlparse.split(ctx.doc_bytes(doc_id), encoding)
    else:
        parts = sqlparse.split(ctx.doc_bytes(doc_id))
    out = [f"count\t{len(parts)}"]
    for idx, part in enumerate(parts):
        out.append(f"part\t{idx}\t{len(part)}\t{enc_field(part)}")
    return lines(*out)


@op("split-strip")
def op_split_strip(ctx: Context, a: Args) -> bytes:
    """split with strip_semicolon=True, over bytes with no declared encoding."""
    import sqlparse

    data = ctx.doc_bytes(a.doc_id(0))
    parts = sqlparse.split(data, strip_semicolon=True)
    out = [f"count\t{len(parts)}"]
    for idx, part in enumerate(parts):
        out.append(f"part\t{idx}\t{len(part)}\t{enc_field(part)}")
    return lines(*out)


@op("parsestream")
def op_parsestream(ctx: Context, a: Args) -> bytes:
    """parsestream over a file-like object.

    The generator path.  It is the same grouping machinery, reached through a
    reader rather than a string, and a port that only implements the string
    entry point fails here without failing anything else.
    """
    import sqlparse

    doc_id = a.doc_id(0)
    encoding = a.doc_encoding(0)
    if encoding:
        # The reference's lexer accepts a TextIOBase, a str or bytes, and
        # nothing else -- a raw BytesIO is a TypeError.  A Python caller with a
        # binary file therefore wraps it, and the wrapper is where the declared
        # encoding is applied.  Go's ParseStream takes an io.Reader and applies
        # the encoding itself, so wrapping here is what makes the pair matched:
        # both halves decode the same bytes with the same encoding and group the
        # same text.  Handing the Python half a BytesIO instead would grade a
        # Python type check that has no Go counterpart.
        stream = io.TextIOWrapper(io.BytesIO(ctx.doc_bytes(doc_id)),
                                  encoding=encoding, newline="")
        statements = list(sqlparse.parsestream(stream, encoding))
    else:
        stream = io.StringIO(ctx.doc_text(doc_id), newline="")
        statements = list(sqlparse.parsestream(stream))
    out = [f"count\t{len(statements)}"]
    for idx, stmt in enumerate(statements):
        out.append(
            f"stmt\t{idx}\t{enc_field(str(stmt.get_type()))}"
            f"\t{enc_field(str(stmt))}"
        )
    return lines(*out)


@op("fmt")
def op_fmt(ctx: Context, a: Args) -> bytes:
    """format() under one named preset.

    The answer is the output, or the exception type and message.  Both are
    contractual: several presets are valid option maps that the formatter
    rejects for a document-independent reason, and the message is what a caller
    sees.
    """
    import sqlparse

    options = ctx.preset(a.name(0))
    doc_id = a.doc_id(1)
    encoding = a.doc_encoding(1)
    try:
        if encoding:
            result = sqlparse.format(ctx.doc_bytes(doc_id), encoding, **options)
        else:
            result = sqlparse.format(ctx.doc_bytes(doc_id), **options)
    except Exception as exc:  # noqa: BLE001 -- the exception is the answer
        return lines("status\terr", *error_lines(exc))
    return lines(
        "status\tok",
        f"len\t{len(result)}",
        f"out\t{enc_field(result)}",
    )


@op("validate")
def op_validate(ctx: Context, a: Args) -> bytes:
    """validate_options: accepted, or the exact rejection message.

    The normalized map itself is not reported.  It holds filter instances and
    other Python objects with no portable rendering, and the contract for a
    valid map is that it is accepted -- what it normalizes to is observable
    through `fmt`, which grades the output the map produces.
    """
    from sqlparse import formatter

    options = ctx.validate_case(a.name(0))
    try:
        formatter.validate_options(dict(options))
    except Exception as exc:  # noqa: BLE001
        return lines("status\terr", *error_lines(exc))
    return lines("status\tok")


@op("err-encoding")
def op_err_encoding(ctx: Context, a: Args) -> bytes:
    """The error surface of a bad encoding name.

    sqlparse does not validate encoding names; it hands them to bytes.decode,
    so the failure is Python's LookupError with Python's message.  What is
    graded is that a failure occurs and which of the two shapes it has, not the
    interpreter's wording -- so the message is reported truncated to its first
    token, which is stable across implementations that fail for the same reason.
    """
    import sqlparse

    data = ctx.doc_bytes(a.doc_id(0))
    encoding = a.text(1)
    try:
        # The gate first, and it is not a formality: CPython would decode gbk,
        # koi8-r and another 90-odd codecs that a stdlib-only Go port has no
        # table for.  Sending the raw name to the reference here would grade
        # CPython's codec registry, and every such case would be one the target
        # half cannot pass however good the port is.
        canonical = ctx.canonical_encoding(encoding)
        sqlparse.parse(data, canonical)
    except Exception as exc:  # noqa: BLE001
        return lines("status\terr", f"category\t{_error_category(exc)}")
    return lines("status\tok")


def error_lines(exc: BaseException) -> list[str]:
    """An error rendered as lines both halves of the pair can produce.

    The obvious rendering -- Python's exception class name -- is not portable.
    The Go port has one error type for the whole library, so it can answer
    `SQLParseError` and nothing else, and a case whose answer is the string
    "NotImplementedError" is a case only the reference can pass.

    Three shapes, and which one a failure gets is itself part of the contract:

      kind SQLParseError  + message  the library's own error.  The message is
                                     published API and is graded verbatim.
      kind not-implemented           the reference raises a bare
                                     NotImplementedError.  Its message is empty,
                                     so grading the message would grade "" and
                                     prove nothing.  The port must fail here
                                     too; what it says while failing is its own
                                     business.  The Go half answers this shape
                                     from sqlparse.IsNotImplemented, which the
                                     contract requires and which is checked
                                     before the *Error assertion: Python's
                                     NotImplementedError and SQLParseError are
                                     disjoint classes, but a Go port may return
                                     one value that is both, so the order is
                                     fixed rather than left to the port.
      kind <category>                anything the interpreter raised.  See
                                     _error_category.

    No `message` line is emitted for the latter two: an absent field cannot
    disagree, and a field whose content is one runtime's wording is not a
    behavioral difference.
    """
    try:
        from sqlparse.exceptions import SQLParseError
    except Exception:  # noqa: BLE001 -- the reference should always import
        SQLParseError = ()  # type: ignore[assignment]
    # Where this shape is reachable, measured over the whole catalog rather than
    # read off the source: 88 cases answer `not-implemented` (72 through `fmt` on
    # the two presets carrying an accepted right_margin, 16 through `filter` on
    # RightMarginFilter directly), 22 answer `SQLParseError` through `validate`,
    # and 4 answer a category.  No case reaches a right_margin filter with a
    # document that fails to decode first, so the not-implemented answer is
    # unconditional for those presets -- which is what lets the Go half key on a
    # predicate instead of having to reproduce an exception hierarchy.
    #
    # sqlparse 0.5.3 has a second `raise NotImplementedError`, at
    # filters/output.py:19, and it is unreachable: build_filter_stack only ever
    # instantiates OutputPythonFilter and OutputPHPFilter, both of which override
    # the abstract _process, `output_format='sql'` installs no filter at all, and
    # validate_options rejects every other value.  Noted because a reader who
    # greps for the exception finds two sites and should not have to work out
    # which one this docstring means.
    if SQLParseError and isinstance(exc, SQLParseError):
        return ["kind\tSQLParseError", f"message\t{enc_field(str(exc))}"]
    if isinstance(exc, NotImplementedError):
        return ["kind\tnot-implemented"]
    return [f"kind\t{_error_category(exc)}"]


def _error_category(exc: BaseException) -> str:
    """Which kind of failure this is, in portable terms.

    Python raises LookupError for an unknown codec and UnicodeDecodeError for
    undecodable bytes; Go's equivalent would be neither type.  What both can
    agree on is the distinction between "the encoding is not known" and "the
    bytes are not valid in it", so that is what is compared.
    """
    if isinstance(exc, RecursionError):
        return "depth-exceeded"
    if isinstance(exc, UnicodeDecodeError):
        # Ordered before LookupError: UnicodeDecodeError is a ValueError, but a
        # bad codec *name* is a LookupError, and the two are different failures
        # that a port must also keep apart.
        return "undecodable"
    if isinstance(exc, LookupError):
        return "unknown-encoding"
    if isinstance(exc, UnicodeError):
        return "unicode"
    if isinstance(exc, AttributeError):
        # The reference reached through something that was not there.  Its live
        # instance is Function.get_window (sql.py:639-642): token_next_by returns
        # the pair (None, None) when it finds nothing, `not (None, None)` is
        # false because a populated tuple is truthy, so the guard never fires and
        # the None is dereferenced.  Every function with no OVER clause fails
        # that way.
        #
        # This is graded, not repaired.  Go's own name for the same failure is a
        # nil pointer dereference, so both halves have something to call it --
        # unlike `other`, which would also absorb failures that are not this one.
        # The contract requires the port to fail here too; a port that returns
        # nil is more correct than the reference and less compatible with it.
        return "nil-deref"
    if isinstance(exc, TypeError):
        return "wrong-type"
    if isinstance(exc, ValueError):
        return "value"
    return "other"


# -- model tier -------------------------------------------------------------


def _statements(ctx: Context, a: Args, idx: int = 0):
    import sqlparse

    doc_id = a.doc_id(idx)
    encoding = a.doc_encoding(idx)
    if encoding:
        return sqlparse.parse(ctx.doc_bytes(doc_id), encoding)
    return sqlparse.parse(ctx.doc_bytes(doc_id))


@op("tok")
def op_tok(ctx: Context, a: Args) -> bytes:
    """The flat token stream after grouping: every leaf, in order."""
    out = []
    for si, stmt in enumerate(_statements(ctx, a)):
        for ti, token in enumerate(stmt.flatten()):
            out.append(
                f"t\t{si}\t{ti}\t{enc_field(str(token.ttype))}"
                f"\t{enc_field(token.value)}"
            )
    return lines(*out) if out else lines("t\tnone")


@op("tree")
def op_tree(ctx: Context, a: Args) -> bytes:
    """The grouped tree, one line per node, depth-first.

    Carries the flags that drive the filters -- is_group, is_keyword,
    is_whitespace, normalized -- because a tree with the right shape and the
    wrong flags formats wrongly, and the format families would report that as a
    formatter bug when it is a grouper bug.
    """
    out = []
    for si, stmt in enumerate(_statements(ctx, a)):
        _render_node(out, stmt, si, 0, [])
    return lines(*out) if out else lines("n\tnone")


def _render_node(out: list, node, si: int, depth: int, path: list) -> None:
    addr = ".".join(str(p) for p in path) or "-"
    out.append(
        "n\t{si}\t{depth}\t{addr}\t{kind}\t{ttype}\t{grp}{kw}{ws}{nl}\t"
        "{norm}\t{value}".format(
            si=si,
            depth=depth,
            addr=addr,
            kind=type(node).__name__,
            ttype=enc_field(str(node.ttype)),
            grp=1 if node.is_group else 0,
            kw=1 if node.is_keyword else 0,
            ws=1 if node.is_whitespace else 0,
            nl=1 if getattr(node, "is_newline", False) else 0,
            norm=enc_field(node.normalized),
            value=enc_field(node.value),
        )
    )
    if node.is_group:
        for idx, child in enumerate(node.tokens):
            _render_node(out, child, si, depth + 1, path + [idx])


@op("stmt")
def op_stmt(ctx: Context, a: Args) -> bytes:
    """Statement-level derived properties."""
    out = []
    for si, stmt in enumerate(_statements(ctx, a)):
        out.append(f"type\t{si}\t{enc_field(str(stmt.get_type()))}")
        out.append(f"tokens\t{si}\t{len(stmt.tokens)}")
        out.append(f"flat\t{si}\t{len(list(stmt.flatten()))}")
        out.append(f"group\t{si}\t{1 if stmt.is_group else 0}")
        out.append(f"ws\t{si}\t{1 if stmt.is_whitespace else 0}")
        out.append(f"len\t{si}\t{len(str(stmt))}")
        # token_first returns the token itself, not an (index, token) pair --
        # only token_next and token_prev return the pair.  The contract's
        # TokenFirst mirrors that asymmetry rather than tidying it away.
        for label, skip_ws, skip_cm in (
            ("first", True, True),
            ("firstws", False, False),
            ("firstnocm", True, False),
        ):
            token = stmt.token_first(skip_ws=skip_ws, skip_cm=skip_cm)
            out.append(
                f"{label}\t{si}\t"
                f"{'nil' if token is None else enc_field(token.value)}\t"
                f"{'' if token is None else enc_field(str(token.ttype))}"
            )
        # The walk both filters rely on: next/prev with each skip combination.
        for skip_ws in (True, False):
            for skip_cm in (True, False):
                seq = []
                idx = -1
                while True:
                    idx, token = stmt.token_next(idx, skip_ws=skip_ws,
                                                 skip_cm=skip_cm)
                    if token is None:
                        break
                    seq.append(str(idx))
                    if len(seq) > 4096:
                        break
                out.append(
                    f"walk\t{si}\t{1 if skip_ws else 0}{1 if skip_cm else 0}\t"
                    f"{','.join(seq)}"
                )
    return lines(*out) if out else lines("type\tnone")


# The method battery.  Each entry is (label, callable), and every one is applied
# to every addressed node.  A method the node's kind does not define is not an
# error and not a skip: it is the answer `no-method`, decided by
# spec.ACCESSOR_SCOPE rather than by asking Python, so the Go half -- whose
# single *Node makes every method callable -- can render the same thing from the
# same table.  See the ACCESSOR_SCOPE comment in spec.py.
#
# `fn` is None for the labels the probe computes from the node instead of reading
# off it; _api_value handles those by name.
def _api_methods():
    def attr(name):
        def get(node):
            return getattr(node, name)
        return get

    def call(name, *args, **kwargs):
        def do(node):
            return getattr(node, name)(*args, **kwargs)
        return do

    def seq(name, *args):
        def do(node):
            return list(getattr(node, name)(*args))
        return do

    def flatseq(name, *args):
        """Like seq, for a reference method that yields *lists* of tokens.

        `get_array_indices` is the only one: it yields `token.tokens[1:-1]` per
        SquareBrackets child, so `list(...)` is a list of lists.  _render_item
        brackets and comma-joins a nested list, which would make the expectation
        `list 1 [1,:,2]` for `a[1:2]` -- a shape no implementation of the published
        contract can produce.  The contract declares
        `func (*Node).ArrayIndices() []*Node`, flat, and the Go probe renders it
        with the flat renderNodeList; the interior tokens concatenated is therefore
        the answer the task asked for, and this is the side that has to move,
        because the contract is what the submission was handed.

        Flattening keeps every failure the check was catching -- a nil return, the
        brackets left in, the wrong tokens -- and gives up only the bracket
        *boundary* between two SquareBrackets children.  No corpus document has two
        (fn-array-index and fn-array-slice are the only ones that reach here), so
        nothing on this corpus becomes indistinguishable.  Restoring that boundary
        means changing the contract to `[][]*Node` and rendering it with
        renderNodeLists, which is already written and, before this, called nowhere.
        """
        def do(node):
            out = []
            for group in getattr(node, name)(*args):
                out.extend(group)
            return out
        return do

    from sqlparse import tokens as T

    return [
        # --- every kind answers -------------------------------------------
        ("kind", lambda n: type(n).__name__),
        ("ttype", lambda n: str(n.ttype)),
        ("value", attr("value")),
        ("normalized", attr("normalized")),
        ("is_group", attr("is_group")),
        ("is_keyword", attr("is_keyword")),
        ("is_whitespace", attr("is_whitespace")),
        ("is_newline", attr("is_newline")),
        ("str", lambda n: str(n)),
        ("flatten_count", lambda n: len(list(n.flatten()))),
        # match() is the reference's own predicate, and each of these three
        # grades a different clause of it (sql.py:89-118).
        #
        # kw_lower   the value list, lowercase, against Token.Keyword tokens
        #            whose value is uppercase: keyword comparison folds case and
        #            everything else does not, so this answers true where a
        #            case-sensitive port answers false.
        # kw_subtype the same values against Token.Keyword, on documents whose
        #            SELECT is Token.Keyword.DML.  The type test is `is`, not
        #            containment, so the reference answers false for every DML
        #            token -- a port matching subtypes answers true and fails
        #            only here.
        # punct_regex the regex mode, on a type that is not a keyword, so the
        #            IGNORECASE flag is off.
        ("match_kw_lower", call("match", T.Keyword, ["from", "table", "as"])),
        ("match_kw_subtype", call("match", T.Keyword, ["SELECT", "INSERT"])),
        ("match_punct_regex", call("match", T.Punctuation, [r"[(),]"],
                                   regex=True)),
        ("multiline_str", None),
        ("within_function", None),
        ("within_parenthesis", None),
        ("parent_kind", None),
        ("has_ancestor_stmt", None),
        ("is_child_of_root", None),
        ("token_index_self", None),
        # --- groups only (absent on Token) --------------------------------
        ("token_count", lambda n: len(n.tokens)),
        ("get_real_name", call("get_real_name")),
        ("get_name", call("get_name")),
        ("get_parent_name", call("get_parent_name")),
        ("get_alias", call("get_alias")),
        ("has_alias", call("has_alias")),
        ("get_sublists", None),
        # token_first yields a token, token_next/token_prev yield (index, token).
        ("token_first", call("token_first")),
        ("token_first_ws", call("token_first", False, False)),
        ("token_next_0", call("token_next", 0)),
        ("token_prev_last", call("token_prev", 1)),
        # Offset addressing over the flattened text -- 0 is the first character
        # and the mid offset lands wherever the node's own length puts it, so
        # the answer depends on the whole flatten order and on every token
        # length along the way.
        ("token_at_offset_0", call("get_token_at_offset", 0)),
        ("token_at_offset_mid", None),
        # --- one kind each ------------------------------------------------
        ("get_type", call("get_type")),
        ("get_ordering", call("get_ordering")),
        ("get_typecast", call("get_typecast")),
        # flatseq, not seq: the reference yields a list per bracket, the contract
        # declares a flat []*Node.  See flatseq's docstring.
        ("get_array_indices", flatseq("get_array_indices")),
        ("is_wildcard", call("is_wildcard")),
        ("get_identifiers", seq("get_identifiers")),
        ("get_parameters", seq("get_parameters")),
        ("get_window", call("get_window")),
        ("get_cases", call("get_cases")),
        ("get_cases_skip", call("get_cases", skip_ws=True)),
        ("left", attr("left")),
        ("right", attr("right")),
        # A plain method in 0.5.3, not a property, so it has to be called --
        # reading it as an attribute yields the bound method and renders nothing
        # a port could match.  Its body is `self.tokens and self.tokens[0].ttype
        # == T.Comment.Multiline`, so a token-less comment would answer with the
        # empty list rather than False; the grouper never builds one (234 Comment
        # nodes in the docs, none empty), so that branch is documented in the
        # contract rather than graded here.
        ("comment_is_multiline", call("is_multiline")),
    ]


API_METHODS = _api_methods()

# Where in each statement to apply the battery.  Addresses are child-index
# paths from the statement; "" is the statement itself.  The deep addresses
# reach into whatever group is there, and a path that does not resolve is
# reported as unresolved rather than skipped, so both halves agree on the shape
# of the answer even when the trees differ.
API_ADDRESSES = ["", "0", "1", "2", "0.0", "1.0", "2.0", "2.1", "2.2",
                 "0.0.0", "2.0.0", "-1", "-2", "-1.-1"]


@op("api")
def op_api(ctx: Context, a: Args) -> bytes:
    """The Node read surface, applied at fixed addresses.

    A document rather than a hand-written statement, so this family rides the
    docs and covers group kinds no curated list would think to include.
    """
    import sqlparse

    statements = sqlparse.parse(ctx.doc_text(a.doc_id(0)))
    out = []
    for si, stmt in enumerate(statements[:3]):
        for addr in API_ADDRESSES:
            node = _resolve(stmt, addr)
            if node is None:
                out.append(f"a\t{si}\t{addr or '-'}\tunresolved")
                continue
            for label, fn in API_METHODS:
                out.append(
                    f"a\t{si}\t{addr or '-'}\t{label}\t"
                    f"{_api_value(ctx, node, label, fn, stmt)}"
                )
    return lines(*out) if out else lines("a\tnone")


def _resolve(stmt, addr: str):
    if addr == "":
        return stmt
    node = stmt
    for part in addr.split("."):
        if not node.is_group:
            return None
        idx = int(part)
        tokens = node.tokens
        if idx < 0:
            idx += len(tokens)
        if idx < 0 or idx >= len(tokens):
            return None
        node = tokens[idx]
    return node


def _api_value(ctx: Context, node, label: str, fn, root) -> str:
    """One method's answer, rendered portably.

    Every branch here is a decision about what two languages can both say.
    Applicability is decided first, from spec.ACCESSOR_SCOPE: a node whose kind
    does not define the accessor answers `no-method`.  That is a table both
    halves read, not a Python hasattr, because the contracted Go *Node makes
    every method callable and could never discover the reference's class
    hierarchy by asking.  Rendering it keeps the hierarchy graded: a port that
    answers a value everywhere fails these positions and nothing else.

    A precondition the reference itself evaluates at runtime renders separately
    -- `no-parent` for the root, which has no parent to index it -- because that
    depends on where the node sits, not on what its kind defines.

    An accessor that raises answers `raise` plus the portable category from
    error_lines: which accessors raise is part of the behavior, and a port
    returning a zero value where the reference raises is not compatible.
    """
    if not ctx.accessor_applies(label, type(node).__name__):
        return "no-method"
    try:
        if label == "token_index_self":
            parent = node.parent
            if parent is None:
                return "no-parent"
            return f"int\t{parent.token_index(node)}"
        if label == "within_function":
            from sqlparse import sql as S
            return f"bool\t{1 if node.within(S.Function) else 0}"
        if label == "within_parenthesis":
            from sqlparse import sql as S
            return f"bool\t{1 if node.within(S.Parenthesis) else 0}"
        if label == "parent_kind":
            return f"str\t{type(node.parent).__name__ if node.parent else ''}"
        if label == "has_ancestor_stmt":
            # Against the statement the node was addressed from.  The statement
            # itself answers false -- has_ancestor walks parents, and the root
            # has none -- and every node below it answers true, so the label
            # grades the parent chain rather than a single link.
            return f"bool\t{1 if node.has_ancestor(root) else 0}"
        if label == "is_child_of_root":
            # Against the statement, not against node.parent -- asking a node
            # whether it is a child of its own parent is true by construction
            # everywhere and grades nothing.  Against the root it separates the
            # statement's direct children from everything deeper, and the
            # statement itself answers false because its parent is None.
            return f"bool\t{1 if node.is_child_of(root) else 0}"
        if label == "multiline_str":
            return f"bool\t{1 if chr(10) in str(node) else 0}"
        if label == "get_sublists":
            return f"int\t{len(list(node.get_sublists()))}"
        if label == "token_at_offset_mid":
            # Halfway through the node's own rendered text.  An empty node asks
            # for offset 0 of nothing and gets None, which renders nil.
            token = node.get_token_at_offset(len(str(node)) // 2)
            return _render_value(token)
        if label in ("get_cases", "get_cases_skip"):
            # A list of (condition, value) pairs, each side a token list or
            # None.  Rendered structurally: str() on the tuple would emit
            # Python reprs, which carry object addresses and are therefore
            # different on every run and impossible in Go.
            skip = label.endswith("_skip")
            cases = node.get_cases(skip_ws=True) if skip else node.get_cases()
            parts = [f"cases\t{len(cases)}"]
            for cond, value in cases:
                parts.append(
                    ("nil" if cond is None
                     else enc_field("".join(str(t) for t in cond)))
                    + "=>"
                    + ("nil" if value is None
                       else enc_field("".join(str(t) for t in value)))
                )
            return "\t".join(parts)
        if fn is None:
            raise ProbeError(
                f"api label {label!r} has no callable and no branch here")
        value = fn(node)
    except ProbeError:
        raise
    except Exception as exc:  # noqa: BLE001
        # An accessor that raises renders through error_lines, which emits a
        # portable category rather than a CPython class name.  AttributeError is
        # deliberately not caught separately any more: applicability is decided
        # from ACCESSOR_SCOPE above, so an AttributeError reaching here means the
        # table and the library disagree, which is a defect and should surface as
        # one instead of being absorbed into an answer.
        return "raise\t" + "\t".join(error_lines(exc))
    return _render_value(value)


def _render_value(value) -> str:
    if value is None:
        return "nil"
    if isinstance(value, bool):
        return f"bool\t{1 if value else 0}"
    if isinstance(value, int):
        return f"int\t{value}"
    if isinstance(value, str):
        return f"str\t{enc_field(value)}"
    if isinstance(value, tuple):
        # token_first / token_next return (index, token).
        if len(value) == 2 and (value[1] is None or hasattr(value[1], "ttype")):
            idx, token = value
            if token is None:
                return f"tok\t{idx if idx is not None else -1}\t"
            return f"tok\t{idx}\t{enc_field(token.value)}"
        return "tuple\t" + "\t".join(_render_value(v) for v in value)
    if isinstance(value, list):
        parts = [f"list\t{len(value)}"]
        for item in value:
            parts.append(_render_item(item))
        return "\t".join(parts)
    if hasattr(value, "ttype"):
        return f"node\t{type(value).__name__}\t{enc_field(str(value))}"
    return _render_item(value)


def _render_item(item) -> str:
    """One element of a sequence, rendered without ever calling repr().

    A token renders as its source text, a nested sequence renders in brackets,
    and anything else renders as its str().  The distinction matters because
    Python's default repr for a token includes its memory address: a renderer
    that falls through to repr() produces a different answer on every run, so
    the expectation store would never match itself, and no Go program could
    ever produce the answer either.
    """
    if item is None:
        return "nil"
    if hasattr(item, "ttype"):
        return enc_field(str(item))
    if isinstance(item, (list, tuple)):
        return "[" + ",".join(_render_item(x) for x in item) + "]"
    if isinstance(item, bool):
        return "1" if item else "0"
    if isinstance(item, (int, str)):
        return enc_field(str(item))
    # Anything else would be a Python object with an address in its repr.
    # Reporting the type alone keeps the answer reproducible and makes the
    # omission visible rather than silent.
    return f"<{type(item).__name__}>"


@op("ttype")
def op_ttype(ctx: Context, a: Args) -> bytes:
    """The token-type lattice, resolved by rendered name.

    One case per name, reporting its rendering, its parent, whether it resolves
    at all, and its containment relation against every name in the spec list.
    The relation is the point: `ttype in T.Keyword` is how every filter in the
    reference dispatches, and a Go type hierarchy that gets containment wrong
    misformats everything while looking correct in isolation.
    """
    name = a.text(0)
    resolved = _resolve_ttype(name)
    out = [f"name\t{enc_field(name)}"]
    if resolved is None:
        out.append("resolved\t0")
        return lines(*out)
    out.append("resolved\t1")
    out.append(f"render\t{enc_field(str(resolved))}")
    # The root's parent is None in the reference, and the contracted Go signature
    # is Parent() TokenType, which has no nil.  Rendering the absent parent as
    # "-" rather than str(None) keeps the field reproducible on both sides.
    parent = resolved.parent
    out.append(f"parent\t{enc_field(str(parent) if parent is not None else '-')}")
    out.append(f"depth\t{len(resolved)}")
    for other in ctx.spec["ttype_names"]:
        target = _resolve_ttype(other)
        if target is None:
            out.append(f"c\t{enc_field(other)}\tunresolved")
            continue
        out.append(
            f"c\t{enc_field(other)}\t{1 if target in resolved else 0}"
            f"\t{1 if resolved in target else 0}"
            f"\t{1 if resolved is target else 0}"
        )
    return lines(*out)


def _resolve_ttype(name: str):
    """Walk a rendered name back to a token type, or None.

    The reference creates token types lazily on attribute access, so a name that
    does not exist would be created rather than rejected.  That would make every
    `Token.Nonexistent` case answer "resolved" -- which is the reference's real
    behavior, and it is graded that way: absence is impossible in the reference,
    so the Go side must also answer that any well-formed name resolves.
    """
    from sqlparse import tokens as T

    parts = name.split(".")
    if not parts or parts[0] != "Token":
        return None
    node = T.Token
    for part in parts[1:]:
        if not part:
            return None
        node = getattr(node, part)
    return node


# -- keywords tier ----------------------------------------------------------


@op("kwtable")
def op_kwtable(ctx: Context, a: Args) -> bytes:
    """One whole keyword table, every entry, sorted by word.

    Sorted because the reference's dicts have insertion order and Go's maps have
    none, so unsorted output would be a coin flip.  The search *order* across
    tables is a separate case, driven through `keyword`.
    """
    from sqlparse import keywords as K

    table = a.text(0)
    if table == "__order__":
        # KEYWORDS_COMMON first, then the dialect tables, in the order the
        # lexer consults them.  Observable through which type a word in two
        # tables resolves to.
        names = [n for n, _ in _keyword_tables()]
        return lines("order\t" + "\t".join(names))
    if table == "__regex__":
        return lines(f"count\t{len(K.SQL_REGEX)}")
    tables = dict(_keyword_tables())
    if table not in tables:
        raise ProbeError(f"unknown keyword table: {table}")
    entries = tables[table]
    out = [f"table\t{enc_field(table)}", f"count\t{len(entries)}"]
    for word in sorted(entries):
        out.append(f"e\t{enc_field(word)}\t{enc_field(str(entries[word]))}")
    return lines(*out)


_KEYWORD_TABLES_CACHE = None


def _keyword_tables():
    """The tables in the order the default lexer installs them.

    Recovered from the default lexer's own list rather than written down here,
    and the difference is not cosmetic.  `default_initialization` installs
    KEYWORDS *last*, after the eight dialect tables -- not second, which is
    where a reader who expects the general table to win would put it.  Three
    words are in both KEYWORDS and a dialect table with different types
    (CHARACTER, MAP, TIMESTAMP), so a written-down order that guessed wrong
    would make this oracle disagree with the reference it is supposed to define,
    and a correct port would fail for being correct.

    `_keywords` holds the dicts themselves, so each is matched back to its
    module-level name by identity.  If a future reference renames or reorders
    them, this follows; if it installs a table that is not a module-level
    KEYWORDS* name, the check below fails loudly rather than dropping it.

    Read from a PRIVATE lexer, not from the shared singleton.  The `lexstate`
    op mutates the singleton on purpose, and while it restores it in a finally,
    a table order latched from a half-mutated singleton would be wrong for every
    later case -- making this op's answer depend on catalog order.  A fresh
    instance running the same default_initialization has the order and none of
    the exposure.
    """
    global _KEYWORD_TABLES_CACHE
    if _KEYWORD_TABLES_CACHE is not None:
        return _KEYWORD_TABLES_CACHE

    from sqlparse import keywords as K, lexer

    by_id = {}
    for attr in dir(K):
        if attr.startswith("KEYWORDS"):
            value = getattr(K, attr)
            if isinstance(value, dict):
                by_id[id(value)] = attr

    private = lexer.Lexer()
    private.default_initialization()
    installed = private._keywords
    tables = []
    for table in installed:
        name = by_id.get(id(table))
        if name is None:
            raise ProbeError(
                "the default lexer installed a keyword table that is not a "
                "module-level KEYWORDS* dict; the probe cannot name it")
        tables.append((name, table))
    if len(tables) != len(by_id):
        missing = sorted(set(by_id.values()) - {n for n, _ in tables})
        raise ProbeError(f"keyword tables never installed: {missing}")
    _KEYWORD_TABLES_CACHE = tables
    return tables


@op("kwnames")
def op_kwnames(ctx: Context, a: Args) -> bytes:
    """Which keyword tables exist, in lexer-installation order, with sizes.

    A port that merged the nine dialect tables into one map answers every
    individual lookup correctly and fails this: the tables are part of the
    published surface, and `keywords.TableNames` is in the contract.
    """
    tables = _keyword_tables()
    out = [f"count\t{len(tables)}"]
    total = 0
    for name, table in tables:
        total += len(table)
        out.append(f"t\t{enc_field(name)}\t{len(table)}")
    out.append(f"entries\t{total}")
    return lines(*out)


@op("keyword")
def op_keyword(ctx: Context, a: Args) -> bytes:
    """One word through the lexer's keyword resolution.

    Reports the resolved type and the value the lexer would carry, which is the
    two-value protocol `Lexer.is_keyword` has.  Also reports which table the
    word came from, because a word present in two tables resolves through the
    first and a port that merges the tables into one map gets that wrong.
    """
    from sqlparse import lexer

    word = a.text(0)
    lex = lexer.Lexer.get_default_instance()
    ttype, value = lex.is_keyword(word)
    out = [
        f"word\t{enc_field(word)}",
        f"ttype\t{enc_field(str(ttype))}",
        f"value\t{enc_field(value)}",
    ]
    found = []
    for name, table in _keyword_tables():
        if word.upper() in table:
            found.append(name)
    out.append("tables\t" + "\t".join(found))
    return lines(*out)


@op("kwlex")
def op_kwlex(ctx: Context, a: Args) -> bytes:
    """A dialect document through the lexer, types only.

    The keyword tables only matter through tokenization.  Values are omitted so
    this family measures classification rather than re-measuring the round trip.
    """
    from sqlparse import lexer

    data = ctx.doc_bytes(a.doc_id(0))
    text = data.decode("utf-8", "surrogateescape")
    out = []
    for idx, (ttype, value) in enumerate(lexer.tokenize(text)):
        out.append(f"t\t{idx}\t{enc_field(str(ttype))}\t{len(value)}")
    return lines(*out) if out else lines("t\tnone")


# -- parts tier -------------------------------------------------------------


@op("lex")
def op_lex(ctx: Context, a: Args) -> bytes:
    """The raw pre-grouping stream: the regex engine's output, unmediated.

    This is the family the RE2 gap lands on.  Everything downstream can be
    rebuilt by hand; this cannot, because the reference's patterns use lookbehind
    and a backreference and Go's regexp supports neither.
    """
    from sqlparse import lexer

    doc_id = a.doc_id(0)
    encoding = a.doc_encoding(0)
    if encoding:
        stream = lexer.tokenize(ctx.doc_bytes(doc_id), encoding)
    else:
        stream = lexer.tokenize(ctx.doc_bytes(doc_id))
    out = []
    for idx, (ttype, value) in enumerate(stream):
        out.append(f"t\t{idx}\t{enc_field(str(ttype))}\t{enc_field(value)}")
    return lines(*out) if out else lines("t\tnone")


@op("filter")
def op_filter(ctx: Context, a: Args) -> bytes:
    """One filter, installed alone at its declared stage.

    Isolating a filter is the only way to attribute a formatting difference.
    Run through format(), every filter's output is entangled with every other
    filter's; run alone, a wrong AlignedIndent cannot be blamed on Reindent.
    """
    name = a.name(0)
    entry = ctx.spec["filters"].get(name)
    if entry is None:
        raise ProbeError(f"unknown filter: {name}")
    stack = _build_stack_with_filter(entry)
    doc_id = a.doc_id(1)
    text = ctx.doc_text(doc_id)
    try:
        results = list(stack.run(text))
    except Exception as exc:  # noqa: BLE001
        return lines("status\terr", *error_lines(exc))
    out = ["status\tok", f"count\t{len(results)}"]
    for idx, item in enumerate(results):
        out.append(f"r\t{idx}\t{enc_field(str(item))}")
    return lines(*out)


def _instantiate_filter(entry: dict):
    """Build one filter from its spec entry.

    The spec names the Python class and the Go constructor side by side, and
    carries the parameters as keyword arguments under the reference's own
    parameter names.  Passing them as keywords rather than positionally is what
    lets the Go side use a config struct: the two languages disagree about
    optional arguments, and a positional encoding would have forced one of them
    to lie about the default.
    """
    from sqlparse import filters as F

    cls_name = entry.get("py")
    if not cls_name:
        raise ProbeError(f"filter entry has no py class: {entry!r}")
    cls = getattr(F, cls_name, None)
    if cls is None:
        raise ProbeError(f"reference has no filter class {cls_name}")
    kwargs = {k: decode_option(v) for k, v in (entry.get("params") or {}).items()}
    return cls(**kwargs)


def _build_stack_with_filter(entry: dict):
    from sqlparse.engine import FilterStack
    from sqlparse import filters as F

    stack = FilterStack()
    instance = _instantiate_filter(entry)
    stage = entry.get("stage", "stmt")
    if stage == "pre":
        stack.preprocess.append(instance)
        stack.postprocess.append(F.SerializerUnicode())
    elif stage == "stmt":
        stack.enable_grouping()
        stack.stmtprocess.append(instance)
        stack.postprocess.append(F.SerializerUnicode())
    elif stage == "post":
        stack.enable_grouping()
        stack.postprocess.append(instance)
    else:
        raise ProbeError(f"unknown filter stage: {stage}")
    return stack


@op("stack")
def op_stack(ctx: Context, a: Args) -> bytes:
    """A whole named stack: composition, grouping and semicolon stripping.

    The individual filters can all be right and the assembly still wrong -- the
    reference runs preprocess on the raw stream, stmtprocess per grouped
    statement and postprocess after, and getting that order wrong produces
    plausible output that differs everywhere.
    """
    from sqlparse.engine import FilterStack

    name = a.name(0)
    shape = ctx.spec["stacks"].get(name)
    if shape is None:
        raise ProbeError(f"unknown stack: {name}")
    stack = FilterStack(strip_semicolon=shape.get("strip_semicolon", False))
    if shape.get("grouping"):
        stack.enable_grouping()
    for filter_name in shape.get("filters", []):
        entry = ctx.spec["filters"].get(filter_name)
        if entry is None:
            raise ProbeError(f"unknown filter in stack {name}: {filter_name}")
        instance = _instantiate_filter(entry)
        stage = entry.get("stage", "stmt")
        {"pre": stack.preprocess, "stmt": stack.stmtprocess,
         "post": stack.postprocess}[stage].append(instance)
    text = ctx.doc_text(a.doc_id(1))
    try:
        results = list(stack.run(text))
    except Exception as exc:  # noqa: BLE001
        return lines("status\terr", *error_lines(exc))
    out = ["status\tok", f"count\t{len(results)}"]
    for idx, item in enumerate(results):
        out.append(f"r\t{idx}\t{enc_field(str(item))}")
    return lines(*out)


@op("remove-quotes")
def op_remove_quotes(ctx: Context, a: Args) -> bytes:
    from sqlparse import utils

    text = a.text(0)
    result = utils.remove_quotes(text)
    return lines(
        f"in\t{enc_field(text)}",
        f"out\t{enc_field(result)}",
        f"len\t{len(result)}",
    )


@op("split-unquoted-newlines")
def op_split_unquoted_newlines(ctx: Context, a: Args) -> bytes:
    from sqlparse import utils

    text = a.text(0)
    parts = utils.split_unquoted_newlines(text)
    out = [f"count\t{len(parts)}"]
    for idx, part in enumerate(parts):
        out.append(f"p\t{idx}\t{len(part)}\t{enc_field(part)}")
    return lines(*out)



@op("optkeys")
def op_optkeys(ctx: Context, a: Args) -> bytes:
    """Which option names the formatter declares, in declaration order."""
    keys = _option_keys()
    return lines(f"count\t{len(keys)}", *[f"k\t{k}" for k in keys])


@op("optkeys-sorted")
def op_optkeys_sorted(ctx: Context, a: Args) -> bytes:
    keys = sorted(_option_keys())
    return lines(f"count\t{len(keys)}", *[f"k\t{k}" for k in keys])


def _option_keys() -> list[str]:
    """The option names validate_options reads, discovered from the source.

    Read out of the reference's own bytecode rather than listed by hand: the
    names are the keys it passes to options.pop and options.get, so this cannot
    drift from what the function actually consults.
    """
    from sqlparse import formatter

    code = formatter.validate_options.__code__
    keys = []
    for const in code.co_consts:
        if isinstance(const, str) and const.islower() and "_" in const or (
            isinstance(const, str) and const in (
                "reindent", "compact", "indent_width", "wrap_after",
                "comma_first", "right_margin", "output_format",
            )
        ):
            if const not in keys and const.replace("_", "").isalpha():
                keys.append(const)
    return keys


@op("lexstate")
def op_lexstate(ctx: Context, a: Args) -> bytes:
    """The lexer's mutable keyword state.

    The reference lexer is a process-wide singleton with an add/clear/
    reinitialize protocol, and each step observably changes tokenization.  A
    port that hard-codes the keyword tables into a package-level map passes
    every other keyword case and fails every script here.

    Each script leaves the singleton as it found it: the scripts run in one
    process, in catalog order, and a script that leaked state would make the
    next one's answer depend on the order rather than on the protocol.
    """
    from sqlparse import lexer
    from sqlparse import tokens as T

    script = a.name(0)
    lex = lexer.Lexer.get_default_instance()
    probe_sql = "select mycustomkw from t where x = 1"
    out = []

    def snapshot(label: str) -> None:
        try:
            stream = list(lexer.tokenize(probe_sql))
        except Exception as exc:  # noqa: BLE001
            out.append(f"{label}\terr\t" + "\t".join(error_lines(exc)))
            return
        parts = [f"{enc_field(str(t))}:{enc_field(v)}" for t, v in stream]
        out.append(f"{label}\tok\t{len(stream)}\t{' '.join(parts)}")

    def kw(label: str, word: str) -> None:
        ttype, value = lex.is_keyword(word)
        out.append(
            f"{label}\t{enc_field(word)}\t{enc_field(str(ttype))}"
            f"\t{enc_field(value)}"
        )

    try:
        if script == "default":
            snapshot("stream")
            kw("kw", "SELECT")
            kw("kw", "MYCUSTOMKW")
        elif script == "clear-then-lex":
            lex.clear()
            snapshot("stream")
        elif script == "clear-then-default":
            lex.clear()
            snapshot("cleared")
            lex.default_initialization()
            snapshot("restored")
        elif script == "add-custom":
            lex.add_keywords({"MYCUSTOMKW": T.Keyword})
            snapshot("stream")
            kw("kw", "MYCUSTOMKW")
        elif script == "add-then-clear":
            lex.add_keywords({"MYCUSTOMKW": T.Keyword})
            snapshot("added")
            lex.clear()
            snapshot("cleared")
        elif script == "add-twice":
            lex.add_keywords({"MYCUSTOMKW": T.Keyword})
            lex.add_keywords({"MYCUSTOMKW": T.Name.Builtin})
            snapshot("stream")
            kw("kw", "MYCUSTOMKW")
        elif script == "add-overrides-builtin":
            lex.add_keywords({"SELECT": T.Name})
            kw("kw", "SELECT")
            snapshot("stream")
        elif script == "default-idempotent":
            lex.default_initialization()
            snapshot("once")
            lex.default_initialization()
            snapshot("twice")
        elif script == "clear-idempotent":
            lex.clear()
            lex.clear()
            snapshot("stream")
            kw("kw", "SELECT")
        elif script == "add-empty-table":
            lex.add_keywords({})
            snapshot("stream")
        elif script == "add-then-reinit":
            lex.add_keywords({"MYCUSTOMKW": T.Keyword})
            snapshot("added")
            lex.default_initialization()
            snapshot("reinit")
            kw("kw", "MYCUSTOMKW")
        elif script == "is-keyword-after-clear":
            lex.clear()
            kw("kw", "SELECT")
            kw("kw", "MYCUSTOMKW")
        elif script == "add-lowercase-key":
            # The reference upper-cases the *lookup*, not the table, so a table
            # keyed in lower case never matches anything.  A port that
            # normalizes keys on insert makes this word a keyword and is wrong.
            lex.add_keywords({"mycustomkw": T.Keyword})
            kw("kw", "MYCUSTOMKW")
            kw("kw", "mycustomkw")
            snapshot("stream")
        elif script == "add-many":
            # Several tables in one call, and one word present in two of them:
            # which wins is the search order, and the search order is observable.
            lex.add_keywords({"MYCUSTOMKW": T.Keyword, "OTHERKW": T.Name.Builtin})
            lex.add_keywords({"MYCUSTOMKW": T.Wildcard, "THIRDKW": T.Comment})
            for word in ("MYCUSTOMKW", "OTHERKW", "THIRDKW", "SELECT"):
                kw("kw", word)
            snapshot("stream")
        else:
            raise ProbeError(f"unknown lexstate script: {script}")
    finally:
        # Always restore, even if the script raised: the next case must see the
        # default lexer regardless of what this one did.
        lex.default_initialization()
    return lines(*out) if out else lines("none")


# ---------------------------------------------------------------------------
# The loop.
# ---------------------------------------------------------------------------


def handle(ctx: Context, line: str) -> str:
    fields = line.rstrip("\n").split("\t")
    if len(fields) < 2:
        return "?\tdefect\t" + b64("malformed request: fewer than two fields")
    case_id, op_name, raw_args = fields[0], fields[1], fields[2:]
    fn = OPS.get(op_name)
    if fn is None:
        return f"{case_id}\tdefect\t" + b64(f"unknown op: {op_name}")
    try:
        answer = fn(ctx, Args(ctx, raw_args))
    except ProbeError as exc:
        return f"{case_id}\tdefect\t" + b64(f"{exc}")
    except RecursionError:
        # A real answer: the reference has a recursion limit and hitting it is
        # observable behavior, graded as "errors rather than crashes".
        return f"{case_id}\terr\t" + b64("depth-exceeded")
    except Exception as exc:  # noqa: BLE001
        # Reported as a portable category, not as the interpreter's wording.
        # An op that reaches here failed inside the reference on a Python-native
        # exception -- a codec error, a type check -- and CPython's message for
        # those ("'unicodeescape' codec can't decode byte 0x5c in position
        # 19") is not something a Go implementation can produce.  Grading it
        # would put a case on the reference side of the pair that the target
        # side cannot answer, which makes the case a trick question rather than
        # a differential test.  The category is the part both halves can agree
        # on, and it is enough: what is graded is that the same input fails the
        # same way, not that two languages phrase a codec error identically.
        #
        # sqlparse's own SQLParseError is the exception: its message is the
        # library's published API, so it is reported verbatim and the port must
        # reproduce it.
        try:
            detail = _portable_error(exc)
        except Exception as inner:  # noqa: BLE001
            # The renderer itself failed.  On this half that can only be this
            # script or the reference library misbehaving, never the thing under
            # test, so it is a defect: freeze.py refuses to write a defect as any
            # submission's expected answer and aborts the image build instead.
            # The Go half treats the same failure as an `err`, and the asymmetry
            # is the point -- there, the renderer asks the *submission's* error
            # predicates, so a failure in it is the submission's.
            return f"{case_id}\tdefect\t" + b64(
                f"rendering {type(exc).__name__} failed: "
                f"{type(inner).__name__}: {inner}")
        if os.environ.get("PROBE_TRACEBACK"):
            detail += "\n" + traceback.format_exc()
        return f"{case_id}\terr\t" + b64(detail)
    if not isinstance(answer, bytes):
        return f"{case_id}\tdefect\t" + b64(f"op {op_name} returned non-bytes")
    return f"{case_id}\tok\t" + base64.b64encode(answer).decode("ascii")


def _portable_error(exc: BaseException) -> str:
    """An error rendering both halves of the pair can produce.

    sqlparse raises exactly one exception of its own, SQLParseError, and its
    message is contractual -- the Go port's Error.Error() must return the same
    string, and the contract says so.  Everything else that escapes is the
    interpreter's: a codec lookup, a decode failure, a type check.  Those are
    reported as a category so the comparison stays a comparison of behavior.
    """
    try:
        from sqlparse.exceptions import SQLParseError
    except Exception:  # noqa: BLE001 -- the reference should always import
        SQLParseError = ()  # type: ignore[assignment]
    if SQLParseError and isinstance(exc, SQLParseError):
        return f"SQLParseError\t{exc}"
    if isinstance(exc, NotImplementedError):
        return "category\tnot-implemented"
    return f"category\t{_error_category(exc)}"


def b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8", "surrogatepass")).decode("ascii")


def main(argv: list[str]) -> int:
    import argparse
    import warnings

    # The `unicode-escape` fallback in the library's lexer emits a
    # DeprecationWarning for every unrecognized escape it passes through, and the
    # docs exercises that path deliberately.  The warnings are not diagnostics
    # about this run, they are noise on a channel the verifier reads, and the Go
    # half has nothing corresponding to emit.  The graded answers all travel on
    # stdout, so silencing this leaves the comparison untouched.
    warnings.filterwarnings("ignore", category=DeprecationWarning)

    ap = argparse.ArgumentParser(description="reference probe")
    ap.add_argument("--docs", required=True, type=Path)
    ap.add_argument("--documents", required=True, type=Path)
    ap.add_argument("--spec", required=True, type=Path)
    ap.add_argument("--tier", default="", help="accepted and ignored; the "
                    "reference answers every tier from one process")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        print(f"{PROBE_VERSION} ops={len(OPS)}")
        for name in sorted(OPS):
            print(f"  {name}")
        return 0

    ctx = Context(
        args.docs,
        json.loads(args.documents.read_text(encoding="utf-8")),
        json.loads(args.spec.read_text(encoding="utf-8")),
    )

    # Line-buffered: the executor writes a request and reads its answer, and a
    # block-buffered writer would deadlock on the first case.
    out = sys.stdout
    for line in sys.stdin:
        if not line.strip():
            continue
        out.write(handle(ctx, line) + "\n")
        out.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
