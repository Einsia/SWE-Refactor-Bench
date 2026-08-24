"""The comparisons every behavioural module makes, written once.

Six modules grade recorded traffic, and each grades the same five things about
each of its cases: the status line, which headers came back and in what order,
what those headers said, the body bytes, and whether the declared length matches
the bytes that arrived.  Those five live here rather than in each module so that
a change to how a difference is *reported* lands in one place, and so that the
six modules differ only in which cases they own.

Two rules hold everywhere in this file:

Nothing here reads the submitted tree.  Every argument is a `Pair` -- an expected
record captured from State A and an actual record captured from the submission --
and the only thing a comparison can conclude is that two responses differ.  There
is deliberately no access to source text, so no assertion in this suite can end up
grading how the port was written instead of what it does.

A difference is reported as a difference, never as a diagnosis.  The message says
which field disagreed and shows both sides; it does not say "you registered the
route with the wrong pattern", because a status mismatch has many causes and
naming one of them sends the reader to the wrong place.  The corpus's own `why`
string travels with every failure, which says what the case was *for* -- and that
is the part a reader needs.
"""
from __future__ import annotations

# `Content-Length` is graded like any other header by `header_values`, so it is not
# special-cased there.  It gets its own check as well because the two can disagree
# in a way no single-field comparison sees: a response can carry the recorded
# length and a body of a different size, and then it is internally inconsistent
# rather than merely different from the recording.
_LENGTH = "content-length"

# Headers whose recorded value is a mask.  A mask is not a hole -- the name and its
# position are still graded -- but the value comparison has to accept it, because
# what it stands for could not be reproduced.  See `harness.normalize`, which is
# where the masking is decided and documented.
_MASKS = ("<MASKED>", "<MASKED-WALLCLOCK>", "<BOUNDARY>")


def _masked(value: str) -> bool:
    return any(m in value for m in _MASKS)


def status(pair) -> None:
    """The status line, first, because everything after it is conditional on it."""
    assert pair.actual["status"] == pair.expected["status"], (
        f"status {pair.actual['status']}, recorded {pair.expected['status']}\n"
        f"{pair.render()}")


def header_names(pair) -> None:
    """Which headers came back, and in what order.

    Order is graded, not just membership.  net/http writes response headers in a
    fixed order for a given set of writes, so the order is a faithful record of
    which layer wrote what: a submission that sets `Content-Type` in a wrapper
    instead of letting `ServeContent` sniff it produces the same header with the
    same value in a different position.  That is a real behavioural difference for
    anything reading the wire, and it is invisible to a set comparison.
    """
    got = list(pair.actual.get("header_names_in_order") or [])
    want = list(pair.expected.get("header_names_in_order") or [])
    if got == want:
        return
    missing = [n for n in want if n not in got]
    extra = [n for n in got if n not in want]
    detail = ""
    if missing:
        detail += f"\n  absent:   {missing}"
    if extra:
        detail += f"\n  added:    {extra}"
    if not missing and not extra:
        detail = "\n  same headers, different order"
    raise AssertionError(
        f"response header order differs\n"
        f"  recorded: {want}\n  actual:   {got}{detail}\n{pair.render()}")


def header_values(pair) -> None:
    """What each header said, including repeats of the same name in order.

    Repeats are compared as a list rather than as a set: `Access-Control-Allow-*`
    is emitted once per response by State A, but a submission that adds a CORS
    middleware on top of the handler's own writes emits two, and the second one
    wins for some clients and not others.  A list comparison sees it.
    """
    got = pair.actual.get("headers") or {}
    want = pair.expected.get("headers") or {}
    problems = []
    for name in sorted(set(want) | set(got)):
        wv, gv = want.get(name), got.get(name)
        if wv is None:
            problems.append(f"  {name}: not recorded, actual {gv!r}")
            continue
        if gv is None:
            problems.append(f"  {name}: recorded {wv!r}, absent")
            continue
        if len(wv) != len(gv):
            problems.append(f"  {name}: recorded {len(wv)} value(s) {wv!r}, "
                            f"actual {len(gv)} value(s) {gv!r}")
            continue
        for w, g in zip(wv, gv):
            if w != g and not _masked(w):
                problems.append(f"  {name}: recorded {w!r}, actual {g!r}")
    assert not problems, ("header values differ\n" + "\n".join(problems)
                         + f"\n{pair.render()}")


def body(pair) -> None:
    """The body, byte for byte.

    Byte equality rather than a parsed comparison, and that is on purpose for the
    JSON bodies too.  This server's error bodies are written with a trailing
    newline in some paths and without it in others, and `{"ok":false,...}` has a
    fixed key order because it is produced by a struct rather than a map.  A JSON
    comparison would call all of those equal, and a client reading
    `Content-Length` would not.
    """
    got, want = pair.actual_body, pair.expected_body
    if got == want:
        return
    if got.startswith(want):
        tail = got[len(want):]
        raise AssertionError(
            f"body has {len(tail)} extra byte(s) after the recorded body: "
            f"{tail[:80]!r}\n{pair.render()}")
    if want.startswith(got):
        raise AssertionError(
            f"body is truncated: {len(got)} of {len(want)} recorded bytes\n"
            f"{pair.render()}")
    at = next((i for i, (a, b) in enumerate(zip(got, want)) if a != b),
              min(len(got), len(want)))
    raise AssertionError(
        f"body differs at byte {at} ({len(got)} bytes, recorded {len(want)})\n"
        f"  recorded: {want[max(0, at - 20):at + 40]!r}\n"
        f"  actual:   {got[max(0, at - 20):at + 40]!r}\n{pair.render()}")


def length_is_honest(pair) -> None:
    """`Content-Length`, when sent, against the bytes that actually arrived.

    This is the one check here that does not compare against the recording, and it
    is the only one that can fail on a response the recording never covered.  A
    header that promises more bytes than were written makes a client hang until it
    times out; one that promises fewer makes the next response on a keep-alive
    connection start mid-body.  Either is worse than an ordinary mismatch, so it is
    worth saying separately from "this header differs".

    HEAD is exempt: `Content-Length` describes the body the same request would
    have returned as a GET, and the body is correctly empty.  304 likewise carries
    no body by definition.

    Measured against `wire_len`, not against the recorded body, because the body
    in the record has been masked and masking changes its length: the two-range
    case replaces a 60-character boundary with `<BOUNDARY>` three times over, so
    the masked body is 150 bytes shorter than what the server sent.  Reading the
    length off the masked form reports a lying Content-Length on a byte-perfect
    response.  Records written without the field fall back to the masked length,
    which is wrong by that much and no worse than having no check at all.
    """
    if pair.method == "HEAD" or pair.actual["status"] == 304:
        return
    declared = pair.header(_LENGTH)
    if not declared:
        return
    try:
        n = int(declared[0])
    except ValueError:
        raise AssertionError(
            f"Content-Length is not a number: {declared[0]!r}\n{pair.render()}"
        ) from None
    actual = pair.actual.get("wire_len")
    if actual is None:
        actual = len(pair.actual_body)
    consequence = ("hangs waiting for bytes that never come" if n > actual
                   else "reads the start of the next response as body")
    assert n == actual, (
        f"Content-Length promises {n} byte(s), {actual} arrived -- a client "
        f"reading this response {consequence}\n{pair.render()}")


#: What a behavioural module runs, in the order a reader wants to see failures.
#: Status first: a 500 where a 200 was recorded makes every later difference a
#: consequence rather than a finding.
ALL = (status, header_names, header_values, body, length_is_honest)
