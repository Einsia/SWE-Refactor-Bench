"""What the app persisted, across all 100 scenarios.

State A persists exactly one thing: the JWT, under the key ``id_token``. The key
is part of the contract rather than an implementation choice, and the reason is
the upgrade. A returning user arrives with a session already in their browser --
that is what the ``token`` scenarios model, seeding localStorage before the first
load -- and a port that stores the same token under ``token`` or ``auth.jwt``
silently signs every existing user out the moment it ships. Nothing in the DOM
would show it, and the app would look perfect to anyone testing with a fresh
browser profile.

Both halves matter. The key set is asserted exactly, so a port that keeps
``id_token`` *and* adds a cache of the user object under another key fails: it has
changed what the app leaves behind on a shared machine. The value is asserted with
the token symbolised, so the contract is "the current user's token is what was
stored", not "this string was stored".
"""

from __future__ import annotations

import pytest

import srbobserve as obs

SCENARIOS = obs.SCENARIOS


def _value_params():
    out = []
    for sid in SCENARIOS:
        for key in sorted(obs.reference(sid)["observed"]["storage"]):
            out.append(pytest.param(sid, key, id=f"{sid}::{key}"))
    return out


@pytest.mark.parametrize("sid", SCENARIOS)
def test_storage_keys(sid: str) -> None:
    """Exactly the same localStorage keys exist -- no more, no fewer."""
    want = sorted(obs.reference(sid)["observed"]["storageKeys"])
    got = sorted(obs.observed(sid)["observed"]["storageKeys"])
    assert got == want, (
        f"localStorage holds {got}, expected {want}. State A persists the JWT "
        "under 'id_token' and nothing else; the key is part of the contract "
        "because an existing session has to survive the migration."
    )


@pytest.mark.parametrize("sid,key", _value_params())
def test_storage_value(sid: str, key: str) -> None:
    """The persisted value under this key is what State A persisted."""
    want = obs.reference(sid)["observed"]["storage"][key]
    got_all = obs.observed(sid)["observed"]["storage"]
    if key not in got_all:
        pytest.fail(f"localStorage has no '{key}'; State A stored {want!r} there")
    assert got_all[key] == want, (
        f"localStorage['{key}'] is {got_all[key]!r}, expected {want!r}"
    )
