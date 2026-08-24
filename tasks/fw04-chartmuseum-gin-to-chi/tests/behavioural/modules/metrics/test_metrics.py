"""The Prometheus exposition, as arithmetic over storage and traffic.

``zsais/go-gin-prometheus`` is on the retired list. In State A it is one
``engine.Use()`` line that registers a collector, mounts ``/metrics``, and -- the
part that gets dropped -- installs a mapping function that rewrites each request's
URL into the *route template* before it becomes a label value.

Without that mapping the exposition still parses, still serves, and still exports
every family, so nothing about its shape looks wrong. What happens instead is that
every chart name anyone requests becomes its own time series, which is a cardinality
leak rather than a visible defect. This module detects it by pushing charts named
after the run nonce and requiring that none of those names appear anywhere in the
exposition.

The rest is arithmetic that a canned exposition cannot satisfy: the chart gauges
have to count what is actually in storage after the module has pushed to it, and
the request counters have to have moved by the traffic the module itself generated,
labelled by the status and method it generated them with.

Scraped twice, before and after that traffic, because a gauge that is right once
may be a constant.
"""

from __future__ import annotations

import pytest

from harness import normalize, probe
from harness.probeassert import battery_or_fail, obs_or_fail


def _metrics(probed):
    return battery_or_fail(probed, "metrics", "metrics")


def _parsed(probed, which):
    m = _metrics(probed)
    o = obs_or_fail(m, which, what="metrics scrape")
    if o["status"] != 200:
        pytest.fail(f"GET /metrics returned {o['status']} with --enable-metrics; "
                    f"the exposition endpoint must be served\n"
                    f"  body: {o.get('text')!r}")
    return normalize.parse_metrics(o["text_full"])


@pytest.mark.parametrize("which", ["before", "after"])
def test_metrics_endpoint_is_served(probed, which):
    """``--enable-metrics`` registers ``/metrics``, and it answers 200."""
    o = obs_or_fail(_metrics(probed), which, what="metrics scrape")
    assert o["status"] == 200, (
        f"GET /metrics ({which} the traffic) returned {o['status']}\n"
        f"  body: {o.get('text')!r}")


@pytest.mark.parametrize("which", ["before", "after"])
def test_exposition_parses_without_malformed_lines(probed, which):
    """Every line is either a comment or a valid sample.

    A hand-rolled exporter that forgets to escape a label value, or emits a bare
    metric name with no value, produces lines Prometheus drops silently. Here they
    are counted.
    """
    parsed = _parsed(probed, which)
    assert not parsed["malformed"], (
        f"the {which} exposition contains lines Prometheus cannot parse: "
        f"{parsed['malformed'][:5]}")


@pytest.mark.parametrize("which", ["before", "after"])
def test_every_series_declares_help_and_type(probed, which):
    """Each metric family has a ``# HELP`` and a ``# TYPE``.

    Dropped when series are written by hand instead of through a registry, and
    their absence is what makes a metric unusable in a dashboard's autocomplete.
    """
    parsed = _parsed(probed, which)
    missing_help = [n for n in parsed["names"] if not _family_declared(parsed["help"], n)]
    missing_type = [n for n in parsed["names"] if not _family_declared(parsed["type"], n)]
    assert not missing_help, (
        f"the {which} exposition has series with no # HELP line: "
        f"{missing_help[:8]}")
    assert not missing_type, (
        f"the {which} exposition has series with no # TYPE line: "
        f"{missing_type[:8]}")


def _family_declared(table: dict, series_name: str) -> bool:
    """Whether ``series_name`` is covered by a HELP/TYPE declaration.

    Histogram and summary families declare one HELP/TYPE for the family and then
    emit ``_bucket``/``_count``/``_sum`` series under it, so an exact lookup is not
    enough.
    """
    if series_name in table:
        return True
    for suffix in ("_bucket", "_count", "_sum", "_total"):
        if series_name.endswith(suffix) and series_name[: -len(suffix)] in table:
            return True
    return False


def _url_labels(parsed: dict) -> set[str]:
    """Every distinct ``url`` label value in an exposition.

    Shared by the two checks that read the label set from opposite directions: one
    requires route templates to be present, the other requires the exposition's own
    path to be absent.
    """
    urls = set()
    for keys in parsed["series"].values():
        for key in keys:
            for part in key.strip("{}").split(","):
                if part.startswith("url="):
                    urls.add(part[len("url="):].strip('"'))
    return urls


