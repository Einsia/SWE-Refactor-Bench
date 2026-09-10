"""The candidate the image build runs, to prove the grade-time path works.

Not a test of the migration.  It asserts only what has to be true for ANY
candidate to mean anything, and it is run through `stage3.py` exactly as the
harness runs a real one -- same driver, same fixtures, same pytest invocation.
That is the point of it: a proof layer that built and booted State A by its own
private commands would prove those commands work, which is not the claim anyone
needs.  The claim needed is that a pytest file handed to run-candidate.sh reaches
two live servers.

What it checks, and why each one is worth a build failure:

  1. Both profiles are present and reachable, and /info answers 200 with JSON.
     If this fails at grade time every candidate "fails on the original", the
     adjudicator upholds none of them, and all six rounds record a survival that
     was never earned -- the submission is paid 60 points for a broken image.
     This is the single most valuable assertion in the stage and the reason the
     proof layer exists.

  2. The `base` profile routes.  A server that binds a port before its graph is
     usable would pass a bare readiness probe and then answer 500 to everything;
     the readiness probe in stage3.py is deliberately weak (any status counts, so
     that a submission cannot be failed for a slow first response), so this is
     where "the graph actually imported" gets established.

  3. The two profiles differ in the way the stage depends on: /route-pt exists
     under `pt` and does not exist under `base`.  Three of the six rounds are
     pointed at conditional registration, and if both servers were booted from
     the same config -- a substitution bug in write_config, a copied filename --
     those rounds would be attacking a distinction that is not there, and would
     report nothing found for a reason that is the harness's fault.

  4. The admin connector answers on a different port from the application.  It is
     a graded surface that stage3.py deliberately does NOT probe for readiness,
     so nothing else in the path would notice if it never came up.

Every assertion is about the ORIGINAL, because at image-build time there is no
submission -- the tree under test is State A, unpacked from data/original.tar.gz.
So each of these also states what State A does, which is the reference the whole
stage is measured against.
"""
from __future__ import annotations

import srbcandidate as rc


def test_both_profiles_are_up(servers):
    assert set(servers) == {"base", "pt"}, (
        f"expected both graph profiles, got {sorted(servers)}. Three of the six "
        f"rounds depend on having one server with transit configured and one "
        f"without."
    )
    for name, client in sorted(servers.items()):
        resp = client.get("/info")
        assert resp.status == 200, (
            f"{name}: /info answered {resp.status}, not 200. If this is the state "
            f"at grade time, no candidate can pass on the original and every "
            f"round pays out."
        )
        assert rc.media_type(resp) == "application/json", (
            f"{name}: /info is {rc.media_type(resp)!r}, not JSON"
        )
        body = rc.as_json(resp)
        assert "profiles" in body, f"{name}: /info has no profiles: {sorted(body)}"


def test_base_routes(server):
    """The graph imported and the routing stack answers, not just the port."""
    resp = server.get(f"/route?point={rc.AD_A}&point={rc.AD_B}&profile=car")
    assert resp.status == 200, (
        f"a car route across Andorra answered {resp.status}: "
        f"{resp.body[:300]!r}. The port is bound but the graph is not usable."
    )
    body = rc.as_json(resp)
    assert body.get("paths"), f"a 200 with no paths: {sorted(body)}"
    assert body["paths"][0]["distance"] > 0


def test_transit_is_registered_only_where_it_is_configured(servers):
    """The distinction three rounds are pointed at, asserted rather than assumed."""
    target = (f"/route-pt?point={rc.BEATTY_A}&point={rc.BEATTY_B}"
              f"&pt.earliest_departure_time={rc.PT_DEPARTURE}")
    on_pt = servers["pt"].get(target)
    assert on_pt.status == 200, (
        f"/route-pt answered {on_pt.status} on the transit profile: "
        f"{on_pt.body[:300]!r}. The GTFS feeds did not load, and the round "
        f"assigned to the transit stack would attack nothing."
    )
    on_base = servers["base"].get(target)
    assert on_base.status == 404, (
        f"/route-pt answered {on_base.status} on the profile with no GTFS feed, "
        f"expected 404. Either both servers were started from the same "
        f"configuration -- which would make the conditional-registration rounds "
        f"meaningless -- or registration is not conditional at all."
    )


def test_admin_connector_is_separate(server):
    """A graded surface nothing else in the launch path would notice was missing."""
    assert server.admin_port != server.app_port, (
        f"both connectors are on port {server.app_port}; the split between the "
        f"application and the admin surface is part of what was ported"
    )
    resp = server.admin("/healthcheck")
    assert resp.status in (200, 500), (
        f"/healthcheck on the admin connector answered {resp.status}: "
        f"{resp.body[:200]!r}. 500 is what this endpoint returns when a check "
        f"reports unhealthy, so it is accepted here -- the assertion is that the "
        f"connector is up and serving, not that every check is green."
    )
    assert rc.media_type(resp) == "application/json"
