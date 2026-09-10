"""Generated traffic, aimed at what a replay cannot see.

Every other behavioural module compares the submission against a recording of
State A.  That is the right way to grade a port, and it has one blind spot which
is structural rather than accidental: a submission whose answers are *stored* for
exactly the inputs the corpus uses passes a replay perfectly.  The corpus is a
fixed list of 87 requests; a table keyed on those 87 requests is a valid solution
to the replay and not a solution to the task.

So the probes here ask questions whose answers cannot have been written down,
because the question did not exist when the submission was written.  Two devices
do that work:

RUN_NONCE
    A per-run random token.  Every path, filename and byte string these probes
    create contains it.  A submission cannot carry a table keyed on
    ``/files/probe-a3f9c1.txt`` because that name did not exist until this process
    started.  The nonce is regenerated per import, which is why these probes run
    in the module process that asserts on them rather than being replayed out of a
    file some earlier module wrote -- a cached launch would carry a nonce the
    assertions cannot match, and would fail for a reason that has nothing to do
    with the submission.

An empty document root
    The corpus's most frequently requested path is ``/files/a.txt``.  Started on a
    document root with nothing in it, a real file server answers 404 for it.  A
    submission that answers 200 with the fixture's bytes has those bytes in it.
    This probe is cheap, it is one launch, and it is the single most direct test of
    "is this program reading a filesystem at all" in the whole suite.

What these probes are NOT for.  They do not check the shape of an answer against
State A -- they cannot, since State A was never asked these questions.  They check
internal consistency: that what was written can be read back, that what was
overwritten reads as the new bytes, that what does not exist is reported absent.
Those are properties any correct implementation has and a lookup table does not.
"""
from __future__ import annotations

import os
import secrets

from . import corpus

#: One token per process.  Short enough to read in a failure message, long enough
#: that it cannot collide with anything in the fixture set or the corpus.
RUN_NONCE = os.environ.get("SRB_PROBE_NONCE") or secrets.token_hex(3)

#: Bytes that go into probe uploads.  The nonce is inside the payload as well as
#: in the path, so a submission that echoes a stored body for a path it recognises
#: fails on content even if it guessed the name.
def payload(tag: str, size: int = 64) -> bytes:
    head = f"probe {tag} {RUN_NONCE} ".encode()
    return head + bytes((i * 7 + 11) % 251 for i in range(max(0, size - len(head))))


def probe_name(tag: str, ext: str = ".txt") -> str:
    """A filename the corpus does not contain and could not have contained."""
    return f"probe-{tag}-{RUN_NONCE}{ext}"


# --------------------------------------------------------------------------- #
# Launch specs
# --------------------------------------------------------------------------- #
#
# Each entry is (id, flags, seed) where `seed` says what the document root holds
# when the server starts:
#
#   "fixtures"  the frozen fixture set, as every recorded profile gets
#   "empty"     an existing but empty directory
#
# The flags are deliberately the default set for the first two.  These probes are
# about whether answers are computed, and adding a flag would mix that question
# with a configuration question that the `headers` and `auth` modules already ask.

PROBE_LAUNCHES = (
    ("created", [], "fixtures"),
    ("rewritten", [], "fixtures"),
    ("empty-root", [], "empty"),
)

#: Which launch each battery of assertions runs against.  A module that reads one
#: battery pays for one launch.
BATTERIES = {
    "created": "created",
    "rewritten": "rewritten",
    "absent": "empty-root",
}


# --------------------------------------------------------------------------- #
# The batteries: sequences of requests, run against one live server
# --------------------------------------------------------------------------- #
#
# A battery is a function of (send) -> dict of named responses, where `send` is
#     send(method, target, headers=None, body=None) -> record
# and the record is `harness.normalize.record`'s shape.  The functions do not
# assert; they collect.  The module that owns the battery makes the judgements,
# so that a failed expectation is reported as a failed check with a weight rather
# than as an exception inside a fixture.


def upload(send, target: str, content: bytes, filename: str = "probe.txt"):
    """PUT `content` to `target` the way this server accepts an upload.

    Through `corpus._mp` rather than through a second multipart builder, because
    the two would drift and only one of them is the one the recording was made
    with.

    The encoding is not incidental here.  `processUpload` obtains the body with
    `r.FormFile("file")`, so PUT shares POST's multipart path and a RAW request
    body fails to parse: State A answers 500 "cannot obtain the uploaded content",
    which the corpus records deliberately as `put-raw-body`.  A probe that sends
    raw bytes and expects 201 therefore asserts the opposite of State A, and
    fails every correct submission -- measured, it failed five of this module's
    checks against a working port.
    """
    headers, body = corpus._mp(filename, content)
    return send("PUT", target, headers=headers, body=body)


def battery_created(send) -> dict:
    """Upload something the corpus never named, then read it back.

    A PUT to a nonce path, a GET of the same path, and a GET of a sibling path
    that was never created.  Between them these pin down that the server wrote
    what it was given, serves back what it wrote, and does not serve what it was
    not given -- three properties a stored answer table cannot have all of.
    """
    name = probe_name("created")
    data = payload("created", 128)
    out = {}
    out["put"] = upload(send, f"/files/{name}", data, name)
    out["get"] = send("GET", f"/files/{name}")
    out["get_sibling"] = send("GET", f"/files/{probe_name('never')}")
    out["_expect"] = {"name": name, "body": data}
    return out


def battery_rewritten(send) -> dict:
    """Write, read, overwrite with different bytes, read again.

    The second read is the one that matters.  A submission that caches the first
    response, or that answers a GET from anything other than the file on disk,
    returns the first payload for the second read.  Both payloads carry the nonce
    and differ in length, so the failure names which one came back.
    """
    name = probe_name("rewritten")
    first = payload("rewritten-1", 96)
    second = payload("rewritten-2", 192)
    out = {}
    out["put1"] = upload(send, f"/files/{name}", first, name)
    out["get1"] = send("GET", f"/files/{name}")
    out["put2"] = upload(send, f"/files/{name}", second, name)
    out["get2"] = send("GET", f"/files/{name}")
    out["_expect"] = {"name": name, "first": first, "second": second}
    return out


def battery_absent(send) -> dict:
    """An empty document root, asked for the corpus's favourite paths.

    ``/files/a.txt`` is requested by more corpus cases than any other path.  On an
    empty root it does not exist.  The directory itself is asked for too, and a
    nonce path, so that "everything 404s because the server is broken" is
    distinguishable from "the server is reading an empty directory" -- a broken
    server fails the OPTIONS readiness check before this battery runs at all.
    """
    out = {}
    out["favourite"] = send("GET", "/files/a.txt")
    out["subdir"] = send("GET", "/files/sub/b.txt")
    out["nonce"] = send("GET", f"/files/{probe_name('absent')}")
    out["options"] = send("OPTIONS", "/upload")
    return out


BATTERY_FUNCS = {
    "created": battery_created,
    "rewritten": battery_rewritten,
    "absent": battery_absent,
}
