"""Multitenancy: a chart pushed to one tenant is visible there and nowhere else.

``--depth=N`` makes the first N path segments a tenant prefix, and State A peels
them off by hand before it decides which route it is looking at. That is the single
hardest thing in the dispatcher to re-host, because on a real router the tenant
prefix and the route are the same path -- ``/api/org1/repo1/charts/foo`` has to
mean tenant ``org1/repo1`` and chart ``foo``, and the router has no way to know
where one ends and the other begins unless the port tells it.

The failure this module exists for is the quiet one. A port that gets the split
slightly wrong still serves every tenant, and serves them each other's charts. So
the checks are a cross product: each tenant is asked about each tenant's chart, and
only the diagonal may be found. The charts are named after the run nonce, so
neither half of that can be answered from a table.

Stated twice, over the API and over ``index.yaml``, because those are assembled by
different code and the index is the document Helm actually consumes.
"""

from __future__ import annotations

import pytest

from harness import probe
from harness.probeassert import battery_or_fail

def test_tenant_pushes_were_accepted(probed):
    t = battery_or_fail(probed, "depth2", "tenant")
    bad = {k: v["status"] for k, v in t["pushes"].items() if v["status"] != 201}
    assert not bad, f"pushes to depth-2 tenants were not accepted: {bad}"


TENANT_VIEW_IDS = list(range(4))


@pytest.mark.parametrize("i", TENANT_VIEW_IDS)
def test_tenant_sees_only_its_own_chart(probed, i):
    """A chart pushed to one tenant is visible there and nowhere else.

    Both halves matter and they fail differently: not finding your own chart is a
    routing bug, and finding someone else's is a disclosure bug. Nonce names, so
    neither answer can be canned.
    """
    t = battery_or_fail(probed, "depth2", "tenant")
    views = sorted(t["views"].items())
    if i >= len(views):
        pytest.fail(f"only {len(views)} tenant views were recorded")
    key, o = views[i]
    if o["is_own"]:
        assert o["status"] == 200, (
            f"{key}: a tenant cannot see its own chart ({o['status']})")
    else:
        assert o["status"] == 404, (
            f"{key}: tenant {o['tenant']!r} can see {o['chart']!r}, which was "
            f"pushed to {o['owner']!r} -- charts are leaking across tenants")


@pytest.mark.parametrize("tenant", ["org1/repo1", "org2/repo2"])
def test_tenant_index_contains_only_its_own_chart(probed, tenant):
    """The same isolation, stated over ``index.yaml`` rather than the API.

    Checked separately because the index is assembled by different code from the
    per-chart lookup, and it is the document Helm actually consumes.
    """
    t = battery_or_fail(probed, "depth2", "tenant")
    o = t.get(f"index {tenant}")
    if o is None:
        pytest.fail(f"no index observation for {tenant}")
    text = o.get("text_full", "")
    own = probe.nonce_name("tenant", 0 if tenant == "org1/repo1" else 1)
    other = probe.nonce_name("tenant", 1 if tenant == "org1/repo1" else 0)
    assert own in text, (
        f"{tenant}: its own chart {own} is missing from its index.yaml")
    assert other not in text, (
        f"{tenant}: its index.yaml advertises {other}, which belongs to the "
        f"other tenant")
