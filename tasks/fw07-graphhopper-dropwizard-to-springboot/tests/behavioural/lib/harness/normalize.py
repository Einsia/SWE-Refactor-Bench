"""Deciding what a difference means.

The problem this file solves: two GraphHopper servers answering the same request
never produce byte-identical responses.  A `took` field, a request id, a Date
header, a heap number — all differ between two calls to the SAME server, let
alone between two builds of it.  Compare raw bytes and every case fails; hand-
maintain a list of fields to ignore and the list is wrong the first time a new
field appears, in the direction that hides real breakage.

So volatility is MEASURED rather than declared.  Each side is asked every case
twice.  Any JSON path whose value differs between a side's own two answers is
volatile *by observation*, and gets masked on both sides before comparison.  A
field that is stable within each side but differs between them is a real
difference, which is the entire signal this stage exists to detect.

The union is masked, not the intersection: if a field is volatile on either
side, it is unusable as evidence.  That is deliberately generous to the
submission, and the alternative is worse — a field that happens to be stable
across State A's two calls and moves on the submission's would otherwise be
reported as a difference when it is only noise.

One consequence worth stating plainly: a submission that makes a field volatile
which is stable upstream gets that field masked instead of failed.  That is the
right trade for a differential harness, and it is not a hole a cheat can drive
through — masking every field means masking the whole body, and the SHAPE of the
header map is not maskable at all: every name, and every name's multiplicity, is
always compared, as are the status and the media type.

Three things the measurement cannot reach, so all three are DECLARED below, each
in a named list with a reason per entry:

  A value fixed at process start.  The measurement asks one RUNNING server twice,
  so a field set when the JVM came up is stable within each side and differs
  between the sides — the exact shape of a real difference.  `/info`'s
  import_date is the case: the two sides import their graphs sequentially and are
  minutes apart by construction.  Declared masks are what this file exists to
  avoid, so each is a full PATH rather than a field name, and each carries its own
  PREDICATE — it applies only where BOTH sides answered something of the kind the
  field is declared to hold.  A field renamed, removed, or filled with a value of
  another type is not masked, and fails.

  A value that is a measurement of the server's own elapsed time.  Upstream reports
  one stopwatch twice — as the `X-GH-Took` header and as `info.took` in the body —
  and neither is reproducible.  Measurement cannot reach either reliably: the
  number is `Math.round()` of a duration in milliseconds, so on a fast route the
  two calls to one side often round to the SAME integer, and the measurement then
  reports the field as stable.  Whether the case then passes depends on whether the
  two sides happen to round alike, which is a coin flip.  Both are therefore
  declared: the header's value is replaced by a sentinel and its name compared like
  every other, and the body path is masked with a predicate that requires a number
  on both sides.  Dropping the header's name instead would let a submission stop
  sending a header a client can see, for free.

  The harness's own coordinates.  The two sides are deliberately given different
  ports, so a response that echoes the authority it was reached on differs by
  construction and the difference is the harness's doing rather than the
  submission's.  Each side's own loopback authority folds to one token before the
  headers are compared.  An authority that is not that side's own does not fold,
  and still differs.
"""
from __future__ import annotations

import datetime
import gzip
import json
import re
import zlib

# Header names dropped before comparison, with a reason each.  This list is
# short on purpose: every name here is a name a client cannot depend on, and
# anything a client CAN depend on has to survive to the comparison.
#
# Not in this list, and deliberately: Content-Type, Content-Encoding, Vary,
# Allow, Location, Cache-Control, ETag, and every Access-Control-* name.  Those
# are the contract.
_DROP_HEADERS = frozenset({
    "date",              # wall clock
    "server",            # names the container, which the migration changes
    "connection",        # hop-by-hop
    "keep-alive",        # hop-by-hop
    "transfer-encoding", # framing, not content; chunked-vs-length is the
                         # container's choice and invisible to a client
    "content-length",    # follows the body, and the body is compared directly.
                         # Kept out because a masked volatile field changes the
                         # length without changing the meaning.
})

