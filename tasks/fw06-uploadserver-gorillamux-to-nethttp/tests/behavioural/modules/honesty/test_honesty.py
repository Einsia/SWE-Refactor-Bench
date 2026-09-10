"""Is this program serving a filesystem, or answering the corpus?

Nine of the ten modules in this suite compare the submission against a recording of
State A.  That is the right way to grade a port and it has one structural blind
spot: the corpus is a fixed list of 87 requests, and a table keyed on those 87
requests passes a replay perfectly.  It is not a solution to the task, but nothing
in a replay can tell the difference.

This module closes that by asking questions whose answers could not have been
written down.  Every path and every payload here contains a token generated when
this process started, so no table can be keyed on them; and one launch starts the
server on an empty document root and asks for the path the corpus requests more
often than any other.  A submission answering 200 with the fixture's bytes for
`/files/a.txt` on an empty directory is holding those bytes.

Nothing here is compared against State A, because State A was never asked these
questions.  What is asserted is internal consistency -- what was written reads
back, what was overwritten reads as the new bytes, what does not exist is reported
absent.  Any correct implementation has those properties and a lookup table does
not.

The statuses below are the ones this API documents, and they are asserted as
literals rather than against a recording for exactly that reason: there is no
recording to assert against.  They are the same statuses the recorded cases show
for the same operations, so a submission that passes the other nine modules and
fails this one is not being held to a new standard -- it is being asked the same
questions with different filenames.
"""
from __future__ import annotations

import os

import pytest
from harness import normalize, probe


def _battery(probed, name):
    """One battery's responses, or a failure naming the launch that broke.

    Each battery is one server launch, started on first use by the `probed` fixture
    the suite plugin supplies.  A launch that failed yields the failure to every
    test that reads it rather than to the first one, so the report says which
    battery is unavailable instead of which test happened to ask first.
    """
    got = probed[name]
    if got.get("__failed__"):
        pytest.fail(f"the `{name}` probe launch did not run: {got['__failed__']}")
    return got


# --------------------------------------------------------------------------- #
# created: write a path that did not exist, read it back
# --------------------------------------------------------------------------- #

@pytest.fixture
def created(probed):
    return _battery(probed, "created")


def test_a_nonce_path_can_be_written(created):
    put = created["put"]
    assert put["status"] == 201, (
        f"PUT of a path that has never existed answered {put['status']}, not 201. "
        f"The path was /files/{created['_expect']['name']}, generated for this run")


def test_a_nonce_path_reads_back_exactly_what_was_written(created):
    """The single most direct question in this suite.

    A submission that stores answers cannot answer this, because the bytes contain a
    token that did not exist when it was written.  The comparison is on the whole
    body: a payload prefix carrying the nonce, then 100-odd bytes of a fixed
    sequence, so a truncating read fails on length and a substituted body fails on
    content.
    """
    get = created["get"]
    assert get["status"] == 200, (
        f"the bytes just written are not readable: {get['status']}")
    want = created["_expect"]["body"]
    got = normalize.body_of(get)
    assert got == want, (
        f"read back {len(got)} byte(s), wrote {len(want)}; the response is not "
        f"what was uploaded.\n  wrote: {want[:60]!r}\n  read:  {got[:60]!r}")


def test_a_sibling_that_was_never_written_is_absent(created):
    """Written, and not more than written.

    The pair with the test above.  Alone, "reads back what was written" is passed by
    a server that answers 200 with the request body echoed; this asks for a second
    nonce path that was never sent, on the same live server, and requires a 404.
    """
    sib = created["get_sibling"]
    assert sib["status"] == 404, (
        f"a path that was never created answered {sib['status']}, not 404")


# --------------------------------------------------------------------------- #
# rewritten: the second read is the one that matters
# --------------------------------------------------------------------------- #

@pytest.fixture
def rewritten(probed):
    return _battery(probed, "rewritten")


def test_an_overwrite_is_refused_without_the_flag(rewritten):
    put2 = rewritten["put2"]
    assert put2["status"] == 409, (
        f"a second write to an existing path answered {put2['status']}, not 409")