def test_no_nonce_name_appears_in_the_exposition(probed):
    """The url label is a route template, not the path that was requested.

    ChartMuseum installs ``mapURLWithParamsBackToRouteTemplate`` so
    ``/api/charts/<name>/<version>`` becomes ``/api/:repo/charts/:name/:version``
    before it is used as a label. Drop it and the label set is unbounded: this
    battery alone requested twelve distinct chart paths, and each would become a
    permanent time series.

    The names were generated when this process started, so a leak is unambiguous.
    """
    m = _metrics(probed)
    text = m["after"]["text_full"]
    pushed = [p["name"] for p in m["pushed"]]
    leaks = [ln for ln in text.splitlines()
             if any(name in ln for name in pushed) or probe.RUN_NONCE in ln]
    assert not leaks, (
        f"chart names pushed during this run appear in /metrics, which means the "
        f"url label is the raw request path rather than the route template -- "
        f"unbounded cardinality:\n  " + "\n  ".join(ln[:150] for ln in leaks[:6]))


def test_url_labels_are_route_templates(probed):
    """Positively: every url label still contains a ``:`` parameter or is static.

    The complement of the leak test. A port could suppress the label entirely and
    pass the test above; this one requires the templates to still be there.
    """
    urls = _url_labels(_parsed(probed, "after"))
    assert urls, (
        "no series carries a url label; the request counter must be labelled by "
        "route, as State A's exporter is")
    parameterised = [u for u in urls if ":" in u]
    assert parameterised, (
        f"no url label looks like a route template (none contains ':'): "
        f"{sorted(urls)}. The label mapping function collapses chart paths back "
        f"to their route shape.")


#: The exposition's own path.  A literal rather than a probe constant because it is
#: what ``--enable-metrics`` publishes and what ``test_metrics_endpoint_is_served``
#: already scrapes; if it moved, this module would not be scraping at all.
EXPOSITION_PATH = "/metrics"


def test_exposition_does_not_count_itself(probed):
    """``/metrics`` must not appear among its own url labels.

    ``go-gin-prometheus`` returns before recording when the request path is the
    endpoint it mounted, so a scrape does not register as traffic. A port that
    instruments every route uniformly -- the obvious way to write it, and what a
    middleware chain in front of a mux does by default -- counts each scrape, and
    then every request-rate series in this repository grows with scrape frequency
    instead of with load.

    Nothing about such an exposition looks wrong: it parses, every family is
    present, the gauges are right, and the counters are higher than before. That
    last part is why ``test_request_counters_moved_with_the_traffic`` cannot see
    this -- it asserts a lower bound, and inflation satisfies a lower bound more
    comfortably than correctness does.

    Read from the second scrape. A recording middleware acts after the handler
    returns, so the first body predates its own increment and shows nothing.

    Calibrated against State A, whose recorded exposition carries six distinct url
    labels across 47 families -- ``/health``, ``/:repo``, ``/:repo/index.yaml``,
    ``/:repo/charts/:filename``, ``/api/:repo/charts``,
    ``/api/:repo/charts/:name/:version`` -- and no ``/metrics``. The reference
    cannot fail this by construction.
    """
    urls = _url_labels(_parsed(probed, "after"))
    if not urls:
        pytest.skip("no url labels in the exposition; "
                    "test_url_labels_are_route_templates owns that case")
    assert EXPOSITION_PATH not in urls, (
        f"the exposition counts its own scrapes: a series is labelled "
        f"url=\"{EXPOSITION_PATH}\", which State A's exporter never emits -- it "
        f"returns early for that path, and its recorded exposition carries six url "
        f"labels, none of them the endpoint itself.\n"
        f"  url labels seen: {sorted(urls)}\n"
        f"Consequence: every request-rate series becomes a function of how often "
        f"the endpoint is scraped rather than of traffic, so changing the scrape "
        f"interval silently rescales every dashboard and alert written against it.")


def test_chart_gauges_count_what_is_in_storage(probed):
    """``chartmuseum_charts_served_total`` is arithmetic, and it is checkable.

    The launch is seeded with 2 charts across 4 versions, and this battery pushes
    ``N_METRICS_PUSH`` more, each at one version. So the gauges must read
    ``2 + N`` and ``4 + N``. Nothing recorded: the count depends on pushes made
    during this run.
    """
    parsed = _parsed(probed, "after")
    m = _metrics(probed)
    pushed = sum(1 for p in m["pushed"] if p["status"] == 201)
    if pushed != probe.N_METRICS_PUSH:
        pytest.fail(f"only {pushed} of {probe.N_METRICS_PUSH} metrics-battery "
                    f"pushes were accepted, so the gauge arithmetic cannot be "
                    f"checked: {m['pushed']}")

    charts = _gauge(parsed, "chartmuseum_charts_served_total")
    versions = _gauge(parsed, "chartmuseum_chart_versions_served_total")
    assert charts == 2 + pushed, (
        f"chartmuseum_charts_served_total reads {charts}; the launch is seeded "
        f"with 2 charts and {pushed} more were pushed, so it must read "
        f"{2 + pushed}")
    assert versions == 4 + pushed, (
        f"chartmuseum_chart_versions_served_total reads {versions}; the launch is "
        f"seeded with 4 versions and {pushed} more were pushed, so it must read "
        f"{4 + pushed}")


