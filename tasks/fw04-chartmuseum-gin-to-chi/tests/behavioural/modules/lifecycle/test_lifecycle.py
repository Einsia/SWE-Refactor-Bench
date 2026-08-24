"""Charts whose names were invented after the submission was written.

Every recorded expectation in this task is finite, and a finite expectation can be
answered from a table. A submission that returned canned bytes for each request in
the corpus would compare perfectly against the recording while storing nothing at
all. So these tests push charts named with a nonce generated at grading time and
then insist the server behave like something that really does store charts:

* the push is accepted, and the object appears **on disk** with the bytes that
  were uploaded -- checked by reading the storage directory, not by asking the API
* the chart is then listed, describable, and downloadable byte-for-byte
* a second push of the same version is refused with 409, or accepted under
  ``--allow-overwrite``
* a delete removes it from disk, and afterwards it is gone from every view

Several independent charts per launch, so a defect that only affects the first push
or only the last is still caught.
"""

from __future__ import annotations

import pytest

from harness import probe
from harness.probeassert import battery_or_fail


LAUNCHES = ["write", "overwrite"]
CHART_IDS = list(range(probe.N_NONCE))


def _chart(probed, launch, i):
    life = battery_or_fail(probed, launch, "lifecycle")
    name = probe.nonce_name("life", i)
    rec = life["charts"].get(name)
    if rec is None:
        pytest.fail(f"{launch}: no lifecycle record for {name}")
    return rec


@pytest.mark.parametrize("launch", LAUNCHES)
@pytest.mark.parametrize("i", CHART_IDS)
def test_push_accepted(probed, launch, i):
    """A freshly named chart is accepted with 201."""
    r = _chart(probed, launch, i)
    assert r["push"]["status"] == 201, (
        f"{launch}: pushing {r['name']}-{r['version']} returned "
        f"{r['push']['status']}, expected 201\n  body: {r['push'].get('text')!r}")


@pytest.mark.parametrize("launch", LAUNCHES)
@pytest.mark.parametrize("i", CHART_IDS)
def test_push_wrote_the_object_to_storage(probed, launch, i):
    """The uploaded bytes are on disk, under the backend's own root.

    The sharpest test in the suite. It does not ask the server anything -- it reads
    the storage directory. A handler that returns 201 without persisting passes
    every status and header test in the behavioural suite and fails here.
    """
    r = _chart(probed, launch, i)
    want_name = f"{r['name']}-{r['version']}.tgz"
    files = r["storage_after_push"]
    assert want_name in files, (
        f"{launch}: after a 201 for {want_name}, no such object exists in "
        f"storage. Present: {sorted(files)[:12]}")
    assert files[want_name] == r["uploaded_sha256"], (
        f"{launch}: {want_name} is on disk but its bytes differ from what was "
        f"uploaded (disk {files[want_name][:16]}..., "
        f"uploaded {r['uploaded_sha256'][:16]}...)")


@pytest.mark.parametrize("launch", LAUNCHES)
@pytest.mark.parametrize("i", CHART_IDS)
def test_chart_is_listed_after_push(probed, launch, i):
    """``GET /api/charts/<name>`` finds it, and reports the version pushed."""
    r = _chart(probed, launch, i)
    o = r["list_one"]
    assert o["status"] == 200, (
        f"{launch}: GET /api/charts/{r['name']} returned {o['status']} after a "
        f"successful push")
    doc = o.get("json")
    assert isinstance(doc, list) and doc, (
        f"{launch}: expected a non-empty JSON list for {r['name']}, got "
        f"{doc!r}")
    versions = {e.get("version") for e in doc if isinstance(e, dict)}
    assert r["version"] in versions, (
        f"{launch}: {r['name']} lists versions {sorted(versions)}, which does "
        f"not include the pushed {r['version']}")


@pytest.mark.parametrize("launch", LAUNCHES)
@pytest.mark.parametrize("i", CHART_IDS)
def test_chart_is_describable_after_push(probed, launch, i):
    """``GET /api/charts/<name>/<version>`` returns that chart's metadata."""
    r = _chart(probed, launch, i)
    o = r["describe"]
    assert o["status"] == 200, (
        f"{launch}: GET /api/charts/{r['name']}/{r['version']} returned "
        f"{o['status']}")
    doc = o.get("json")
    assert isinstance(doc, dict), f"{launch}: expected a JSON object, got {doc!r}"
    assert doc.get("name") == r["name"] and doc.get("version") == r["version"], (
        f"{launch}: describe returned name={doc.get('name')!r} "
        f"version={doc.get('version')!r}, expected {r['name']!r}/{r['version']!r}")