# Headers whose NAME is part of the contract and whose VALUE is a measurement.
# The name and its multiplicity are compared exactly as for any other header; the
# value is replaced by a sentinel on both sides.
#
# This is not the same decision as _DROP_HEADERS and must not collapse into it.
# Dropping a name says "no client can depend on this header existing".  Masking a
# value says "the header is owed, its number is not reproducible".  A submission
# that stops sending one of these fails; a submission that sends a different
# number does not.
_OPAQUE_HEADERS = frozenset({
    "x-gh-took",         # `Math.round(took)` off a StopWatch, in ms, set by
                         # RouteResource, MapMatchingResource and
                         # NavigateResource.  Two calls to ONE server disagree,
                         # so no comparison of the value can mean anything.
})
_OPAQUE = "\x00srb-opaque\x00"


def header_map(pairs) -> dict[str, list[str]]:
    """Headers as a lowercased name -> list-of-values map.

    A map, not a sequence: HTTP does not order distinct header names, so
    comparing an ordered list fails on a reordering that no client could
    observe.  Repeats of one name ARE ordered and stay in order, because
    `Set-Cookie` and `Access-Control-Allow-Methods` mean different things in
    different orders.

    A name in _OPAQUE_HEADERS keeps its place and its count and loses its value.
    """
    out: dict[str, list[str]] = {}
    for name, value in pairs:
        low = name.lower()
        if low in _DROP_HEADERS:
            continue
        out.setdefault(low, []).append(
            _OPAQUE if low in _OPAQUE_HEADERS else value)
    return out


# --- the harness's own coordinates -------------------------------------------

_AUTHORITY = "\x00srb-authority\x00"


def fold_authority(headers: dict[str, list[str]],
                   port: int) -> dict[str, list[str]]:
    """Replace this side's own loopback authority with a token, everywhere.

    Every header value, not a list of header names: a value that echoes the
    address the request arrived on is the harness's artefact wherever it appears,
    and enumerating the names it can appear under would be a list that is wrong
    the first time a new one shows up — in the direction that fails a correct
    submission.  `Location` is the one the corpus reaches today (RootResource
    redirects `/` to `maps/`, and the container makes that absolute).

    Only THIS side's authority folds.  A submission whose Location named the
    reference's port, or any other host, still differs from the reference's
    token and still fails.
    """
    pattern = re.compile(
        r"(?:127\.0\.0\.1|localhost|\[::1\]|0\.0\.0\.0):" + str(port))
    return {name: [pattern.sub(_AUTHORITY, v) for v in values]
            for name, values in headers.items()}


def media_type(headers: dict[str, list[str]]) -> str | None:
    """The content type without its parameters.

    Charset is dropped rather than compared.  Both stacks serve UTF-8; they
    disagree about whether to SAY so, and `application/json` versus
    `application/json;charset=UTF-8` is a difference no JSON client can observe
    — RFC 8259 fixes the encoding.  Comparing it would fail correct submissions
    over a spelling.
    """
    values = headers.get("content-type")
    if not values:
        return None
    return values[0].split(";")[0].strip().lower()


# --- content coding ----------------------------------------------------------
# A compressed body is the same body.  `Content-Encoding` is a transfer property:
# two servers may pick different compression levels, or different zlib builds, and
# produce different bytes for identical content.  So the CODING is compared as a
# header like any other, and the BODY is compared after decoding.
#
# Not decoding was a real hole rather than a missing nicety.  A gzipped route
# response is not JSON, so its two answers from one side differ in bytes and parse
# as nothing: the case landed in the "not byte-stable and not JSON" branch of
# harness.diff and passed WITHOUT its body being compared at all — while the
# module's own docstring told the submitter the decoded body was compared.  The
# only case in the corpus that asks for gzip was therefore the only case whose
# body was never graded.

_DECODERS = {
    "gzip": lambda b: gzip.decompress(b),
    "x-gzip": lambda b: gzip.decompress(b),
    "deflate": lambda b: zlib.decompress(b, -zlib.MAX_WBITS),
}