def _gauge(parsed: dict, name: str) -> float:
    """The single sample of a gauge family, or a failure explaining what was seen."""
    matches = {k: v for k, v in parsed["values"].items() if k.startswith(name)}
    if not matches:
        pytest.fail(f"{name} is not exported at all (families present: "
                    f"{[n for n in parsed['names'] if 'chartmuseum' in n]})")
    if len(matches) > 1:
        pytest.fail(f"{name} has {len(matches)} series where one is expected: "
                    f"{sorted(matches)}")
    raw = next(iter(matches.values()))
    try:
        return float(raw)
    except ValueError:
        pytest.fail(f"{name} has a non-numeric sample value {raw!r}")


def test_request_counters_moved_with_the_traffic(probed):
    """The request counter is higher after the traffic than before.

    Deliberately an inequality rather than an exact count: the two scrapes are
    themselves requests, and so is the readiness probe, so an exact figure would
    encode the harness rather than the server. What must hold is that a counter
    counts -- a hard-coded exposition body does not move at all.
    """
    before = _parsed(probed, "before")
    after = _parsed(probed, "after")
    made = _metrics(probed)["requests_made"]

    name = "chartmuseum_request_duration_seconds_count"
    b = _gauge(before, name)
    a = _gauge(after, name)
    assert a >= b + made, (
        f"{name} went from {b} to {a} while {made} requests were made in "
        f"between, so it moved by {a - b}. A counter that lags the traffic is "
        f"either not wired to every route or not a counter.")


def test_request_totals_are_labelled_by_status_and_method(probed):
    """``chartmuseum_requests_total`` distinguishes what happened.

    The battery generates 201s, 200s and 404s deliberately. All three must appear as
    distinct series: an exporter installed on only the success path -- or one that
    labels everything ``200`` -- collapses them, and every error-rate alert written
    against this repository stops firing.
    """
    parsed = _parsed(probed, "after")
    keys = parsed["series"].get("chartmuseum_requests_total")
    if not keys:
        pytest.fail("chartmuseum_requests_total is not exported; the request "
                    f"counter is the exposition's primary series (families: "
                    f"{[n for n in parsed['names'] if 'chartmuseum' in n]})")
    codes = set()
    methods = set()
    for key in keys:
        for part in key.strip("{}").split(","):
            if part.startswith("code="):
                codes.add(part[len("code="):].strip('"'))
            if part.startswith("method="):
                methods.add(part[len("method="):].strip('"'))
    assert {"200", "201", "404"} <= codes, (
        f"chartmuseum_requests_total carries codes {sorted(codes)}; this run "
        f"produced 200s, 201s and 404s, and each must be its own series")
    assert {"GET", "POST"} <= methods, (
        f"chartmuseum_requests_total carries methods {sorted(methods)}; this run "
        f"made both GETs and POSTs")


#: The six families ChartMuseum registers itself, with the type each is declared
#: as. Named here rather than read off a recording because these are the surface
#: of the *retired* module: ``zsais/go-gin-prometheus`` is what emitted them, and a
#: port that drops it has to re-create exactly these. All six are stated in
#: ``instruction.md``, so nothing here is a trap.
#:
#: Written because the gap was measured: before this, the only families any test
#: named were ``chartmuseum_requests_total`` and
#: ``chartmuseum_request_duration_seconds_count``, both incidentally, inside tests
#: about label sets and counter movement. ``chartmuseum_request_size_bytes`` and
#: ``chartmuseum_response_size_bytes`` appeared nowhere in the suite at all, so
#: renaming or deleting them cost nothing on this side.
CHARTMUSEUM_FAMILIES = {
    "chartmuseum_requests_total": "counter",
    "chartmuseum_request_duration_seconds": "summary",
    "chartmuseum_request_size_bytes": "summary",
    "chartmuseum_response_size_bytes": "summary",
    "chartmuseum_charts_served_total": "gauge",
    "chartmuseum_chart_versions_served_total": "gauge",
}