@pytest.mark.parametrize("launch", LAUNCHES)
@pytest.mark.parametrize("i", CHART_IDS)
def test_download_returns_the_uploaded_bytes(probed, launch, i):
    """The archive comes back byte-identical to what was uploaded."""
    r = _chart(probed, launch, i)
    o = r["download"]
    assert o["status"] == 200, (
        f"{launch}: GET /charts/{r['name']}-{r['version']}.tgz returned "
        f"{o['status']}")
    assert o["body_sha256"] == r["uploaded_sha256"], (
        f"{launch}: downloaded archive differs from the uploaded one "
        f"(got {o['body_sha256'][:16]}..., uploaded {r['uploaded_sha256'][:16]}"
        f"..., {o['body_len']} vs {r['uploaded_len']} bytes)")


@pytest.mark.parametrize("launch", LAUNCHES)
@pytest.mark.parametrize("i", CHART_IDS)
def test_index_lists_the_new_chart_with_its_digest(probed, launch, i):
    """``index.yaml`` gains the chart, with the digest of the real bytes.

    The index is what Helm reads, so a chart that stores and downloads but never
    reaches the index is invisible in practice.
    """
    r = _chart(probed, launch, i)
    text = r["index"].get("text_full", "")
    assert r["name"] in text, (
        f"{launch}: index.yaml does not mention {r['name']} after a successful "
        f"push")
    assert r["uploaded_sha256"] in text, (
        f"{launch}: index.yaml mentions {r['name']} but not its digest "
        f"{r['uploaded_sha256'][:16]}... -- the advertised digest does not match "
        f"the bytes that were stored")


@pytest.mark.parametrize("i", CHART_IDS)
def test_repush_conflicts_without_allow_overwrite(probed, i):
    """A second push of the same version is refused with 409.

    This is what stops a CI job from silently replacing a released chart, so it is
    a contract rather than an implementation detail.
    """
    r = _chart(probed, "write", i)
    assert r["repush"]["status"] == 409, (
        f"re-pushing {r['name']}-{r['version']} returned "
        f"{r['repush']['status']}, expected 409 without --allow-overwrite\n"
        f"  body: {r['repush'].get('text')!r}")


@pytest.mark.parametrize("i", CHART_IDS)
def test_repush_accepted_with_allow_overwrite(probed, i):
    """Under ``--allow-overwrite`` the same push succeeds instead."""
    r = _chart(probed, "overwrite", i)
    assert r["repush"]["status"] == 201, (
        f"with --allow-overwrite, re-pushing {r['name']}-{r['version']} returned "
        f"{r['repush']['status']}, expected 201\n"
        f"  body: {r['repush'].get('text')!r}")


@pytest.mark.parametrize("launch", LAUNCHES)
@pytest.mark.parametrize("i", CHART_IDS)
def test_delete_accepted(probed, launch, i):
    r = _chart(probed, launch, i)
    assert r["delete"]["status"] == 200, (
        f"{launch}: DELETE /api/charts/{r['name']}/{r['version']} returned "
        f"{r['delete']['status']}, expected 200\n"
        f"  body: {r['delete'].get('text')!r}")


@pytest.mark.parametrize("launch", LAUNCHES)
@pytest.mark.parametrize("i", CHART_IDS)
def test_delete_removed_the_object_from_storage(probed, launch, i):
    """The object is gone from disk, again read directly rather than asked about."""
    r = _chart(probed, launch, i)
    gone = f"{r['name']}-{r['version']}.tgz"
    files = r["storage_after_delete"]
    assert gone not in files, (
        f"{launch}: after a successful DELETE, {gone} is still present in "
        f"storage -- the delete was reported but not performed")


@pytest.mark.parametrize("launch", LAUNCHES)
@pytest.mark.parametrize("i", CHART_IDS)
def test_download_after_delete_is_404(probed, launch, i):
    r = _chart(probed, launch, i)
    o = r["download_after_delete"]
    assert o["status"] == 404, (
        f"{launch}: after deleting {r['name']}-{r['version']}, downloading it "
        f"returned {o['status']}, expected 404")


@pytest.mark.parametrize("launch", LAUNCHES)
@pytest.mark.parametrize("i", CHART_IDS)
def test_describe_after_delete_is_404(probed, launch, i):
    r = _chart(probed, launch, i)
    o = r["describe_after_delete"]
    assert o["status"] == 404, (
        f"{launch}: after deleting {r['name']}/{r['version']}, describing it "
        f"returned {o['status']}, expected 404")


