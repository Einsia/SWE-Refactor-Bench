"""Response bodies, at the strength each case was recorded with.

``body_mode`` is not a hint, it is the contract. It was chosen per case at
construction time because the six kinds of body in this API admit different
amounts of pinning:

``exact``    byte-for-byte, after masking moving instants. 298 cases.
``json``     the parsed document must be equal; whitespace and key order free. 241.
``index``    a repository index: compared structurally, plus digests and order. 120.
``len``      a chart archive: identity is its sha256 and length, not its text. 32.
``metrics``  Prometheus exposition: every family's HELP and TYPE and every
             series' label signature, one test each; sample values are masked. 6.
``ignore``   the body genuinely varies in State A; only status and headers hold. 31.

Asserting bytes where the mode says ``json`` would fail an honest port for
re-serialising a map in a different key order, which Go makes no promise about.
Asserting only JSON where the mode says ``exact`` would let a port drop a trailing
newline that clients parse. So each mode gets its own test and the modes are read
off the golden rather than decided here.

Which cases each test is collected over is decided at collection, in
``lib/reference.py``, and that is the only mechanism this file uses to avoid asking
a question State A has no answer to.  ``BODY_KEYS`` drops the 31 ``ignore`` cases
from the three tests that run across all modes, ``JSON_OBJECT_KEYS`` drops the 13
documents that are a list or a scalar from the key-set test, and the stable gauge
test is not defined at all while no gauge was recorded as stable.

Filtering at collection rather than skipping in the body, because a skip is charged
0: an id no submission can ever answer would sit in ``corpus``'s denominator
forever, costing everyone the same check.  The size of that is measurable on this
recording -- 112 of ``corpus``'s 9492 checks are questions State A itself does not
answer, on a corpus that is a transcript of State A's own answers.  Filtering
deletes the check instead of exempting it, which is what makes the module's size
the number of questions it can actually ask.

``srb_skip_ok`` stays on the tests, and it buys no exemption: it decides only that a
skip keeps the suite's own wording instead of being rewritten to ``fail`` by
``pytest_module``.  What it covers here is the volatility guards:
``body_is_volatile`` and ``json_is_volatile`` are the two capture runs of State A
disagreeing with each other, never anything a submission did.  They are dead on
this recording --- its ``volatile`` map is empty, and none of the 520 graded cells
ever reached one --- and they are left in the body rather than lifted into a key
list because they describe a *re*-capture: if a later one does mark a field
unstable, the same reasoning as above applies to it, and the filter belongs beside
the others in ``reference.py`` at that point.

What a submission *can* reach --- a server that did not launch, a case that got no
response, a key the recording does not carry --- all raise ``pytest.fail`` inside
the ``pair`` fixture, so the escape route that would matter is closed before a test
body runs.
"""

from __future__ import annotations

import pytest

import reference as ref

BY = ref.KEYS_BY_MODE


def _skip_if_volatile(pair):
    if pair.body_is_volatile:
        pytest.skip("the body was not stable across two runs of State A")


@pytest.mark.srb_skip_ok
@pytest.mark.parametrize("key", ref.BODY_KEYS)
def test_body_sha256_matches(pair, key):
    """The body digest matches, for every case whose body is a contract.

    Runs across all modes including ``len`` and ``index``, because the digest is
    computed over the *comparable* form: masked text for a text body, raw bytes
    for a binary one. ``ignore`` cases are not collected -- their body is recorded
    but was never a requirement, so there is no answer to compare against.
    """
    _skip_if_volatile(pair)
    assert pair.actual["body_sha256"] == pair.expected["body_sha256"], (
        f"{pair.describe()}: body digest differs\n"
        f"  expected sha256 {pair.expected['body_sha256']} "
        f"({pair.expected['body_len']} bytes)\n"
        f"  got      sha256 {pair.actual['body_sha256']} "
        f"({pair.actual['body_len']} bytes)")


@pytest.mark.srb_skip_ok
@pytest.mark.parametrize("key", ref.BODY_KEYS)
def test_body_length_matches(pair, key):
    """The comparable body length matches."""
    _skip_if_volatile(pair)
    assert pair.actual["body_len"] == pair.expected["body_len"], (
        f"{pair.describe()}: body is {pair.actual['body_len']} bytes, "
        f"expected {pair.expected['body_len']}")