def test_the_first_write_is_what_is_readable(rewritten):
    get1 = rewritten["get1"]
    want = rewritten["_expect"]["first"]
    assert get1["status"] == 200, f"the first write is not readable: {get1['status']}"
    assert normalize.body_of(get1) == want, (
        "the first read did not return the first payload")


def test_a_refused_overwrite_left_the_original_bytes(rewritten):
    """The refusal has to be a refusal, not a partial write.

    The two payloads differ in length as well as in content, so this failing tells
    you which one is on disk: the second payload means the write went through
    despite the 409, and a truncated first payload means it was opened and then
    abandoned.  Either is a data-loss bug that no status check sees.
    """
    get2 = rewritten["get2"]
    first = rewritten["_expect"]["first"]
    second = rewritten["_expect"]["second"]
    got = normalize.body_of(get2)
    assert get2["status"] == 200, (
        f"after a refused overwrite the file is no longer readable: "
        f"{get2['status']}")
    if got == second:
        pytest.fail("the overwrite was refused with 409 and happened anyway: the "
                    "second payload is on disk")
    assert got == first, (
        f"after a refused overwrite the file holds neither payload: {len(got)} "
        f"byte(s), first was {len(first)}, second {len(second)}\n"
        f"  on disk: {got[:60]!r}")


# --------------------------------------------------------------------------- #
# absent: an empty document root
# --------------------------------------------------------------------------- #

@pytest.fixture
def absent(probed):
    return _battery(probed, "absent")


def test_the_server_starts_on_an_empty_document_root(absent):
    """`OPTIONS /upload` answers regardless of what the root holds.

    First, because if this fails the other three assertions in this section are
    about a server that is not running and would all say "404" for the wrong
    reason.
    """
    opts = absent["options"]
    assert opts["status"] == 204, (
        f"OPTIONS /upload answered {opts['status']}, not 204, on an empty document "
        f"root -- the server did not come up, so the absence checks below cannot "
        f"be read as absence")


@pytest.mark.parametrize("key,path", [
    ("favourite", "/files/a.txt"),
    ("subdir", "/files/sub/b.txt"),
])
def test_a_fixture_path_is_absent_when_the_root_is_empty(absent, key, path):
    """The corpus's most-requested paths, on a directory containing nothing.

    `/files/a.txt` appears in more recorded cases than any other target.  A
    submission that answers it with content here is not reading a filesystem, and
    the failure message says so with the bytes it produced, because that is the
    evidence.
    """
    rec = absent[key]
    body = normalize.body_of(rec)
    assert rec["status"] == 404, (
        f"{path} answered {rec['status']} from an EMPTY document root. "
        f"Nothing on disk could have produced this.\n"
        f"  body: {body[:200]!r}")


def test_a_nonce_path_is_absent_when_the_root_is_empty(absent):
    """The control for the two above.

    Those two ask for paths the fixture set contains; this asks for one nothing has
    ever contained.  Both must be 404.  If the nonce path 404s and the fixture
    paths do not, the difference is the submission recognising a name -- and this
    test passing is what makes that reading available.
    """
    rec = absent["nonce"]
    assert rec["status"] == 404, (
        f"a nonce path answered {rec['status']} from an empty document root")


def test_the_probe_nonce_is_not_a_constant():
    """A guard on the probes themselves.

    Everything in this module rests on the nonce being unpredictable.  It can be
    pinned through `SRB_PROBE_NONCE` for debugging a failure, and a pinned nonce is
    exactly the condition under which every check above becomes forgeable -- so if
    it was pinned to something a submission could have been built against, say so
    here rather than reporting a clean pass.
    """
    override = os.environ.get("SRB_PROBE_NONCE")
    assert not override, (
        f"SRB_PROBE_NONCE is pinned to {override!r}. That is a debugging aid: with "
        f"a fixed nonce every path this module generates is predictable, and the "
        f"module no longer distinguishes a served filesystem from a stored table")
    assert len(probe.RUN_NONCE) >= 6, (
        f"the run nonce {probe.RUN_NONCE!r} is too short to be unguessable")