@pytest.mark.parametrize("launch", LAUNCHES)
@pytest.mark.parametrize("i", CHART_IDS)
def test_head_says_absent_before_the_push(probed, launch, i):
    """``HEAD /api/charts/<nonce>`` is 404 before anything was pushed.

    The existence check is one of only two HEAD routes ChartMuseum declares, and it
    is what a helm client asks before publishing. Asked here about a name generated
    when this grading process started, so a 200 means the route is claiming
    something exists that cannot.
    """
    r = _chart(probed, launch, i)
    for key, what in (("head_before_push", r["name"]),
                      ("head_version_before_push",
                       f"{r['name']}/{r['version']}")):
        o = r[key]
        assert o["status"] == 404, (
            f"{launch}: HEAD on {what} returned {o['status']} before it was ever "
            f"pushed -- the existence check is reporting a chart that does not "
            f"exist")


@pytest.mark.parametrize("launch", LAUNCHES)
@pytest.mark.parametrize("i", CHART_IDS)
def test_head_says_present_after_the_push(probed, launch, i):
    """And 200 once it has been.

    The other half of the contract, and the half that breaks when the two HEAD
    routes are not carried over: a port that drops them 404s here, which makes every
    ``helm cm-push`` existence check report "not published" for charts that are.
    """
    r = _chart(probed, launch, i)
    if r["push"]["status"] != 201:
        pytest.skip("reported by test_push_accepted")
    for key, what in (("head_after_push", r["name"]),
                      ("head_version_after_push",
                       f"{r['name']}/{r['version']}")):
        o = r[key]
        assert o["status"] == 200, (
            f"{launch}: HEAD on {what} returned {o['status']} after a successful "
            f"push -- the chart is there and the existence check cannot see it")


@pytest.mark.parametrize("launch", LAUNCHES)
@pytest.mark.parametrize("i", CHART_IDS)
def test_head_says_absent_again_after_the_delete(probed, launch, i):
    """Back to 404 after the delete: the check tracks state, not a table."""
    r = _chart(probed, launch, i)
    if r["delete"]["status"] != 200:
        pytest.skip("reported by test_delete_accepted")
    o = r["head_version_after_delete"]
    assert o["status"] == 404, (
        f"{launch}: HEAD on {r['name']}/{r['version']} returned {o['status']} "
        f"after it was deleted -- the existence check is stale")


@pytest.mark.parametrize("launch", LAUNCHES)
@pytest.mark.parametrize("i", CHART_IDS)
def test_head_sends_no_body(probed, launch, i):
    """None of those four HEAD responses carried a body."""
    r = _chart(probed, launch, i)
    for key in ("head_before_push", "head_version_before_push",
                "head_after_push", "head_version_after_push",
                "head_version_after_delete"):
        o = r[key]
        assert o["body_len"] == 0, (
            f"{launch}: {key} for {r['name']} returned a {o['body_len']}-byte "
            f"body; HEAD must not send one")


@pytest.mark.parametrize("launch", LAUNCHES)
def test_storage_returned_to_its_seeded_shape(probed, launch):
    """Nothing the probes pushed is left behind.

    Stated as a whole-launch property rather than per chart: it is the statement
    that twelve pushes and twelve deletes net out, which is what a real store does
    and what a bookkeeping bug breaks.
    """
    life = battery_or_fail(probed, launch, "lifecycle")
    leftovers = set()
    for name, r in life["charts"].items():
        for fn in r["storage_after_delete"]:
            if name in fn:
                leftovers.add(fn)
    assert not leftovers, (
        f"{launch}: probe charts remain in storage after their deletes: "
        f"{sorted(leftovers)[:10]}")


# --- writes that must not land ----------------------------------------------
#
# Its own launch, and that is the point of it: these writes are aimed at
# resources that cannot exist, so the launch they run on must be one whose
# storage nobody else is reading. A write that is refused and persists anyway is
# invisible to every check that only reads the status.

def test_writes_to_nonexistent_resources_are_refused(probed):
    """Every write aimed at a resource that does not exist is refused.

    The targets embed the run nonce, so none of them can exist. A 2xx here means
    the router accepted a write to something it should never have matched.
    """
    b = battery_or_fail(probed, "ghost-writes", "write_discipline")
    bad = {k: v["status"] for k, v in b.items()
           if isinstance(v, dict) and "status" in v and 200 <= v["status"] < 300}
    assert not bad, (
        f"writes to nonexistent, nonce-named resources were accepted: {bad}")


def test_refused_writes_left_storage_untouched(probed):
    """And nothing was written while refusing them.

    The seeded charts are still exactly the four that were seeded, plus whatever
    index cache the server maintains. A write that 404s and persists anyway is the
    subtle version of this bug.
    """
    b = battery_or_fail(probed, "ghost-writes", "write_discipline")
    ghost = b["ghost"]
    residue = sorted(fn for fn in b["storage"] if ghost in fn)
    assert not residue, (
        f"refused writes to {ghost} nevertheless created: {residue}")