@pytest.mark.srb_skip_ok
@pytest.mark.parametrize("key", BY.get("exact", []))
def test_body_exact(pair, key):
    """Byte-for-byte equality, after the same masking State A's record got."""
    _skip_if_volatile(pair)
    want = pair.expected["body"]
    got = pair.actual["body"]
    assert got == want, (
        f"{pair.describe()}: body differs\n"
        f"  expected {want[:400]!r}\n  got      {got[:400]!r}")


@pytest.mark.srb_skip_ok
@pytest.mark.parametrize("key", BY.get("json", []))
def test_body_json_equal(pair, key):
    """The parsed document is equal; serialisation details are free."""
    _skip_if_volatile(pair)
    if pair.json_is_volatile():
        pytest.skip("the parsed body was not stable across two runs of State A")
    want = pair.expected["json"]
    got = pair.actual["json"]
    if want is None:
        # State A answered this one with something that is not JSON at all, and
        # that is a contract in its own direction. Measured: at ``--depth`` 1 and
        # above, ``GET /info`` peels ``info`` off as the repository name and serves
        # the HTML welcome page. A port that "helpfully" returned a JSON error
        # there would be a behaviour change, so it has to fail rather than skip.
        assert got is None, (
            f"{pair.describe()}: the port returned JSON, but State A returned "
            f"a non-JSON body\n  got {got!r}")
        return
    assert got is not None, (
        f"{pair.describe()}: body did not parse as JSON, but State A's did\n"
        f"  got {pair.actual['body'][:400]!r}")
    assert got == want, (
        f"{pair.describe()}: JSON differs\n  expected {want!r}\n  got      {got!r}")


@pytest.mark.srb_skip_ok
@pytest.mark.parametrize("key", BY.get("len", []))
def test_binary_body_identity(pair, key):
    """A chart archive is returned byte-identically.

    Asserted through the digest and the length rather than the bytes, and split
    out from ``test_body_sha256_matches`` so that a corrupted download reads as
    'the archive changed' rather than as a generic digest mismatch. ``body_binary``
    is checked too: a port that started returning a base64 or JSON-wrapped archive
    would otherwise only fail on length.
    """
    _skip_if_volatile(pair)
    assert pair.actual["body_binary"] == pair.expected["body_binary"], (
        f"{pair.describe()}: body_binary is {pair.actual['body_binary']}, "
        f"expected {pair.expected['body_binary']} -- the archive was returned in "
        f"a different form, not as raw bytes")
    assert (pair.actual["body_sha256"], pair.actual["body_len"]) == \
           (pair.expected["body_sha256"], pair.expected["body_len"]), (
        f"{pair.describe()}: archive bytes differ -- got sha256 "
        f"{pair.actual['body_sha256']} ({pair.actual['body_len']} bytes), "
        f"expected {pair.expected['body_sha256']} "
        f"({pair.expected['body_len']} bytes)")


@pytest.mark.srb_skip_ok
@pytest.mark.parametrize("key", ref.BODY_KEYS)
def test_body_binary_flag_matches(pair, key):
    """Text stayed text and bytes stayed bytes.

    Cheap, and it catches a whole class of content-negotiation regression: a port
    that starts sending a JSON error page where State A sent an archive, or one
    whose archive response is suddenly decodable as UTF-8 text.
    """
    _skip_if_volatile(pair)
    assert pair.actual["body_binary"] == pair.expected["body_binary"], (
        f"{pair.describe()}: body_binary is {pair.actual['body_binary']}, "
        f"expected {pair.expected['body_binary']}")


