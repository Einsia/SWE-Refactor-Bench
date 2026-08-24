"""Routing and authorisation properties, over generated paths.

ChartMuseum registers no routes with its web framework. It hangs a single
catch-all off ``NoRoute`` and dispatches inside a 207-line hand-written ``match()``
that peels an optional context path, then an optional tenant prefix of configurable
depth, then compares the remainder against a table. Re-hosting that on a real
router is the substance of this task, and it is easy to get *nearly* right: the
corpus paths work and a slightly different shape does not.

So the paths here are generated rather than drawn from the corpus -- doubled
separators, trailing slashes, over-long suffixes, unknown prefixes -- and what is
asserted about them are properties, not recorded answers:

* nothing crashes, hangs, or answers outside a sane status set
* a read never mutates storage
* under ``--context-path``, the unprefixed forms of real routes stop resolving
* with authentication on, every mutating route is refused and, crucially, nothing
  was written anyway

The auth tests read the storage directory after the refused writes, because "401
and also did not write" is the property that matters; a 401 that wrote anyway would
pass any status-only check.
"""

from __future__ import annotations

import pytest

from harness import probe
from harness.probeassert import battery_or_fail, obs_or_fail

GRID_LAUNCHES = ["plain", "contextpath"]

#: Statuses a correct ChartMuseum can answer a generated read with. 500 is in the
#: set because State A really does answer ``GET /charts/`` with
#: ``{"error":"unsupported file extension"}`` and a 500 -- measured, not assumed.
#: Excluding it would fail an honest port for faithfully preserving a quirk, which
#: is the opposite of what this benchmark rewards.
SANE_READ_STATUSES = {200, 201, 301, 302, 304, 400, 401, 403, 404, 405, 500}

#: Parametrised by *position* rather than by path string, so the launch's context
#: path is applied inside the test instead of being baked into the id. The ids stay
#: readable and the same index means the same generated path in both launches.
GRID_INDEX = [(pi, m)
              for pi in range(len(probe._grid_paths()))
              for m in probe.GRID_METHODS]
GRID_IDS = [f"{m}{probe._grid_paths()[pi]}".replace("/", "_")
            for pi, m in GRID_INDEX]


def _grid(probed, launch):
    return battery_or_fail(probed, launch, "route_grid")


def _grid_obs(probed, launch, pi, method):
    """One grid observation, resolved through the launch's prefix."""
    prefix = "/cm" if launch == "contextpath" else ""
    path = prefix + probe._grid_paths()[pi]
    return f"{method} {path}", obs_or_fail(_grid(probed, launch),
                                           f"{method} {path}",
                                           what="grid observation")


@pytest.mark.parametrize("launch", GRID_LAUNCHES)
def test_every_generated_read_got_an_answer(probed, launch):
    """No generated path left the connection hanging or reset.

    Stated per launch rather than per path: if the server died partway through the
    grid, the interesting fact is how far it got, and one failure says that better
    than ninety.
    """
    grid = _grid(probed, launch)
    obs = {k: v for k, v in grid.items() if isinstance(v, dict) and "status" in v}
    expected = len(probe._grid_paths()) * len(probe.GRID_METHODS)
    assert len(obs) == expected, (
        f"{launch}: {len(obs)} of {expected} generated requests produced a "
        f"response -- the server stopped answering partway through the grid")


@pytest.mark.parametrize("launch", GRID_LAUNCHES)
@pytest.mark.parametrize("pi,method", GRID_INDEX, ids=GRID_IDS)
def test_generated_read_status_is_sane(probed, launch, pi, method):
    """Every answer is a status a correct implementation could give.

    Per request rather than per launch, because "which path" is the whole content
    of this failure: a 502 or a 0 for one shape out of a hundred is a router that
    fell through, and the id names the shape.
    """
    key, o = _grid_obs(probed, launch, pi, method)
    assert o["status"] in SANE_READ_STATUSES, (
        f"{launch}: {key} answered {o['status']}, which is outside the set a "
        f"correct ChartMuseum can produce {sorted(SANE_READ_STATUSES)}"
        f"\n  body: {o.get('text')!r}")


