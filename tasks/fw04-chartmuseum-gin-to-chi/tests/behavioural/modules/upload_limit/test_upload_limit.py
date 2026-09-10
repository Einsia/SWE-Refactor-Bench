"""The request size limit, which the migration has to re-implement rather than swap.

``gin-contrib/size`` is on the retired list and has no drop-in equivalent. In State
A it is one ``engine.Use()`` line, it wraps *every* request rather than only the
uploads, and what it does when a body is too large is specific in a way that is
easy to miss.

One measured detail deserves stating up front, because it is the single most likely
thing to be got wrong: an over-limit upload is refused with **413 and a bare
``text/plain`` body**, while every other error this API produces is
``{"error":"..."}``. A port that sends the 413 through its own JSON error helper is
wrong in a way that looks right.

The sizes are jittered by the run nonce, so "which bodies are too big" cannot be a
fixed rule to hard-code, and the refusals are checked against storage afterwards:
a body rejected at the limit must not have been written on the way to being
rejected.

Its own launch, because the flag it needs breaks everything else -- an upload limit
small enough to trip would refuse the pushes every other module depends on.
"""

from __future__ import annotations

import pytest

from harness import probe
from harness.probeassert import battery_or_fail, obs_or_fail

UPLOAD_LABELS = [label for label, _, _ in probe.upload_sizes()]
ACCEPTED_LABELS = [label for label, _, ok in probe.upload_sizes() if ok]
REFUSED_LABELS = [label for label, _, ok in probe.upload_sizes() if not ok]

#: The exact refusal body. Measured on State A, and it is what ``gin-contrib/size``
#: writes: no trailing newline, no JSON envelope.
TOO_LARGE_BODY = "request too large"


def _limit(probed):
    return battery_or_fail(probed, "upload-limit", "upload_limit")


def _case(probed, label):
    return obs_or_fail(_limit(probed)["cases"], label, what="upload case")


# --- gin-contrib/size -------------------------------------------------------

@pytest.mark.parametrize("label", ACCEPTED_LABELS)
def test_upload_under_the_limit_is_accepted(probed, label):
    """A body inside ``--max-upload-size`` is still accepted.

    Stated first because it is what a too-aggressive re-implementation breaks: a
    limit applied to the wrong quantity, or off by a header's worth of bytes,
    rejects legitimate uploads.
    """
    o = _case(probed, label)
    assert o["status"] == 201, (
        f"a {o['body_bytes']}-byte upload (limit {probe.UPLOAD_LIMIT}) returned "
        f"{o['status']}, expected 201\n  body: {o.get('text')!r}")


@pytest.mark.parametrize("label", REFUSED_LABELS)
def test_upload_over_the_limit_is_refused_with_413(probed, label):
    """Over the limit is 413, not 400 and not 500."""
    o = _case(probed, label)
    assert o["status"] == 413, (
        f"a {o['body_bytes']}-byte upload (limit {probe.UPLOAD_LIMIT}) returned "
        f"{o['status']}, expected 413\n  body: {o.get('text')!r}")


@pytest.mark.parametrize("label", REFUSED_LABELS)
def test_refusal_body_is_bare_text(probed, label):
    """And the body is exactly ``request too large``, as plain text.

    The one non-JSON error in the API. Asserted on the body and the content type
    together, because getting one right and the other wrong is the common outcome.
    """
    o = _case(probed, label)
    if o["status"] != 413:
        pytest.skip("reported by test_upload_over_the_limit_is_refused_with_413")
    assert o.get("text") == TOO_LARGE_BODY, (
        f"the 413 body was {o.get('text')!r}, expected exactly "
        f"{TOO_LARGE_BODY!r} -- this error is bare text, not the "
        f'{{"error":"..."}} envelope the rest of the API uses')
    ctype = (o["headers"].get("content-type") or [""])[0]
    assert ctype.startswith("text/plain"), (
        f"the 413 carried Content-Type {ctype!r}, expected text/plain; a JSON "
        f"content type means the refusal went through the wrong error path")


def test_refused_uploads_wrote_nothing(probed):
    """None of the over-limit uploads reached storage.

    A limiter that reads the whole body before rejecting it can still hand the
    parsed chart to the backend on the way out. Checked on disk.
    """
    lim = _limit(probed)
    refused = {lim["cases"][label]["chart"] for label in REFUSED_LABELS}
    leaked = sorted(fn for fn in lim["storage"]
                    if any(name in fn for name in refused))
    assert not leaked, (
        f"uploads that were refused with 413 nevertheless wrote: {leaked}")


def test_the_limit_does_not_apply_to_reads(probed):
    """``GET /index.yaml`` still works with a 4 KiB upload limit.

    The limiter is installed for every request, not only for writes, so a
    re-implementation that checks the *response* size, or that rejects any request
    on a route capable of uploads, breaks reads. The seeded index is larger than the
    configured limit, which is what makes this test meaningful.
    """
    lim = _limit(probed)
    o = lim["read"]
    assert o["status"] == 200, (
        f"GET /index.yaml returned {o['status']} under "
        f"--max-upload-size={probe.UPLOAD_LIMIT}; the upload limit must not apply "
        f"to responses or to reads\n  body: {o.get('text')!r}")
    assert o["body_len"] > 0, "the index came back empty"