@pytest.mark.srb_skip_ok
@pytest.mark.parametrize("key", ref.JSON_OBJECT_KEYS)
def test_json_error_shape(pair, key):
    """A JSON body that is an object keeps the same key set.

    Separate from equality because it localises the failure: an error whose
    ``{"error": ...}`` envelope became ``{"message": ...}`` is a client-visible
    break, and this says so in one line instead of printing two documents.

    Collected over the object-valued cases only. The other 13 recorded a list or a
    bare scalar, where a key set is not a thing to compare; they are not exempted
    here, they are not asked. ``test_body_json_equal`` still grades those, and it
    is the stronger statement anyway -- full equality rather than the key set.
    """
    _skip_if_volatile(pair)
    if pair.json_is_volatile():
        pytest.skip("the parsed body was not stable in State A")
    want, got = pair.expected["json"], pair.actual["json"]
    assert isinstance(got, dict), (
        f"{pair.describe()}: expected a JSON object, got {type(got).__name__}")
    assert sorted(got) == sorted(want), (
        f"{pair.describe()}: JSON keys differ -- "
        f"missing {sorted(set(want) - set(got))}, "
        f"unexpected {sorted(set(got) - set(want))}")


@pytest.mark.srb_skip_ok
@pytest.mark.parametrize("key", BY.get("metrics", []))
def test_metrics_series_names(pair, key):
    """The Prometheus surface keeps every series name it had.

    ``zsais/go-gin-prometheus`` is on the retired list, so the port has to re-emit
    this whole surface itself, and the name set is the contract for anyone
    scraping it: a renamed family silently breaks every dashboard and alert
    written against this repository.

    The comparison is the *series* names -- ``..._count``/``..._sum`` included --
    rather than the family names, because that is what a scrape actually returns.

    Was a tautology until it was measured: it compared ``sorted(got)`` against
    ``sorted(want)`` where both were the five-key wrapper dict
    (``help``/``type``/``series``/``names``/``malformed``), so it asserted
    ``['help', ...] == ['help', ...]`` and passed on any exposition at all,
    including an empty one.
    """
    _skip_if_volatile(pair)
    want = pair.expected["metrics"]["names"]
    got = (pair.actual.get("metrics") or {}).get("names")
    assert got is not None, (
        f"{pair.describe()}: no Prometheus exposition was parsed from the body")
    assert sorted(got) == sorted(want), (
        f"{pair.describe()}: metric names differ -- "
        f"missing {sorted(set(want) - set(got))}, "
        f"unexpected {sorted(set(got) - set(want))}")


@pytest.mark.srb_skip_ok
@pytest.mark.parametrize("key", BY.get("metrics", []))
def test_metrics_declared_families(pair, key):
    """The set of families carrying a ``# HELP``/``# TYPE`` block is unchanged.

    Separate from the series names because the two can diverge in one direction
    that matters: an exporter can emit the right samples with no declarations at
    all, and Prometheus will scrape it while every UI that reads HELP shows a
    blank description.
    """
    _skip_if_volatile(pair)
    want = pair.expected["metrics"]
    got = pair.actual.get("metrics") or {}
    for table in ("help", "type"):
        w, g = want[table], got.get(table)
        assert g is not None, (
            f"{pair.describe()}: no {table} table was parsed from the exposition")
        assert sorted(g) == sorted(w), (
            f"{pair.describe()}: families declaring # {table.upper()} differ -- "
            f"missing {sorted(set(w) - set(g))}, "
            f"unexpected {sorted(set(g) - set(w))}")


@pytest.mark.srb_skip_ok
@pytest.mark.parametrize("key", BY.get("metrics", []))
def test_metrics_has_no_malformed_lines(pair, key):
    """No line of the exposition is one Prometheus would drop."""
    _skip_if_volatile(pair)
    got = pair.actual.get("metrics") or {}
    assert got.get("malformed") == pair.expected["metrics"]["malformed"], (
        f"{pair.describe()}: the exposition contains lines Prometheus cannot "
        f"parse: {(got.get('malformed') or [])[:5]}")


@pytest.mark.srb_skip_ok
@pytest.mark.parametrize("key,family", ref.METRICS_FAMILY_PAIRS)
def test_metrics_family_help(pair, key, family):
    """One test per family: the ``# HELP`` text is byte-identical.

    Help strings come from the collector definitions, so this is where a
    re-implementation that re-worded a description -- or dropped the trailing
    full stop that ``go-gin-prometheus`` writes and the runtime collectors do
    not -- is caught, per family rather than in aggregate.
    """
    _skip_if_volatile(pair)
    want = pair.expected["metrics"]["help"].get(family)
    got = (pair.actual.get("metrics") or {}).get("help", {}).get(family)
    assert got is not None, (
        f"{pair.describe()}: family {family} declares no # HELP line")
    assert got == want, (
        f"{pair.describe()}: # HELP for {family} differs\n"
        f"  expected {want!r}\n  got      {got!r}")


