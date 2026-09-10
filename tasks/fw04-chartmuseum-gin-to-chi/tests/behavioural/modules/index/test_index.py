"""``index.yaml``: the one response Helm itself parses, checked structurally.

This is the most load-bearing surface in the project. Every ``helm repo update``
in the world reads this document, so a port may reformat it freely but may not
change what it says. Three separate properties are asserted, because they fail
independently and for different reasons:

*structure* -- which charts and versions the index lists. A port that lost the
multi-tenancy depth logic serves a syntactically perfect index for the wrong
tenant, and only the entry set shows it.

*digests* -- the sha256 recorded per chart version. Helm verifies downloads
against these; a wrong digest turns every install into a checksum failure. They
come from the frozen fixture tree, so they are constants.

*order* -- the sequence of versions within each entry. Helm resolves a bare chart
name to the *first* entry it finds, so reordering silently changes which version
users get. Go map iteration is randomised, which means a port that rebuilt this
document by ranging over a map would produce a different order run to run --
exactly the bug this catches, and one no digest check would.

``generated`` is masked before comparison: it is a live timestamp with second
precision, and comparing it would fail on the clock rather than on the code.

Every test carries ``srb_skip_ok``: the only skip in this file is
``_skip_if_volatile``, which fires when State A's own two capture runs disagreed
about the index or its order. That is a fact about the recording, so scoring it as
a miss would charge a submission for the oracle's instability. See
``../corpus/test_body.py`` for the argument in full.
"""

from __future__ import annotations

import pytest

import reference as ref


def _skip_if_volatile(pair, field: str) -> None:
    if pair.field_is_volatile(field) or pair.body_is_volatile:
        pytest.skip(f"{field} was not stable across two runs of State A")


@pytest.mark.srb_skip_ok
@pytest.mark.parametrize("key", ref.INDEX_KEYS)
def test_index_parses(pair, key):
    """The response is still a parseable index document."""
    _skip_if_volatile(pair, "index")
    got = pair.actual.get("index")
    assert got is not None, (
        f"{pair.describe()}: the body did not parse as an index document\n"
        f"  got {pair.actual['body'][:400]!r}")


@pytest.mark.srb_skip_ok
@pytest.mark.parametrize("key", ref.INDEX_KEYS)
def test_index_api_version(pair, key):
    """``apiVersion`` is unchanged.

    Helm switches parsing behaviour on this field, so it is a compatibility
    boundary rather than a label.
    """
    _skip_if_volatile(pair, "index")
    want = (pair.expected["index"] or {}).get("apiVersion")
    got = (pair.actual.get("index") or {}).get("apiVersion")
    assert got == want, (
        f"{pair.describe()}: index apiVersion is {got!r}, expected {want!r}")


@pytest.mark.srb_skip_ok
@pytest.mark.parametrize("key", ref.INDEX_KEYS)
def test_index_entry_names(pair, key):
    """Exactly the same chart names are listed."""
    _skip_if_volatile(pair, "index")
    want = sorted((pair.expected["index"] or {}).get("entries", {}))
    got = sorted((pair.actual.get("index") or {}).get("entries", {}))
    assert got == want, (
        f"{pair.describe()}: index lists different charts -- "
        f"missing {sorted(set(want) - set(got))}, "
        f"unexpected {sorted(set(got) - set(want))}")


@pytest.mark.srb_skip_ok
@pytest.mark.parametrize("key", ref.INDEX_KEYS)
def test_index_version_sets(pair, key):
    """Each chart lists the same set of versions."""
    _skip_if_volatile(pair, "index_order")
    want = {k: sorted(v) for k, v in pair.expected["index_order"].items()}
    got_raw = pair.actual.get("index_order") or {}
    got = {k: sorted(v) for k, v in got_raw.items()}
    assert got == want, (
        f"{pair.describe()}: index version sets differ\n"
        f"  expected {want!r}\n  got      {got!r}")


@pytest.mark.srb_skip_ok
@pytest.mark.parametrize("key", ref.INDEX_KEYS)
def test_index_version_order(pair, key):
    """Versions appear in the same order within each entry.

    The sharpest test in this file. See the module docstring: Helm resolves a bare
    chart name to the first listed version, and Go randomises map iteration, so a
    port that reassembled entries from a map passes every other index test here
    and fails this one intermittently -- which is precisely why it is asserted
    rather than sorted away.
    """
    _skip_if_volatile(pair, "index_order")
    want = pair.expected["index_order"]
    got = pair.actual.get("index_order")
    assert got == want, (
        f"{pair.describe()}: version ordering differs\n"
        f"  expected {want!r}\n  got      {got!r}")


@pytest.mark.srb_skip_ok
@pytest.mark.parametrize("key", ref.INDEX_KEYS)
def test_index_digests(pair, key):
    """Every listed chart version carries the same sha256.

    These are the digests Helm verifies downloads against, and they are constants
    of the frozen fixture tree, so any difference is a real defect rather than an
    environmental one.
    """
    _skip_if_volatile(pair, "index_digests")
    want = pair.expected["index_digests"]
    got = pair.actual.get("index_digests")
    assert got is not None, f"{pair.describe()}: no digests parsed from the index"
    missing = {k: v for k, v in want.items() if k not in got}
    wrong = {k: (got[k], v) for k, v in want.items()
             if k in got and got[k] != v}
    extra = sorted(set(got) - set(want))
    assert not missing and not wrong and not extra, (
        f"{pair.describe()}: index digests differ\n"
        f"  missing {sorted(missing)}\n"
        f"  wrong (got, expected) {wrong}\n"
        f"  unexpected {extra}")


@pytest.mark.srb_skip_ok
@pytest.mark.parametrize("key", ref.INDEX_KEYS)
def test_index_urls_present(pair, key):
    """Each entry keeps a download URL.

    Checked for presence and shape rather than exact value: the URL embeds the
    bound host and port, which the harness assigns per launch, so normalize has
    already reduced the authority to a placeholder. What must hold is that a URL
    is still there -- an index whose entries have no ``urls`` is unusable to Helm
    even though it parses.
    """
    _skip_if_volatile(pair, "index")
    want_entries = (pair.expected["index"] or {}).get("entries", {})
    got_entries = (pair.actual.get("index") or {}).get("entries", {})
    bad = []
    for name, versions in want_entries.items():
        got_versions = got_entries.get(name) or []
        for i, wv in enumerate(versions):
            if not isinstance(wv, dict) or not wv.get("urls"):
                continue
            gv = got_versions[i] if i < len(got_versions) else None
            if not isinstance(gv, dict) or not gv.get("urls"):
                bad.append(f"{name}[{i}]")
    assert not bad, (
        f"{pair.describe()}: entries lost their download urls: {bad[:10]}")
