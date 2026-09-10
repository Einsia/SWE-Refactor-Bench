"""The quirks a faithful rewrite keeps, each asserted as the quirk it is.

Every exchange named here is already compared byte for byte by `read`, `write` or
`options`. This module deliberately revisits a handful of them with a *narrower*
claim, and that duplication is the point: it turns "a body differed" into "the weak
ETag is not in Express's format", and "a status differed" into "DELETE under
``--id`` is supposed to fail".

These are the places where State A is surprising, which makes them the places a
rewrite is likeliest to quietly improve. Three of them are bugs. They are still the
contract -- a client of json-server 0.17.4 that works around a 500 is broken by a
rewrite that fixes it -- so they are graded like anything else.

Nothing here reads the submission's source. A quirk is a thing the server does.
"""

from __future__ import annotations

import json
import re

import pytest
from harness.sessions import Case, Session

pytestmark = pytest.mark.behaviour

#: Express's weak ETag: ``W/"<body-length-hex>-<27 chars of base64 sha1>"``.
#:
#: The 27 characters are a 28-character base64 encoding of a 20-byte digest with
#: its ``=`` padding removed, and the length prefix is the body length in hex.
#: Both halves matter: a strong tag, a different digest, or a different truncation
#: all fail while the body still matches.
EXPRESS_WEAK_ETAG = re.compile(r'^W/"[0-9a-f]+-[A-Za-z0-9+/]{27}"$')

#: A static file's tag instead comes from stat: size and mtime, both in hex.
EXPRESS_STATIC_ETAG = re.compile(r'^W/"[0-9a-f]+-[0-9a-f]+"$')


def _first(headers: dict, name: str) -> str | None:
    value = headers.get(name)
    if not value:
        return None
    return value[0] if isinstance(value, list) else value


# --------------------------------------------------------------------------- #
# The three deliberate 500s
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("key,why", [
    ("read::expand-missing-fk",
     "GET /users?_expand=author -- users carry no authorId, so the expansion "
     "looks up the foreign key `undefined` and the data layer throws"),
    ("create::create-duplicate-id",
     "POST /posts with an id that already exists -- the insert is attempted "
     "rather than rejected, and the data layer's duplicate-id error escapes"),
    ("custom-id::cid-delete",
     "DELETE /posts/4 under --id _id -- the cascade check reads the row's `id` "
     "field, which no longer exists, and throws on an undefined id; the row is "
     "removed anyway, so the 500 and the removal are both State A"),
])
def test_documented_server_error_is_reproduced(pairs, key, why):
    """A 500 State A produces on purpose has to still be a 500.

    This is the one place where "the submission returned 200 and State A returned
    500" is a failure rather than an improvement. A client written against
    json-server 0.17.4 can be relying on the error -- and more to the point, a
    rewrite that returns 200 here is doing something different with the request,
    which is exactly what is being measured.
    """
    pair = pairs[key]
    if pair.session_failure:
        pytest.fail(f"session {pair.session_id} did not start:\n"
                    f"{pair.session_failure}")
    assert pair.expected["status"] == 500, (
        f"{key} is listed as a deliberate 500 but the recording has "
        f"{pair.expected['status']}; this test's premise is wrong")
    assert pair.actual is not None, f"{key} was never recorded"
    assert pair.actual["status"] == 500, (
        f"{pair.describe()}\n"
        f"  State A answers 500 here ({why})\n"
        f"  the submission answered {pair.actual['status']}\n"
        f"  body: {pair.actual['body'][:400]}")


def test_delete_under_custom_id_still_removes_the_resource(pairs):
    """The strangest of the three: it fails *and* it works.

    ``cid-delete`` is a 500, and the very next exchange -- a GET of the same path
    -- is a 404. So the delete happened; only the response is broken. A rewrite
    that treats the 500 as "the operation failed" and rolls back is wrong in a way
    no single-response comparison would catch, because both of its answers would
    look individually plausible.
    """
    for key, want in (("custom-id::cid-delete", 500),
                      ("custom-id::cid-delete-verify", 404)):
        pair = pairs[key]
        if pair.session_failure:
            pytest.fail(f"session {pair.session_id} did not start:\n"
                        f"{pair.session_failure}")
        assert pair.actual is not None, f"{key} was never recorded"
        assert pair.actual["status"] == want, (
            f"{pair.describe()}\n"
            f"  expected {want} here: under --id _id the delete raises a 500 and "
            f"still removes the row, so the follow-up read must 404\n"
            f"  the submission answered {pair.actual['status']}")


# --------------------------------------------------------------------------- #
# The ETag format
# --------------------------------------------------------------------------- #

def test_weak_etag_is_in_express_format(pairs):
    """Not just equal to the recording -- the right *shape*, everywhere.

    Equality is already checked. This says what is wrong when it fails, and it
    catches the near-miss the task warns about: a plugin that emits a strong tag,
    or a different digest, or the same digest without the length prefix. All of
    those serve the correct body and a tag no Express client would have seen.
    """
    checked = wrong = 0
    problems = []
    for key, pair in sorted(pairs.items()):
        if pair.session_failure or pair.actual is None:
            continue
        if "etag" in pair.waived_headers():
            continue
        if not _first(pair.expected["headers"], "etag"):
            continue
        checked += 1
        got = _first(pair.actual["headers"], "etag")
        if got is None:
            wrong += 1
            problems.append(f"    {key}: State A sent an ETag, the submission "
                            f"sent none")
        elif not EXPRESS_WEAK_ETAG.match(got):
            wrong += 1
            problems.append(f"    {key}: {got!r} is not W/\"<len-hex>-<27 chars "
                            f"of base64 sha1>\"")
    assert checked, ("no exchange carried a comparable ETag; this test measured "
                     "nothing, which means the replay or the recording is empty")
    assert not problems, (
        f"{wrong} of {checked} responses carry an ETag that is not in Express's "
        f"weak format:\n" + "\n".join(problems[:20])
        + (f"\n    ... and {len(problems) - 20} more" if len(problems) > 20 else ""))