@pytest.mark.srb_skip_ok
@pytest.mark.parametrize("key,family", ref.METRICS_FAMILY_PAIRS)
def test_metrics_family_type(pair, key, family):
    """One test per family: the ``# TYPE`` is the same kind of metric.

    A summary re-implemented as a gauge still scrapes, and then every
    ``rate()`` and quantile written against it is wrong.
    """
    _skip_if_volatile(pair)
    want = pair.expected["metrics"]["type"].get(family)
    got = (pair.actual.get("metrics") or {}).get("type", {}).get(family)
    assert got is not None, (
        f"{pair.describe()}: family {family} declares no # TYPE line")
    assert got == want, (
        f"{pair.describe()}: {family} is a {got}, expected a {want}")


@pytest.mark.srb_skip_ok
@pytest.mark.parametrize("key,name", ref.METRICS_SERIES_PAIRS)
def test_metrics_series_labels(pair, key, name):
    """One test per series: its label signatures are exactly the recorded ones.

    Values are masked -- a duration sum moves between runs -- but label sets are
    not, and they are the part with a fixed cardinality contract:
    ``chartmuseum_requests_total`` carries the ``code``, ``method``, ``url`` and
    ``handler`` labels, where ``url`` is a route template rather than a requested
    path and ``handler`` is the Go symbol ``instruction.md`` pins by name. Both
    are graded here as the byte strings they are on the wire.
    """
    _skip_if_volatile(pair)
    want = pair.expected["metrics"]["series"][name]
    got = (pair.actual.get("metrics") or {}).get("series", {}).get(name)
    assert got is not None, (
        f"{pair.describe()}: series {name} is not exported")
    assert sorted(got) == sorted(want), (
        f"{pair.describe()}: label signatures for {name} differ\n"
        f"  missing    {sorted(set(want) - set(got))}\n"
        f"  unexpected {sorted(set(got) - set(want))}")


# Defined only over the cases that recorded a stable gauge, which today is none of
# the six. ``STABLE_GAUGE_HINTS`` matches on ``chart_total``/``chart_version_total``
# while the names on the wire are ``chartmuseum_charts_served_total`` and
# ``chartmuseum_chart_versions_served_total``, so no sample was ever selected and
# this compared ``{} == {}`` -- it passed on an exposition with no gauges at all.
#
# Those two gauges are not ungraded. The audit suite checks them as exact
# arithmetic over storage (``test_chart_gauges_count_what_is_in_storage``), computed
# from pushes made during the run, which is a stronger statement than replaying a
# recorded number. So nothing is lost by not collecting here.
#
# The guard, not an empty ``parametrize``: pytest turns an empty argvalues list into
# one collected item carrying a skip, and a charged skip on a question the recording
# does not ask is exactly what this is avoiding. Written this way, the day
# ``normalize.stable_gauge_values`` keeps a sample, it grades with no further edit.
if ref.METRICS_VALUE_KEYS:
    @pytest.mark.srb_skip_ok
    @pytest.mark.parametrize("key", ref.METRICS_VALUE_KEYS)
    def test_metrics_stable_values(pair, key):
        """The gauges whose value is a count of frozen inputs still read the same.

        Expectation-driven: only the samples ``normalize.stable_gauge_values`` kept
        in the golden are compared, so this grades what was actually measured to be
        stable rather than a list asserted here.
        """
        _skip_if_volatile(pair)
        want = pair.expected["metrics_values"]
        got = pair.actual.get("metrics_values")
        assert got is not None, f"{pair.describe()}: no stable gauge values recorded"
        for gauge, value in sorted(want.items()):
            assert got.get(gauge) == value, (
                f"{pair.describe()}: {gauge} reads {got.get(gauge)!r}, "
                f"expected {value!r}")
