"""How a recorded answer and a live answer are compared.

One implementation of each dimension, called by the four comparison modules. The
modules choose *which* exchanges they are about; this file decides what it means
for two answers to the same request to agree.

Keeping it in one place is not only about duplication. These comparisons encode
every judgement about what a client can and cannot observe -- which headers are
part of the interface, whether a stack trace's frames are comparable across
frameworks, whether key order in a JSON body is contractual. Those judgements have
to be identical for a read and for a write, or the same defect would be a failure
in one module and invisible in another.

Every function here either returns ``None`` or raises ``AssertionError`` with a
message that names the request, the dimension and both values. None of them look
at the submission's source; they look at two HTTP responses.
"""

from __future__ import annotations

import json

#: Compared on every exchange, whether or not the exchange asked for them.
#:
#: These are the headers json-server sets deliberately, so a rewrite that drops
#: one has changed the interface even when the body is right. ``content-type``
#: leads because it is the most common near-miss: Fastify and Express spell the
#: charset differently by default.
#:
#: ``etag`` is here rather than left to the eleven exchanges that name it, because
#: 350 of the 369 recorded responses carry Express's body-derived weak tag and it
#: is cheap, exact, and the one header a plugin swap changes without changing a
#: body: a strong tag, a different hash, or a different base64 truncation all
#: match the body byte for byte and differ here. The eleven static responses waive
#: it per-exchange -- their tag is derived from size and mtime rather than content
#: -- and that waiver still applies.
ALWAYS = (
    "content-type",
    "etag",
    "cache-control",
    "pragma",
    "expires",
    "x-content-type-options",
    "vary",
    "access-control-allow-origin",
    "access-control-allow-credentials",
    "access-control-allow-methods",
    "access-control-allow-headers",
    "access-control-expose-headers",
    "x-total-count",
    "link",
    "location",
    "content-encoding",
    "accept-ranges",
    "allow",
)

#: Never compared as a value here.
#:
#: ``x-powered-by`` is Express's fingerprint. State A sends it because Express
#: does, so its *absence* is a migration requirement rather than a behaviour to
#: preserve -- comparing it by value would demand the submission keep it. It is
#: asserted, as an absence, in the `semantics` module.
NEVER = ("x-powered-by",)

#: Every value ``Case.body_mode`` may take, and which comparison implements it.
KNOWN_BODY_MODES = frozenset({"exact", "stack"})


def _clip(text: str, limit: int = 400) -> str:
    text = text if isinstance(text, str) else repr(text)
    return text if len(text) <= limit else text[:limit] + f"... (+{len(text) - limit})"


# --------------------------------------------------------------------------- #
# Status
# --------------------------------------------------------------------------- #

def status(pair) -> None:
    if pair.status_is_volatile:
        return
    assert pair.actual["status"] == pair.expected["status"], (
        f"{pair.describe()}\n"
        f"  expected status {pair.expected['status']}, "
        f"got {pair.actual['status']}\n"
        f"  body was: {_clip(pair.actual['body'])}")


def status_class(pair) -> None:
    """The class alone, so a 201-vs-200 slip reads differently from a 404."""
    if pair.status_is_volatile:
        return
    assert pair.actual["status"] // 100 == pair.expected["status"] // 100, (
        f"{pair.describe()}\n"
        f"  expected a {pair.expected['status'] // 100}xx, "
        f"got {pair.actual['status']}")


def no_new_server_error(pair) -> None:
    """A 5xx State A did not produce is always a defect.

    Separate from the equality check because it means something different: not
    "the answer changed" but "the rewrite crashes here". State A has three
    deliberate 500s of its own, and those are compared by equality like anything
    else.
    """
    if pair.expected["status"] >= 500:
        return
    assert pair.actual["status"] < 500, (
        f"{pair.describe()}\n"
        f"  State A returned {pair.expected['status']}, the submission returned "
        f"{pair.actual['status']}\n"
        f"  body was: {_clip(pair.actual['body'], 800)}")


# --------------------------------------------------------------------------- #
# Headers
# --------------------------------------------------------------------------- #

def content_type(pair) -> None:
    if pair.header_is_volatile("content-type"):
        return
    expected = pair.expected["headers"].get("content-type")
    actual = pair.actual["headers"].get("content-type")
    assert actual == expected, (
        f"{pair.describe()}\n"
        f"  expected Content-Type {expected}, got {actual}")