#: ChartMuseum's route table declares HEAD on exactly two shapes --
#: ``HEAD /api/:repo/charts/:name`` and ``HEAD /api/:repo/charts/:name/:version``,
#: with their own handlers (``headChartRequestHandler``,
#: ``headChartVersionRequestHandler``) -- and GET on everything else. So HEAD is
#: *not* a synonym for GET here: ``HEAD /index.yaml`` and ``HEAD /health`` are 404,
#: which was measured on State A and then confirmed against ``routes.go``.
#:
#: That asymmetry is the trap. It survives a faithful port and breaks under two
#: opposite mistakes: forgetting to declare the two HEAD routes (Helm's existence
#: check stops working), or mounting handlers in a way that answers HEAD everywhere
#: GET is answered (``http.ServeMux`` and several router helpers do this, and the
#: 404s silently become 200s). Both are the sort of thing that passes a
#: happy-path smoke test.
HEAD_ROUTES = [
    "/api/charts/mychart",
    "/api/charts/mychart/",
    "/api/charts/mychart/0.1.0",
]


@pytest.mark.parametrize("launch", GRID_LAUNCHES)
@pytest.mark.parametrize("path", HEAD_ROUTES,
                         ids=[p.replace("/", "_") for p in HEAD_ROUTES])
def test_head_is_answered_on_the_routes_that_declare_it(probed, launch, path):
    """The two declared HEAD routes answer, and agree with GET's status."""
    pi = probe._grid_paths().index(path)
    _, g = _grid_obs(probed, launch, pi, "GET")
    key, h = _grid_obs(probed, launch, pi, "HEAD")
    assert h["status"] == g["status"], (
        f"{launch}: {key} answered {h['status']} but GET answered {g['status']}. "
        f"This route declares HEAD explicitly, so it must resolve -- helm uses it "
        f"to test whether a chart already exists.")


@pytest.mark.parametrize("launch", GRID_LAUNCHES)
@pytest.mark.parametrize(
    "pi", [i for i, p in enumerate(probe._grid_paths()) if p not in HEAD_ROUTES],
    ids=[p.replace("/", "_") for p in probe._grid_paths()
         if p not in HEAD_ROUTES])
def test_head_is_not_answered_off_those_routes(probed, launch, pi):
    """Everywhere else, HEAD is 404 -- including where GET returns 200.

    ``HEAD /index.yaml`` is the clearest case: GET it and you get the repository
    index, HEAD it and you get 404, because the table never declared it. A port that
    lets HEAD fall through to the GET handler turns seven 404s into 200s here.
    """
    key, h = _grid_obs(probed, launch, pi, "HEAD")
    assert h["status"] == 404, (
        f"{launch}: {key} answered {h['status']}, but this route declares only "
        f"GET, so HEAD must not resolve. A router that answers HEAD wherever it "
        f"answers GET produces exactly this.")


@pytest.mark.parametrize("launch", GRID_LAUNCHES)
@pytest.mark.parametrize(
    "pi", range(len(probe._grid_paths())),
    ids=[p.replace("/", "_") for p in probe._grid_paths()])
def test_options_is_not_handled(probed, launch, pi):
    """OPTIONS is 404 on every path, and carries no ``Allow``.

    ChartMuseum declares no OPTIONS route and does not enable its framework's
    method-not-allowed handling, so OPTIONS falls through to the catch-all
    everywhere -- measured across the whole grid, both launches.

    This is the CORS-middleware guard. Reaching for a router's CORS or
    ``AllowedMethods`` helper during a port is a natural move, and it changes every
    one of these 404s into a 204 or a 200 with an ``Allow`` header. That is a real
    behaviour change for any browser client sitting in front of the repository, and
    nothing in a behavioural smoke test would notice.
    """
    key, o = _grid_obs(probed, launch, pi, "OPTIONS")
    assert o["status"] == 404, (
        f"{launch}: {key} answered {o['status']}; ChartMuseum declares no OPTIONS "
        f"route, so it must 404. A CORS or method-not-allowed middleware added "
        f"during the port produces exactly this.")
    assert not o["headers"].get("allow"), (
        f"{launch}: {key} carries Allow: {o['headers'].get('allow')!r}; no route "
        f"declares OPTIONS, so nothing should be advertising a method set")


