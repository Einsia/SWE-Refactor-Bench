"""Status codes: one test per measured case, plus per-profile launch evidence.

Status is separated from headers and body because it is the one field that is
meaningful for every case without exception, including the 31 cases whose body is
deliberately not compared. A router migration is most likely to go wrong here
first: chi and Gin disagree about trailing slashes, about which method mismatch
yields 404 versus 405, and about whether an unmatched path reaches a catch-all at
all. ChartMuseum registers *no* routes with Gin -- it hangs one ``NoRoute``
handler off a 207-line hand-written ``match()`` -- so every one of these 728
numbers comes from code the migration has to re-host without changing.
"""

from __future__ import annotations

import pytest

import reference as ref


# Licensed because `status_is_volatile` is State A's own two capture runs
# disagreeing, which no submission can cause.  Deliberately NOT applied to
# `test_profile_answered_every_case` below: that one skips when the profile failed
# to launch, which a submission very much can cause, and licensing it would let a
# server that never started shrink the denominator it is scored over.
@pytest.mark.srb_skip_ok
@pytest.mark.parametrize("key", ref.ALL_KEYS)
def test_status_matches(pair, key):
    """The response status is exactly what State A returned."""
    if pair.status_is_volatile:
        pytest.skip("status was not stable across two runs of State A")
    assert pair.actual["status"] == pair.expected["status"], (
        f"{pair.describe()}: expected HTTP {pair.expected['status']}, "
        f"got {pair.actual['status']}")


@pytest.mark.parametrize("profile_id", ref.PROFILE_IDS)
def test_profile_launched(pairs, profile_id):
    """The submission started under this profile's flag set.

    One test per profile, so a flag combination the migration broke is named once
    rather than inferred from a wall of case failures. This is also the only place
    a launch failure is reported as a single fact: every other test for the
    profile will fail too, but this one says why.
    """
    mine = [p for p in pairs.values() if p.profile_id == profile_id]
    assert mine, f"{profile_id}: no cases were collected for this profile"
    broken = [p for p in mine if p.launch_error]
    if broken:
        p = broken[0]
        tail = p.log[-3000:] if p.log else "(no output)"
        pytest.fail(
            f"{profile_id}: server did not start with flags "
            f"{list(ref.PROFILES[profile_id]['flags'])}: {p.launch_error}\n"
            f"--- process output ---\n{tail}")


@pytest.mark.parametrize("profile_id", ref.PROFILE_IDS)
def test_profile_answered_every_case(pairs, profile_id):
    """Every case in this profile produced a response.

    A profile can launch and then die partway through -- a panic on the third
    request leaves the rest unanswered. Without this the missing cases would each
    fail separately with 'no response recorded', which describes the symptom of a
    crash rather than the crash.
    """
    mine = [p for p in pairs.values() if p.profile_id == profile_id]
    if any(p.launch_error for p in mine):
        pytest.skip("profile did not launch; reported by test_profile_launched")
    missing = sorted(p.case_id for p in mine if p.actual is None)
    if missing:
        log = next((p.log for p in mine if p.log), "")
        pytest.fail(
            f"{profile_id}: {len(missing)} of {len(mine)} cases got no response "
            f"-- the server most likely stopped mid-profile. First missing: "
            f"{missing[:8]}\n--- process output ---\n{log[-3000:]}")
