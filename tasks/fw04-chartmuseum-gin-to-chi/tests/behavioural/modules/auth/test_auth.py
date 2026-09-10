"""Basic auth, as a property of what the server did rather than of its status.

State A installs authentication as one middleware ahead of the dispatcher, so
"who may do what" is decided in the same place for every route. A port that moves
the check into the handlers has to get it right once per handler, and the way that
fails is not a missing 401 -- it is a 401 on a route that wrote anyway, or an open
route nobody remembered was mutating.

So each check here pairs the status with the storage tree that was left behind.
"401 and also did not write" is the property that matters; a 401 that persisted
would pass any status-only check, and did, in an early draft of this file.

Two launches, because the flag that distinguishes them is the interesting one:
with ``--basic-auth-user``/``--basic-auth-pass`` alone every request needs
credentials, and adding ``--auth-anonymous-get`` opens the reads and must leave
the writes exactly as closed as they were.
"""

from __future__ import annotations

import pytest

from harness import probe
from harness.probeassert import battery_or_fail

AUTH_LAUNCHES = ["auth-closed", "auth-anon-get"]
MUTATING_KEYS = [f"{m} {p}" for m, p in probe.MUTATING_ROUTES]
READ_KEYS = [f"{m} {p}" for m, p in probe.READ_ROUTES]


def _auth(probed, launch):
    return battery_or_fail(probed, launch, "auth")


@pytest.mark.parametrize("launch", AUTH_LAUNCHES)
@pytest.mark.parametrize("route", MUTATING_KEYS,
                         ids=[k.replace(" ", "_").replace("/", "-")
                              for k in MUTATING_KEYS])
def test_unauthenticated_write_is_refused(probed, launch, route):
    """With basic auth configured, an unauthenticated write gets 401.

    True with and without ``--auth-anonymous-get``: that flag opens reads, never
    writes. A port that wired the auth middleware onto only some routes -- easy to
    do when the routes are being re-declared one by one -- fails exactly here.
    """
    a = _auth(probed, launch)
    o = a["mutating"].get(route)
    if o is None:
        pytest.fail(f"{launch}: no observation for {route}")
    assert o["status"] == 401, (
        f"{launch}: unauthenticated {route} returned {o['status']}, expected 401"
        f"\n  body: {o.get('text')!r}")


@pytest.mark.parametrize("launch", AUTH_LAUNCHES)
@pytest.mark.parametrize("route", MUTATING_KEYS,
                         ids=[k.replace(" ", "_").replace("/", "-")
                              for k in MUTATING_KEYS])
def test_refused_write_sends_a_challenge(probed, launch, route):
    """The 401 carries ``WWW-Authenticate``, so a client can retry.

    A 401 without a challenge is a dead end for every Helm client and for ``helm
    repo add --username``.
    """
    a = _auth(probed, launch)
    o = a["mutating"].get(route)
    if o is None:
        pytest.fail(f"{launch}: no observation for {route}")
    if o["status"] != 401:
        pytest.skip("reported by test_unauthenticated_write_is_refused")
    assert o["headers"].get("www-authenticate"), (
        f"{launch}: the 401 for {route} carries no WWW-Authenticate header "
        f"(headers: {sorted(o['headers'])})")


@pytest.mark.parametrize("launch", AUTH_LAUNCHES)
def test_refused_writes_did_not_mutate_storage(probed, launch):
    """Nothing the refused writes carried reached the disk.

    The battery pushes a nonce-named chart at ``POST /api/charts`` without
    credentials. If that name appears in storage, the request was rejected in its
    response and honoured in its effect.
    """
    a = _auth(probed, launch)
    stored = sorted(a["storage"])
    leaked = [fn for fn in stored if "probe-auth-" in fn]
    assert not leaked, (
        f"{launch}: an unauthenticated push was refused but still wrote "
        f"{leaked}")


@pytest.mark.parametrize("route", READ_KEYS,
                         ids=[k.replace(" ", "_").replace("/", "-")
                              for k in READ_KEYS])
def test_anonymous_get_opens_reads(probed, route):
    """``--auth-anonymous-get`` lets unauthenticated reads through."""
    a = _auth(probed, "auth-anon-get")
    o = a["reading"].get(route)
    if o is None:
        pytest.fail(f"no observation for {route}")
    assert 200 <= o["status"] < 300, (
        f"with --auth-anonymous-get, unauthenticated {route} returned "
        f"{o['status']}, expected success")


@pytest.mark.parametrize("route", READ_KEYS,
                         ids=[k.replace(" ", "_").replace("/", "-")
                              for k in READ_KEYS])
def test_reads_are_closed_without_anonymous_get(probed, route):
    """And without the flag, the same reads are refused.

    The pair of these two tests is what shows the flag is actually consulted,
    rather than reads being open (or closed) unconditionally.
    """
    a = _auth(probed, "auth-closed")
    o = a["reading"].get(route)
    if o is None:
        pytest.fail(f"no observation for {route}")
    assert o["status"] == 401, (
        f"without --auth-anonymous-get, unauthenticated {route} returned "
        f"{o['status']}, expected 401")