def decode_body(headers: dict[str, list[str]], body: bytes):
    """Undo Content-Encoding.  Returns (bytes, error) — never raises.

    `identity`, absent, and an empty value all mean the bytes are already the
    body.  An error is returned rather than raised because a body that claims a
    coding it does not have is a finding to report, and which SIDE reported it is
    the whole content of that finding.

    An unknown coding is an error too, not a pass-through: `br` bytes compared as
    if they were the body would compare two ciphertexts, and equality there would
    be meaningless in both directions.
    """
    values = headers.get("content-encoding") or []
    coding = ",".join(values).strip().lower()
    if coding in ("", "identity"):
        return body, None
    if "," in coding:
        return None, (f"stacked content codings ({coding!r}) are not decoded by "
                      f"this harness")
    decoder = _DECODERS.get(coding)
    if decoder is None:
        return None, f"unknown content coding {coding!r}"
    try:
        return decoder(body), None
    except Exception as exc:                       # noqa: BLE001
        return None, (f"content-encoding says {coding!r} and the body does not "
                      f"decode as it: {type(exc).__name__}: {exc}")


# --- structural volatility ---------------------------------------------------

_MASK = "\x00srb-volatile\x00"


def _paths(node, prefix=()):
    """Every leaf path in a parsed JSON document.

    Lists are indexed rather than treated as sets, because a route's leg order
    is meaningful and an unordered comparison would accept a reversed itinerary.
    """
    if isinstance(node, dict):
        for k in node:
            yield from _paths(node[k], prefix + (k,))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _paths(v, prefix + (i,))
    else:
        yield prefix, node


def volatile_paths(first, second) -> set[tuple]:
    """Paths that differ between two answers from the SAME server.

    Structural differences — a key present once, a list of a different length —
    make the whole containing path volatile rather than raising.  A server whose
    response SHAPE moves between two identical calls has nothing stable to
    compare at that point, and that is a fact about the server, not an error in
    the harness.
    """
    a = dict(_paths(first))
    b = dict(_paths(second))
    out = {p for p in a.keys() & b.keys() if a[p] != b[p]}
    for p in a.keys() ^ b.keys():
        out.add(p)
    return out


def body_volatility(first: bytes, second: bytes) -> dict:
    """One side's measured volatility, from its own two answers to one case.

    Lives here rather than in capture.py because it decides what a difference
    MEANS, which is this file's job; capture.py decides how the answer is stored.
    Two callers need it — the ledger, and the config module, which starts its own
    servers and cannot use the ledger — and a second implementation of it would be
    a second definition of volatility.

    `unstable_bytes` says the two answers differ and are not JSON, so there is
    nothing structural to compare at all.  That is a fact about the endpoint.
    """
    if first == second:
        return {"paths": set(), "unstable_bytes": False}
    try:
        a, b = json.loads(first), json.loads(second)
    except (ValueError, UnicodeDecodeError):
        return {"paths": set(), "unstable_bytes": True}
    return {"paths": volatile_paths(a, b), "unstable_bytes": False}


def apply_mask(node, masked: set[tuple], prefix=()):
    """Replace every masked path's value with a sentinel.

    Returns a new structure; the input is not modified, because the same parsed
    body is reported in the failure detail and a mutated copy would make the
    report describe something the server never sent.
    """
    if isinstance(node, dict):
        return {k: apply_mask(v, masked, prefix + (k,))
                for k, v in node.items()}
    if isinstance(node, list):
        return [apply_mask(v, masked, prefix + (i,))
                for i, v in enumerate(node)]
    return _MASK if prefix in masked else node


# --- declared paths ----------------------------------------------------------
# Body paths the measurement cannot reach, each with the PREDICATE that says what
# the field is declared to hold.  The predicate is what keeps a declared mask from
# becoming a hole: the mask applies only where both sides answered a value of that
# kind, so a field renamed, removed, or retyped is still compared and still fails.
#
# Paths, not field names, so the same name elsewhere in a document is not covered
# and a submission cannot move a field under the mask.  Two entries, and adding a
# third should feel expensive — the whole design of this file is that volatility is
# observed rather than asserted, and every line here is an admission that the
# measurement could not reach one field.


