"""Responses held to their own claims, and to each other.

No oracle is involved in this file, by design: the verifier image does not ship
State A's binary, so nothing here can be a differential comparison. What it can be
is an *internal* consistency check, and those turn out to be strong. The index
advertises, for every chart version, a download URL and a sha256. Follow the URL,
hash the bytes, and the response has either agreed with itself or contradicted
itself -- and a canned-response table has to contradict itself, because keeping two
endpoints in agreement about a digest is exactly the work it was trying to avoid.

Determinism is checked the same way. Four identical requests must produce four
identical responses. ChartMuseum assembles ``index.yaml`` from a map, and Go
randomises map iteration order, so a port that rebuilt the document by ranging
over one produces a different byte string every time. That defect passes the
behavioural suite whenever the capture happened to agree, and it fails here every
run.
"""

from __future__ import annotations

import pytest

from harness import normalize, probe
from harness.probeassert import battery_or_fail

SELF_LAUNCHES = ["plain", "contextpath"]
DET_LAUNCHES = ["plain", "contextpath"]


def _downloads(probed, launch):
    return battery_or_fail(probed, launch, "index_self")["downloads"]


@pytest.mark.parametrize("launch", SELF_LAUNCHES)
def test_index_advertises_at_least_the_seeded_charts(probed, launch):
    """The index lists every chart the launch was seeded with.

    A floor rather than an equality: the seeds are known, so this cannot be
    satisfied by an empty index, and it does not forbid a correct port from
    listing something the seeds imply.
    """
    d = _downloads(probed, launch)
    idx = battery_or_fail(probed, launch, "index_self")["index"]
    assert idx["status"] == 200, (
        f"{launch}: GET index.yaml returned {idx['status']}")
    assert len(d) >= 4, (
        f"{launch}: the index advertises {len(d)} downloadable chart versions "
        f"({sorted(d)}), but the launch was seeded with 4")


@pytest.mark.parametrize("launch", SELF_LAUNCHES)
def test_every_advertised_url_is_fetchable(probed, launch):
    """Each URL the index advertises really serves a chart.

    Resolved against the index's own location per RFC 3986, exactly as Helm does,
    which matters under ``--context-path``: ChartMuseum advertises a relative
    ``charts/x.tgz`` there, so resolving against the root instead would look for
    ``/charts/x.tgz`` and miss the prefix.
    """
    d = _downloads(probed, launch)
    bad = {k: (v["status"], v["requested_path"]) for k, v in d.items()
           if v["status"] != 200}
    assert not bad, (
        f"{launch}: the index advertises URLs that do not serve: {bad}")


@pytest.mark.parametrize("launch", SELF_LAUNCHES)
def test_every_advertised_digest_matches_the_served_bytes(probed, launch):
    """The index's digest is the sha256 of what the download actually returns.

    Helm verifies downloads against this value, so a mismatch is not cosmetic: it
    turns every ``helm install`` into a checksum failure.
    """
    d = _downloads(probed, launch)
    bad = {}
    for k, v in d.items():
        if v["status"] != 200:
            continue
        if v["body_sha256"] != v["advertised_digest"]:
            bad[k] = (v["advertised_digest"], v["body_sha256"])
    assert not bad, (
        f"{launch}: index digest disagrees with the served bytes for "
        f"{sorted(bad)} -- (advertised, actual): {bad}")


@pytest.mark.parametrize("launch", SELF_LAUNCHES)
def test_download_content_length_matches_body(probed, launch):
    """``Content-Length`` agrees with the number of bytes delivered.

    A framework swap changes who writes this header. A wrong value truncates the
    download for any client that trusts it, which is all of them.
    """
    d = _downloads(probed, launch)
    bad = {}
    for k, v in d.items():
        if v["status"] != 200:
            continue
        cl = v["headers"].get("content-length")
        if not cl:
            bad[k] = "absent"
        elif cl[0] != str(v["body_len"]):
            bad[k] = f"{cl[0]} != {v['body_len']}"
    assert not bad, (
        f"{launch}: Content-Length disagrees with the body for {bad}")


