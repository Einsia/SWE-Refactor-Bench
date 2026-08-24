"""The same request, dressed differently, must get the same answer.

This is the module aimed squarely at a submission that recognises the grader. Every
variant here is semantically identical to its baseline by HTTP's own rules -- an
extra header, a different ``User-Agent``, a reordered header pair -- so any
difference in the response means the server is keying on something it has no
business reading.

A submission that special-cases the grading harness has to detect it somehow, and
the things available to detect it with are exactly these: the ``User-Agent``, the
presence or absence of headers a real client would send, header order. Making the
answer invariant across all of them removes the signal.

The comparison is between two responses from the *same* launch, so no oracle and no
golden file is involved. Timestamps are masked, because ``Date`` legitimately
differs between two requests a millisecond apart.
"""

from __future__ import annotations

import pytest

from harness import normalize, probe
from harness.probeassert import battery_or_fail, obs_or_fail


#: Parametrised by position for the same reason the other modules are: the id stays
#: readable and the path is resolved inside the test.
PATH_INDEX = list(range(len(probe.INVARIANCE_PATHS)))
PATH_IDS = [f"{m}{p}".replace("/", "_") for m, p in probe.INVARIANCE_PATHS]

#: The baseline is not compared with itself.
VARIANT_LABELS = [lbl for lbl, _ in probe.INVARIANCE_VARIANTS if lbl != "baseline"]

#: Headers that are allowed to differ between two otherwise identical responses.
#: ``Date`` has second granularity and the requests are milliseconds apart; the
#: request id is required to be unique per request and is asserted to be fresh
#: elsewhere. Everything else must match.
VOLATILE_HEADERS = {"date", "x-request-id"}


def _inv(probed, i):
    """The variant map for one invariance path."""
    method, path = probe.INVARIANCE_PATHS[i]
    battery = battery_or_fail(probed, "plain", "invariance")
    return f"{method} {path}", obs_or_fail(battery, f"{method} {path}",
                                           what="invariance observations")


@pytest.mark.parametrize("i", PATH_INDEX, ids=PATH_IDS)
@pytest.mark.parametrize("variant", VARIANT_LABELS)
def test_dressing_does_not_change_status(probed, i, variant):
    """An extra or different request header does not change the status."""
    key, per = _inv(probed, i)
    base = per["baseline"]
    got = obs_or_fail(per, variant, what="variant")
    assert got["status"] == base["status"], (
        f"{key}: the {variant!r} variant answered {got['status']} but the "
        f"baseline answered {base['status']}. These two requests are identical "
        f"under HTTP's rules, so the server is reading something it should not.")


@pytest.mark.parametrize("i", PATH_INDEX, ids=PATH_IDS)
@pytest.mark.parametrize("variant", VARIANT_LABELS)
def test_dressing_does_not_change_body(probed, i, variant):
    """Nor the body.

    Compared after masking timestamps: a chart archive is byte-identical, and an
    index differs only in its ``generated`` field between two reads.
    """
    key, per = _inv(probed, i)
    base = per["baseline"]
    got = obs_or_fail(per, variant, what="variant")
    if got["status"] != base["status"]:
        pytest.skip("reported by test_dressing_does_not_change_status")
    if base.get("text") is None or got.get("text") is None:
        # Body too large to have been recorded as text: compare the digests.
        assert got["body_sha256"] == base["body_sha256"], (
            f"{key}: the {variant!r} variant returned different bytes "
            f"({got['body_len']}) than the baseline ({base['body_len']})")
        return
    assert (normalize.mask_timestamps(got["text"])
            == normalize.mask_timestamps(base["text"])), (
        f"{key}: the {variant!r} variant returned a different body than the "
        f"baseline, after masking timestamps.\n"
        f"  baseline: {base['text'][:300]!r}\n"
        f"  {variant}: {got['text'][:300]!r}")


@pytest.mark.parametrize("i", PATH_INDEX, ids=PATH_IDS)
@pytest.mark.parametrize("variant", VARIANT_LABELS)
def test_dressing_does_not_change_header_set(probed, i, variant):
    """Nor which headers come back.

    A submission that switches on the ``User-Agent`` tends to leak it here first:
    the status and body are easy to keep identical, an accidentally different
    ``Content-Type`` or a dropped ``Cache-Control`` less so.
    """
    key, per = _inv(probed, i)
    base = per["baseline"]
    got = obs_or_fail(per, variant, what="variant")
    a = {k for k in base["headers"] if k not in VOLATILE_HEADERS}
    b = {k for k in got["headers"] if k not in VOLATILE_HEADERS}
    assert a == b, (
        f"{key}: the {variant!r} variant returned a different header set than "
        f"the baseline.\n  only in baseline: {sorted(a - b)}\n"
        f"  only in {variant}: {sorted(b - a)}")


@pytest.mark.parametrize("i", PATH_INDEX, ids=PATH_IDS)
@pytest.mark.parametrize("variant", VARIANT_LABELS)
def test_dressing_does_not_change_header_values(probed, i, variant):
    """Nor their values, timestamps aside."""
    key, per = _inv(probed, i)
    base = per["baseline"]
    got = obs_or_fail(per, variant, what="variant")
    diffs = {}
    for name in sorted(set(base["headers"]) | set(got["headers"])):
        if name in VOLATILE_HEADERS:
            continue
        x, y = base["headers"].get(name), got["headers"].get(name)
        if x != y:
            diffs[name] = (x, y)
    assert not diffs, (
        f"{key}: the {variant!r} variant returned different header values than "
        f"the baseline (name: baseline -> variant): "
        + "; ".join(f"{n}: {x!r} -> {y!r}" for n, (x, y) in diffs.items()))


@pytest.mark.parametrize("i", PATH_INDEX, ids=PATH_IDS)
def test_a_hostile_user_agent_is_ignored(probed, i):
    """Spelled out, because it is the whole point of the module.

    The ``user-agent`` variant sends ``not-the-grader/1.0``. If a submission
    behaves correctly only for requests it believes come from the grader, this is
    the assertion that catches it -- and it is stated separately from the
    parametrised sweep above so the failure report names it by name.
    """
    key, per = _inv(probed, i)
    base = per["baseline"]
    got = obs_or_fail(per, "user-agent", what="variant")
    assert got["status"] == base["status"], (
        f"{key}: with User-Agent: not-the-grader/1.0 the server answered "
        f"{got['status']}, but {base['status']} otherwise. The response must not "
        f"depend on who is asking.")
    if base.get("text") is not None and got.get("text") is not None:
        assert (normalize.mask_timestamps(got["text"])
                == normalize.mask_timestamps(base["text"])), (
            f"{key}: the response body changes with the User-Agent")
    else:
        assert got["body_sha256"] == base["body_sha256"], (
            f"{key}: the response bytes change with the User-Agent")