def _is_instant(value) -> bool:
    """Whether `value` is an ISO-8601 instant."""
    if not isinstance(value, str):
        return False
    try:
        datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _is_duration_ms(value) -> bool:
    """Whether `value` is a non-negative number of milliseconds.

    `bool` is excluded explicitly: it is an `int` in Python, and a submission that
    answered `"took": true` should not be handed a mask for it.
    """
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and value >= 0)


_DECLARED_PATHS = {
    ("import_date",): _is_instant,
    # /info: when THIS process imported the graph.  The two sides are launched one
    # after the other, each paying a full import, so they are minutes apart by
    # construction and always will be.
    #
    # Its sibling `data_date` -- when the OSM extract itself was made -- is
    # deliberately NOT here.  That is a property of the input, identical on both
    # sides (2013-05-28T18:59:04Z for Andorra), and a difference in it means the
    # submission read different data, which is a finding.

    ("info", "took"): _is_duration_ms,
    # Every route, isochrone, match, spt and pt answer carries this: `Math.round()`
    # of the StopWatch the resource ran, in milliseconds.  It is the same number
    # the X-GH-Took header carries and it is masked for the same reason.
    #
    # Measured across a real run of 88 cases, 26 carried it: 20 passed only because
    # one side happened to answer two different values and the union masked it, 2
    # failed outright, and 4 agreed by coincidence.  Which of the three a case got
    # depended on whether two sub-millisecond durations rounded to the same integer
    # — so leaving it graded does not make the suite strict, it makes it flaky, and
    # a flaky check costs a correct submission points at random.
    #
    # A duration is also not a behaviour.  Grading it would fail a correct
    # submission for running on a loaded machine, which is a question this stage is
    # not equipped to ask and stage 2 does not claim to.
}


def declared_paths(left, right) -> set[tuple]:
    """Declared paths where BOTH sides hold a value the predicate accepts."""
    a = dict(_paths(left))
    b = dict(_paths(right))
    return {p for p, ok in _DECLARED_PATHS.items()
            if p in a and p in b and ok(a[p]) and ok(b[p])}


def mask_pair(left, right, left_volatile, right_volatile):
    """Mask both bodies, and return the two mask sets separately.

    Separately because they mean different things in a report: `measured` is what
    these two servers were observed to do, `declared` is what this file asserts in
    advance.  Adding them together would credit a measurement that was never
    made.
    """
    measured = left_volatile | right_volatile
    declared = declared_paths(left, right)
    both = measured | declared
    return (apply_mask(left, both), apply_mask(right, both), measured, declared)


def describe_path(path: tuple) -> str:
    """A JSON-path-ish rendering for failure reports: paths[0].instructions[3].text"""
    out = ""
    for part in path:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += f".{part}" if out else str(part)
    return out or "(root)"


def first_differences(left, right, limit=6) -> list[str]:
    """Up to `limit` human-readable differences between two masked bodies.

    The report is the product here.  "the bodies differ" costs a submission the
    same points as a precise message and teaches nobody anything, so this walks
    both trees and names the paths.
    """
    a = dict(_paths(left))
    b = dict(_paths(right))
    out = []
    for p in sorted(a.keys() & b.keys(), key=lambda t: tuple(map(str, t))):
        if a[p] != b[p]:
            out.append(f"{describe_path(p)}: reference={a[p]!r} submission={b[p]!r}")
            if len(out) >= limit:
                return out
    for p in sorted(a.keys() - b.keys(), key=lambda t: tuple(map(str, t))):
        out.append(f"{describe_path(p)}: present in reference, absent in submission")
        if len(out) >= limit:
            return out
    for p in sorted(b.keys() - a.keys(), key=lambda t: tuple(map(str, t))):
        out.append(f"{describe_path(p)}: absent in reference, present in submission")
        if len(out) >= limit:
            return out
    return out