#: The handler label value on the wire in State A. An internal Go symbol: the
#: method value of ``rootHandler`` on ``*Router`` in
#: ``helm.sh/chartmuseum/pkg/chartmuseum/router``, as ``runtime.FuncForPC`` renders
#: it. Disclosed verbatim in ``instruction.md`` precisely because it is
#: unguessable, and graded as a byte string either way -- derived through
#: ``runtime.FuncForPC`` from a real method, or emitted as this literal.
ROOT_HANDLER_LABEL = (
    "helm.sh/chartmuseum/pkg/chartmuseum/router.(*Router).rootHandler-fm")


@pytest.mark.parametrize("family", sorted(CHARTMUSEUM_FAMILIES))
def test_chartmuseum_family_is_exported(probed, family):
    """Each of ChartMuseum's own six families is exported, and as its own type.

    One test per family so that losing one costs one failure and losing all six
    costs six: an exporter rebuilt by hand tends to keep the counter everyone
    looks at and quietly drop the request and response size summaries, which is
    exactly what no other test in this suite would have noticed.
    """
    parsed = _parsed(probed, "after")
    names = set(parsed["names"])
    declared = parsed["type"]
    covered = family in names or any(
        n.startswith(family + suffix) for n in names
        for suffix in ("_count", "_sum", "_bucket"))
    assert covered, (
        f"{family} is not exported; ChartMuseum registers six families of its "
        f"own and this is one of them. Exported chartmuseum families: "
        f"{sorted(n for n in names if n.startswith('chartmuseum_'))}")
    want = CHARTMUSEUM_FAMILIES[family]
    assert declared.get(family) == want, (
        f"{family} is declared as {declared.get(family)!r}, expected {want!r}. "
        f"A summary re-implemented as a gauge still scrapes, and then every "
        f"quantile and rate() written against it is wrong.")


@pytest.mark.parametrize("family", sorted(
    f for f, t in CHARTMUSEUM_FAMILIES.items() if t == "summary"))
def test_summary_families_emit_count_and_sum(probed, family):
    """A summary is a ``_count`` and a ``_sum``, not a bare number.

    The shape a client library gives you for free and a hand-rolled exporter
    forgets: without ``_count`` there is no request rate, and without ``_sum``
    there is no average. Declaring ``# TYPE ... summary`` while emitting one plain
    sample passes every structural check and is still unusable.
    """
    parsed = _parsed(probed, "after")
    names = set(parsed["names"])
    missing = [s for s in ("_count", "_sum") if family + s not in names]
    assert not missing, (
        f"{family} is declared a summary but emits no {missing} series; "
        f"present under that name: "
        f"{sorted(n for n in names if n.startswith(family))}")


def test_request_counter_carries_the_root_handler_symbol(probed):
    """The ``handler`` label is still the ``rootHandler`` method value.

    ``instruction.md`` pins this string, so it is graded here rather than left to
    a body digest. It is not decoration: it is the evidence that the counter is
    installed on the one handler every request goes through, which is how State A
    is built -- ``engine.NoRoute(router.rootHandler)``, no registered routes. A
    port that instead labels each chi route with its own symbol produces a
    different label set, and every recorded query against
    ``handler="...rootHandler-fm"`` returns nothing.
    """
    parsed = _parsed(probed, "after")
    keys = parsed["series"].get("chartmuseum_requests_total")
    if not keys:
        pytest.fail("chartmuseum_requests_total is not exported at all, so its "
                    "handler label cannot be checked")
    handlers = set()
    for key in keys:
        for part in key.strip("{}").split(","):
            if part.startswith("handler="):
                handlers.add(part[len("handler="):].strip('"'))
    assert handlers, (
        f"no chartmuseum_requests_total series carries a handler label; State A "
        f"labels every one of them with {ROOT_HANDLER_LABEL!r}. Signatures seen: "
        f"{sorted(keys)[:4]}")
    assert handlers == {ROOT_HANDLER_LABEL}, (
        f"chartmuseum_requests_total carries handler labels {sorted(handlers)}; "
        f"State A carries exactly {[ROOT_HANDLER_LABEL]}")


def test_go_runtime_collectors_are_registered(probed):
    """The default Go and process collectors are still there.

    A hand-rolled exporter that builds its own registry instead of using the
    default one exports the ChartMuseum series and silently drops
    ``go_goroutines``, ``go_memstats_*`` and ``process_*``. Those are what every
    "is this process healthy" panel is built on.
    """
    parsed = _parsed(probed, "after")
    names = set(parsed["names"])
    required = ["go_goroutines", "go_memstats_alloc_bytes", "process_cpu_seconds_total"]
    missing = [n for n in required if n not in names]
    assert not missing, (
        f"the exposition is missing the default runtime collectors {missing}; "
        f"{len(names)} families are exported, so the registry is there but "
        f"incomplete")