def header_values(pair, names=ALWAYS) -> None:
    """The named headers, compared by value, reported together."""
    waived = pair.waived_headers()
    problems = []
    for name in names:
        if name in waived:
            continue
        want = pair.expected["headers"].get(name)
        got = pair.actual["headers"].get(name)
        if want != got:
            problems.append(f"    {name}: expected {want!r}, got {got!r}")
    assert not problems, f"{pair.describe()}\n" + "\n".join(problems)


def case_headers(pair) -> None:
    """The headers this exchange singled out as its point."""
    waived = pair.waived_headers()
    problems = []
    for raw in pair.case.headers_extra:
        name = raw.lower()
        if name in waived:
            continue
        want = pair.expected["headers"].get(name)
        got = pair.actual["headers"].get(name)
        if want != got:
            problems.append(f"    {name}: expected {want!r}, got {got!r}")
    assert not problems, f"{pair.describe()}\n" + "\n".join(problems)


def _name_sets(pair) -> tuple[set[str], set[str]]:
    exempt = pair.waived_headers() | set(NEVER)
    want = {h for h in pair.expected["header_names"] if h not in exempt}
    got = {h for h in pair.actual["header_names"] if h not in exempt}
    return want, got


def no_extra_headers(pair) -> None:
    """Nothing State A did not send.

    Headers are a public interface: a client can branch on one appearing. A
    plugin that helpfully adds ``x-request-id`` or a security header has changed
    the observable surface, so this is strict, with the documented exemptions.
    """
    want, got = _name_sets(pair)
    extra = sorted(got - want)
    assert not extra, (
        f"{pair.describe()}\n"
        f"  headers the submission adds and State A never sent: {extra}")


def no_missing_headers(pair) -> None:
    want, got = _name_sets(pair)
    missing = sorted(want - got)
    assert not missing, (
        f"{pair.describe()}\n"
        f"  headers State A sent and the submission does not: {missing}")


# --------------------------------------------------------------------------- #
# Bodies
# --------------------------------------------------------------------------- #

def body(pair) -> None:
    """The bytes, compared as text.

    Not as re-serialised JSON: State A configures Express with ``json spaces =
    2``, so the indentation and the key order are part of what a client receives.
    Fastify's default is compact, which makes this one of the differences a
    rewrite has to notice rather than one it can normalise away.
    """
    if pair.body_is_volatile or pair.case.body_mode != "exact":
        return
    want, got = pair.expected["body"], pair.actual["body"]
    if want == got:
        return
    want_lines, got_lines = want.split("\n"), got.split("\n")
    for i, (a, b) in enumerate(zip(want_lines, got_lines)):
        if a != b:
            detail = [f"  first difference at line {i + 1}:",
                      f"    expected: {a[:200]!r}",
                      f"    actual:   {b[:200]!r}"]
            break
    else:
        detail = [f"  line counts differ: expected {len(want_lines)}, "
                  f"got {len(got_lines)}"]
    raise AssertionError(
        f"{pair.describe()}\n" + "\n".join(detail)
        + f"\n  expected length {len(want)}, actual {len(got)}")


def body_length(pair) -> None:
    if pair.body_is_volatile or pair.case.body_mode != "exact":
        return
    assert pair.actual["body_len"] == pair.expected["body_len"], (
        f"{pair.describe()}\n"
        f"  expected {pair.expected['body_len']} bytes, "
        f"got {pair.actual['body_len']}")


def json_shape(pair) -> None:
    """Keys, types and array lengths, independent of values.

    Survives the volatile exchanges, so a generated id does not stop the
    structure around it from being compared.
    """
    if pair.expected["json_shape"] is None:
        return
    assert pair.actual["json_shape"] == pair.expected["json_shape"], (
        f"{pair.describe()}\n"
        f"  the JSON structure differs (keys, types or array lengths)\n"
        f"  expected: {_clip(json.dumps(pair.expected['json_shape']))}\n"
        f"  actual:   {_clip(json.dumps(pair.actual['json_shape']))}")


def json_values(pair) -> None:
    """Values, with generated identifiers masked by type and length."""
    if pair.expected["json_masked"] is None:
        return
    assert pair.actual["json_masked"] == pair.expected["json_masked"], (
        f"{pair.describe()}\n"
        f"  JSON values differ once generated ids are masked\n"
        f"  expected: {_clip(json.dumps(pair.expected['json_masked']))}\n"
        f"  actual:   {_clip(json.dumps(pair.actual['json_masked']))}")