def test_static_etag_is_derived_from_stat(boot):
    """A static file's tag is ``W/"<size-hex>-<mtime-hex>"``, not a body digest.

    Express derives static validators from ``stat`` rather than from content, which
    is why the recording waives the value for these eleven responses -- it depends
    on how the tree was checked out. The *shape* does not, and it is the half that
    distinguishes ``@fastify/static`` configured like Express from a rewrite that
    serves static files through the JSON path and hashes them.
    """
    server = boot(Session(id="sem-static", cases=(), mutating=True))
    got = server.request(Case(id="sem-static-probe", path="/index.html"))
    assert got["status"] == 200, (
        f"GET /index.html returned {got['status']}; the static mount is not "
        f"serving\n  body: {got['body'][:300]}")
    tag = _first(got["headers"], "etag")
    assert tag, ("the static mount sent no ETag; Express sends one derived from "
                 "size and mtime, and a conditional client depends on it")
    assert EXPRESS_STATIC_ETAG.match(tag), (
        f"static ETag {tag!r} is not W/\"<size-hex>-<mtime-hex>\". A body digest "
        f"here means static files are not being served the way Express served "
        f"them, and the conditional-request exchanges depend on the difference.")
    assert _first(got["headers"], "last-modified"), (
        "the static mount sent no Last-Modified; it comes from the same stat as "
        "the ETag and the If-Modified-Since exchanges depend on it")


# --------------------------------------------------------------------------- #
# Serialisation and the framework fingerprint
# --------------------------------------------------------------------------- #

def test_json_is_two_space_indented_with_no_trailing_newline(pairs):
    """``json spaces = 2``, which Fastify does not do by default.

    Checked across every JSON response rather than on one, because the setting is
    global in State A and a rewrite that applies it in one serialiser and not
    another produces exactly this partial failure.
    """
    checked = 0
    problems = []
    for key, pair in sorted(pairs.items()):
        if pair.session_failure or pair.actual is None:
            continue
        if pair.expected["json"] is None or pair.body_is_volatile:
            continue
        checked += 1
        got = pair.actual["json_style_violations"]
        want = pair.expected["json_style_violations"]
        if got != want:
            problems.append(f"    {key}: {got} (State A: {want})")
    assert checked, "no JSON response was comparable; the replay looks empty"
    assert not problems, (
        f"{len(problems)} of {checked} JSON responses are not serialised the way "
        f"State A serialises -- two-space indent, no trailing newline:\n"
        + "\n".join(problems[:20])
        + (f"\n    ... and {len(problems) - 20} more" if len(problems) > 20 else ""))


@pytest.mark.srb_weight(0.0)
@pytest.mark.migration
def test_x_powered_by_is_gone(pairs):
    """Express's fingerprint, which State A sends on all 369 responses.

    The only header in the recording that the submission must *not* reproduce.
    Express sends it because Express is on the path, so its presence after the
    migration means Express is still there -- which is why it is excluded from the
    value comparisons and asserted here as an absence.

    Recorded at weight 0.0, and the docstring above says why better than a note
    could: State A sends this header on all 369 responses. Every other check in this
    module asks "State A did X, does yours"; this one asks "State A did X, it had
    better have stopped", which is a statement about whether the migration happened
    rather than about behaviour that a rewrite must preserve. `old_stack_retired` in
    stage 1 asks it over both trees, where a required-gate failure scores the
    submission zero.

    Still recorded, because it remains the cheapest single signal in the suite: a
    submission that merely kept Express shows up here on all 369 responses without
    anybody reading its source.
    """
    checked = 0
    present = []
    for key, pair in sorted(pairs.items()):
        if pair.session_failure or pair.actual is None:
            continue
        checked += 1
        if pair.actual.get("has_x_powered_by"):
            present.append(key)
    assert checked, "nothing was replayed; this test measured nothing"
    assert not present, (
        f"X-Powered-By is still being sent on {len(present)} of {checked} "
        f"responses. State A sends it because Express does; after the migration "
        f"nothing should:\n    " + "\n    ".join(present[:15])
        + (f"\n    ... and {len(present) - 15} more" if len(present) > 15 else ""))


@pytest.mark.srb_weight(0.0)
@pytest.mark.migration
def test_no_response_announces_a_retired_framework(pairs):
    """Any header whose value names the stack that was supposed to leave.

    Broader than X-Powered-By and cheap: a rewrite that installed a bridge often
    advertises it, and a server header naming Express is the same evidence under a
    different key.

    Recorded at weight 0.0 for the same reason as `test_x_powered_by_is_gone`: its
    subject is the retired stack, State A announces Express by construction, and
    `old_stack_retired` in stage 1 owns the judgement.
    """
    retired = ("express", "connect", "middie", "serve-static", "body-parser")
    found = []
    for key, pair in sorted(pairs.items()):
        if pair.session_failure or pair.actual is None:
            continue
        for name, values in pair.actual["headers"].items():
            joined = " ".join(values if isinstance(values, list) else [values])
            for needle in retired:
                if needle in joined.lower():
                    found.append(f"    {key}: {name}: {joined[:120]}")
                    break
    assert not found, (
        "these responses name a retired dependency in a header value:\n"
        + "\n".join(found[:15]))