# --- determinism ------------------------------------------------------------

def _det(probed, launch):
    return battery_or_fail(probed, launch, "determinism")


#: Parametrised by position in :data:`harness.probe.DETERMINISM_PATHS` so the
#: launch's own prefix can be applied inside the test. Reconstructing the key from
#: a formatted string outside would have to re-derive the prefix, which is how the
#: two views drift apart.
DET_INDEX = list(range(len(probe.DETERMINISM_PATHS)))
DET_IDS = [f"{m}{p}".replace("/", "_") for m, p in probe.DETERMINISM_PATHS]


def _det_runs(probed, launch, i):
    method, path = probe.DETERMINISM_PATHS[i]
    prefix = "/cm" if launch == "contextpath" else ""
    key = f"{method} {prefix}{path}"
    det = _det(probed, launch)
    runs = det.get(key)
    if runs is None:
        pytest.fail(f"{launch}: no determinism observations for {key!r} "
                    f"(have {sorted(det)[:6]})")
    return key, runs


@pytest.mark.parametrize("launch", DET_LAUNCHES)
@pytest.mark.parametrize("i", DET_INDEX, ids=DET_IDS)
def test_repeated_request_gives_identical_status(probed, launch, i):
    """Four identical requests, four identical statuses."""
    key, runs = _det_runs(probed, launch, i)
    seen = {r["status"] for r in runs}
    assert len(seen) == 1, (
        f"{launch}: {key} returned different statuses across "
        f"{len(runs)} identical requests: {sorted(seen)}")


@pytest.mark.parametrize("launch", DET_LAUNCHES)
@pytest.mark.parametrize("i", DET_INDEX, ids=DET_IDS)
def test_repeated_request_gives_identical_body(probed, launch, i):
    """Four identical requests, one body.

    ``index.yaml`` carries a ``generated`` timestamp with second precision, so it
    is masked before comparison -- otherwise this would fail on the clock whenever
    four requests straddled a second boundary. Everything else, including the order
    of entries, is compared literally. That is the point: entry order is where Go's
    randomised map iteration shows up.
    """
    key, runs = _det_runs(probed, launch, i)
    forms = set()
    for r in runs:
        text = r.get("text")
        forms.add(r["body_sha256"] if text is None
                  else normalize.mask_timestamps(text))
    assert len(forms) == 1, (
        f"{launch}: {key} returned {len(forms)} different bodies across "
        f"{len(runs)} identical requests -- a response that changes when nothing "
        f"did. This is what an index assembled by ranging over a Go map looks "
        f"like.")


@pytest.mark.parametrize("launch", DET_LAUNCHES)
@pytest.mark.parametrize("i", DET_INDEX, ids=DET_IDS)
def test_repeated_request_gives_identical_header_set(probed, launch, i):
    key, runs = _det_runs(probed, launch, i)
    # x-request-id is per-request by design, so the comparison is over names.
    sets = {tuple(sorted(r["headers"])) for r in runs}
    assert len(sets) == 1, (
        f"{launch}: {key} sent different header sets across identical "
        f"requests: {sets}")


@pytest.mark.parametrize("launch", DET_LAUNCHES)
def test_request_id_is_fresh_per_request(probed, launch):
    """``X-Request-Id`` differs between two identical requests.

    The complement of the tests above: everything else must be stable, and this
    must not be. A port that hard-coded the value recorded in the golden file --
    the obvious way to make the behavioural header test pass -- fails here.
    """
    det = _det(probed, launch)
    offenders = {}
    for key, runs in det.items():
        ids = [r["headers"].get("x-request-id", [None])[0] for r in runs]
        present = [i for i in ids if i]
        if len(present) < 2:
            continue
        if len(set(present)) == 1:
            offenders[key] = present[0]
    assert not offenders, (
        f"{launch}: X-Request-Id was identical across separate requests for "
        f"{sorted(offenders)} (value {list(offenders.values())[:1]}) -- it must be "
        f"generated per request, not fixed")