def json_style(pair) -> None:
    """Two-space indentation, no trailing newline -- State A's ``json spaces``.

    Implied by the byte comparison, and checked separately anyway: a submission
    that emits compact JSON should be told that its serialisation style is wrong,
    not merely that several hundred bodies differed.
    """
    if pair.expected["json"] is None:
        return
    got = pair.actual["json_style_violations"]
    want = pair.expected["json_style_violations"]
    assert got == want, (
        f"{pair.describe()}\n"
        f"  JSON serialisation style differs from State A: {got}\n"
        f"  (State A reported: {want})\n"
        f"  first 200 bytes: {pair.actual['body'][:200]!r}")


def stack_body(pair) -> None:
    """A stack-bearing body, compared exactly except for the frames.

    The frames name Express's internals and Node's, so a Fastify port cannot
    reproduce them and is not asked to. What survives the cut is everything a
    client can rely on: the status, the error class, the message, and -- for the
    ones that render Express's HTML error page -- every byte of that page. Only
    the ``at ...`` lines are dropped, and only for exchanges the request list
    marks ``body_mode="stack"``, never inferred from what came back.
    """
    if pair.case.body_mode != "stack":
        return
    want = pair.expected["body_no_frames"]
    got = pair.actual["body_no_frames"]
    assert got == want, (
        f"{pair.describe()}\n"
        f"  with stack frames removed, the body still differs\n"
        f"  expected: {_clip(want)!r}\n"
        f"  actual:   {_clip(got)!r}")


def stack_present(pair) -> None:
    """An error State A reports with a stack must not lose it.

    The frame contents are not comparable; their presence is. A submission that
    swallows the stack and returns a bare message has changed what a client sees.
    Compared as a boolean, because frame counts differ legitimately between
    frameworks.
    """
    if pair.case.body_mode != "stack":
        return
    assert pair.expected["stack_frames"] > 0, (
        f"{pair.key} is marked body_mode='stack' but State A's body has no "
        f"stack frames; the annotation in exchanges.py is wrong")
    assert pair.actual["stack_frames"] > 0, (
        f"{pair.describe()}\n"
        f"  State A reported {pair.expected['stack_frames']} stack frame(s) here; "
        f"the submission reported none\n"
        f"  actual body: {_clip(pair.actual['body'])!r}")


def stack_validator_shape(pair) -> None:
    """A stack body's ETag: same form as State A's, not the same value.

    The value cannot be compared -- it is a digest of bytes that contain absolute
    node_modules paths, so it moves with the checkout depth of whatever machine
    ran the capture. See `Pair.waived_headers`.

    The form can. If State A answered this error with a weak validator, the
    submission has to answer with one too: a client may issue an
    If-None-Match against an error response, and a port that stopped sending
    ETags on 5xx changed what that client sees. Presence and weakness are the
    parts a client can branch on.
    """
    if pair.case.body_mode != "stack":
        return
    want = pair.expected["headers"].get("etag")
    got = pair.actual["headers"].get("etag")
    if not want:
        assert not got, (
            f"{pair.describe()}\n"
            f"  State A sent no ETag with this error; the submission sent {got!r}")
        return
    assert got, (
        f"{pair.describe()}\n"
        f"  State A sent a weak ETag with this error ({want!r}); the submission "
        f"sent none. A client may still make a conditional request against an "
        f"error response.")
    assert all(v.startswith('W/"') and v.endswith('"') for v in got), (
        f"{pair.describe()}\n"
        f"  State A's ETag here is weak ({want!r}); the submission's is not "
        f"({got!r}). The value is not compared -- it digests the absolute paths "
        f"in the stack -- but strong-vs-weak is a real difference to a cache.")


def body_mode_known(pair) -> None:
    """Guards the request list itself: a typo must not silently skip a body."""
    assert pair.case.body_mode in KNOWN_BODY_MODES, (
        f"{pair.key} declares body_mode={pair.case.body_mode!r}, which no "
        f"comparison implements; its body would be graded by nothing")


def decompresses(pair) -> None:
    """A declared Content-Encoding must actually decode under that scheme."""
    if pair.case.decompress is None:
        return
    assert pair.actual["decompressed"] is not False, (
        f"{pair.describe()}\n"
        f"  the body did not decode as {pair.case.decompress}; Content-Encoding "
        f"was {pair.actual['headers'].get('content-encoding')!r}")