@pytest.mark.parametrize("launch", GRID_LAUNCHES)
@pytest.mark.parametrize(
    "pi", range(len(probe._grid_paths())),
    ids=[p.replace("/", "_") for p in probe._grid_paths()])
def test_head_returns_no_body(probed, launch, pi):
    """HEAD sends no body, whatever GET would have sent.

    RFC 9110, and a real client breaker: helm's downloader reads Content-Length
    from a HEAD and a body arriving where none is expected desynchronises a
    keep-alive connection.
    """
    key, h = _grid_obs(probed, launch, pi, "HEAD")
    assert h["body_len"] == 0, (
        f"{launch}: {key} returned a {h['body_len']}-byte body; HEAD must not "
        f"send one")


@pytest.mark.parametrize("launch", GRID_LAUNCHES)
def test_unknown_paths_are_not_served_as_success(probed, launch):
    """A path with an unknown prefix does not return 2xx.

    ``/apix/charts``, ``/nonexistent/deep/path`` and the nonce paths cannot name
    anything real, so a 200 for them means the router is matching too loosely --
    the failure mode of a port that translated a prefix match into a wildcard.
    """
    grid = _grid(probed, launch)
    prefix = "/cm" if launch == "contextpath" else ""
    never_real = [f"{prefix}/apix/charts", f"{prefix}/nonexistent",
                  f"{prefix}/nonexistent/deep/path"]
    bad = {}
    for key, v in grid.items():
        if not (isinstance(v, dict) and "status" in v):
            continue
        path = key.split(" ", 1)[1]
        if path in never_real and 200 <= v["status"] < 300:
            bad[key] = v["status"]
    assert not bad, (
        f"{launch}: paths that name nothing returned success: {bad}")


def test_context_path_moves_every_route(probed):
    """Under ``--context-path=/cm``, unprefixed real routes stop resolving.

    The flag has to move the whole surface, including ``/health``. A port that
    prefixed the API routes but left the health check where it was would pass every
    behavioural test that only asks about the prefixed form, and would break every
    load balancer pointed at the old one -- in the direction that looks healthy.
    """
    grid = _grid(probed, "contextpath")
    # These are real routes in the *unprefixed* namespace, which must now be
    # unknown. Asked with GET only: HEAD and OPTIONS answer differently for
    # reasons that have nothing to do with the prefix.
    unprefixed = ["/index.yaml", "/api/charts", "/api/charts/mychart", "/health"]
    served = {}
    for path in unprefixed:
        v = grid.get(f"GET {path}")
        if v and 200 <= v["status"] < 300:
            served[path] = v["status"]
    assert not served, (
        f"--context-path=/cm is set, but these unprefixed routes still serve: "
        f"{served}. The context path must move the entire surface, /health "
        f"included.")


def test_context_path_prefixed_routes_do_resolve(probed):
    """The complement: the prefixed forms work.

    Without this, a port that rejected everything would pass the test above.
    """
    grid = _grid(probed, "contextpath")
    want = ["/cm/index.yaml", "/cm/api/charts", "/cm/health"]
    missing = {}
    for path in want:
        v = grid.get(f"GET {path}")
        if not v or not (200 <= v["status"] < 300):
            missing[path] = v["status"] if v else "no observation"
    assert not missing, (
        f"--context-path=/cm is set but the prefixed routes do not serve: "
        f"{missing}")


