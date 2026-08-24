"""Fixtures a candidate test gets.  Loaded by name (``-p srbcandidate``).

A candidate is a black-box HTTP test.  Everything here hands it a way to ask a
running server a question and read the exact bytes back.  Nothing here tells it
which of the two trees it is talking to, and there is no fixture that could: what
arrives is a host, two port numbers per profile, and the profile names.

The sender is stage 2's ``harness.wire``, imported rather than reimplemented, and
the image build asserts it is byte-identical to the behavioural suite's copy.  That
matters beyond saving code: a finding here is only interesting if it is a
divergence stage 2's client would also have seen.  Two HTTP clients can disagree
about a response without either server being wrong -- one adds an Accept-Encoding
the case did not ask for, one canonicalises `/route%2Fnonsense` before it reaches
the socket, one folds repeated headers into a comma-joined string -- and a stage 3
built on its own client would report those as defects.

WHAT WILL NOT SURVIVE ADJUDICATION, stated here because this is the file a
candidate author reads.  A claim is upheld only if it reproduces three times, and
the harness alternates trees between reruns, so anything a server does not
reproduce against ITSELF is discarded before scope is considered:

    `Date`; `X-GH-Took` and the `info.took` field; a `<time>` element in GPX
    output; the log id in a 500 body; a healthcheck `timestamp`; the ordering of
    distinct header names; header name casing; the charset parameter on a
    Content-Type; the bytes of a protobuf tile.

Stage 2 masks those by measurement -- it asks every case twice per side and drops
whatever differs within one side's own pair -- and this stage gets the same
protection from `reruns = 3` with `require_deterministic`.  A candidate keyed on
one of them fails to reproduce and is rejected as a flake rather than counted.
`headers_map()` below is the one convenience provided against this class of
mistake: comparing maps rather than lists is right for the same reason stage 2
compares maps.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harness import wire  # noqa: E402

DATA = Path(os.environ.get("SRB_ADV_DATA", "/tests/verification/data"))

#: The two graph profiles a candidate may address, and what each one is.
#:
#: Two servers per tree, because the transit endpoints are registered only when
#: the configuration names a GTFS feed: under `base`, /route-pt, /isochrone-pt and
#: /pt-mvt do not exist as routes at all, and under `pt` the road network is a
#: different extract with different coordinates.  A test that asks the `base`
#: server for /route-pt is testing nothing.
PROFILES = {
    "base": "Andorra, three routing profiles (car, bike, foot) across all three "
            "back-ends -- CH on `car`, LM on `bike`, flexible on `foot`",
    "pt": "Beatty with two GTFS feeds: the transit endpoints, plus a road "
          "network small enough that walk legs matter",
}

# --- coordinates, fixed and inside the imported extracts ---------------------
#: Andorra la Vella and Encamp, ~7 km apart, both inside the `base` extract.
AD_A = "42.5063,1.5218"
AD_B = "42.5432,1.5906"
#: Between them, for a three-point route.
AD_C = "42.5300,1.5600"
#: Paris: inside no extract this stage imports, so a routing request naming it is
#: answered by the out-of-bounds path rather than by the router.
OFF_MAP = "48.8566,2.3522"
#: Two stops in the `pt` extract.
BEATTY_A = "36.914893,-116.76821"
BEATTY_B = "36.914944,-116.761472"
#: Inside sample-feed's service calendar.  Fixed rather than `now`: a relative
#: time makes the same request answer differently as the clock crosses a service
#: boundary, and a candidate built on one cannot reproduce.
PT_DEPARTURE = "2007-01-01T08:00:00Z"


def _servers() -> dict:
    raw = os.environ.get("SRB_SERVERS")
    if not raw:
        raise RuntimeError(
            "SRB_SERVERS is not set.  This test is being run outside the "
            "verification harness, which is the only thing that builds the trees "
            "and starts the servers it needs.")
    return json.loads(raw)


class Client:
    """One profile's server, addressed on either of its two ports.

    The application port and the admin port are separate connectors on the same
    process, and which surface lives on which is part of what was ported: State A
    serves /metrics, /healthcheck, /ping and /threads on the admin connector and
    everything else on the application connector.  A rewrite that put a monitoring
    endpoint on the wrong port has changed the contract, so both are reachable
    here and `port="admin"` selects the second.
    """

    def __init__(self, profile: str, host: str, app_port: int, admin_port: int):
        self.profile = profile
        self.host = host
        self.app_port = app_port
        self.admin_port = admin_port

    def send(self, method: str, target: str, *, headers: dict | None = None,
             body: bytes | None = None, port: str = "app",
             timeout: float = 60.0) -> wire.Response:
        """One request.  `target` is used VERBATIM as the request-target.

        No normalisation, no percent-encoding, no collapsing of slashes: what is
        passed here is what goes on the wire.  That is the point of a raw socket
        rather than an HTTP library -- on this task the difference between
        `/route%2Fnonsense` and `/route/nonsense` is graded behaviour, and a
        client that tidied either would hide it.

        The timeout is generous because it is not the interesting number: a route
        request against a cold LM back-end can take seconds, and a candidate that
        failed on a slow first call would fail to reproduce and be discarded.
        """
        p = self.app_port if port == "app" else self.admin_port
        return wire.Transport(self.host, p, timeout=timeout).send(
            method, target, headers or {}, body)

    def get(self, target: str, **kw) -> wire.Response:
        return self.send("GET", target, **kw)

    def head(self, target: str, **kw) -> wire.Response:
        return self.send("HEAD", target, **kw)

    def post(self, target: str, **kw) -> wire.Response:
        return self.send("POST", target, **kw)

    def options(self, target: str, **kw) -> wire.Response:
        return self.send("OPTIONS", target, **kw)

    def post_json(self, target: str, payload, **kw) -> wire.Response:
        """POST a JSON document, with the header the resource requires.

        /route accepts both GET and POST and they are two different bindings —
        one reads query parameters, the other deserialises a request body — so a
        rewrite can port one and lose the other.
        """
        headers = {"Content-Type": "application/json"}
        headers.update(kw.pop("headers", None) or {})
        return self.send("POST", target,
                         headers=headers,
                         body=json.dumps(payload).encode(), **kw)

    def admin(self, target: str, **kw) -> wire.Response:
        return self.send("GET", target, port="admin", **kw)

    def __repr__(self) -> str:
        return (f"<Client {self.profile} {self.host}:{self.app_port}"
                f" admin:{self.admin_port}>")


def headers_map(resp: wire.Response) -> dict[str, list[str]]:
    """A response's headers as lowercase name -> list of values, order dropped.

    The shape stage 2 compares, and for the same reason: Jetty and Tomcat do not
    agree on the order of distinct header names and the order is not the contract.
    Repeats are PRESERVED as a list, because two `Access-Control-Allow-Methods`
    values and one comma-joined value are genuinely different responses.
    """
    out: dict[str, list[str]] = {}
    for name, value in resp.headers:
        out.setdefault(name.lower(), []).append(value)
    return out


def media_type(resp: wire.Response) -> str:
    """Content-Type with parameters stripped, lowercased.

    `application/json` and `application/json;charset=UTF-8` are the same answer
    from two embedded containers; stage 2 compares media types without the
    charset, so a claim resting on the parameter is outside this stage's scope.
    """
    for name, value in resp.headers:
        if name.lower() == "content-type":
            return value.split(";")[0].strip().lower()
    return ""


def as_json(resp: wire.Response):
    """The body parsed as JSON, or a failure that says what arrived instead."""
    try:
        return json.loads(resp.body)
    except ValueError as exc:
        raise AssertionError(
            f"expected JSON, got HTTP {resp.status} "
            f"{media_type(resp) or '(no content-type)'}: "
            f"{resp.body[:200]!r} ({exc})") from exc


@pytest.fixture(scope="session")
def servers() -> dict[str, Client]:
    """Both profiles, keyed by name: ``servers["pt"].get("/route-pt?...")``.

    Both are already running when the test starts.  They are also the SAME
    processes the previous candidate talked to, which is safe here because
    GraphHopper serves no endpoint that mutates anything: the graph is imported
    once at startup and read from thereafter.  Nothing needs cleaning up, and
    nothing a candidate does can change what the next one sees -- except killing
    a server, which the harness detects and replaces.
    """
    return {name: Client(name, info["host"], int(info["app_port"]),
                         int(info["admin_port"]))
            for name, info in _servers().items()}


@pytest.fixture(scope="session")
def server(servers) -> Client:
    """The `base` profile: Andorra, three routing profiles, no transit."""
    return servers["base"]


@pytest.fixture(scope="session")
def pt(servers) -> Client:
    """The `pt` profile: Beatty plus two GTFS feeds, transit endpoints live."""
    return servers["pt"]


@pytest.fixture(scope="session")
def gpx_body() -> bytes:
    """The GPX track /match is fed, byte-identical to the one stage 2 posts.

    A recorded track inside the Andorra extract.  Provided rather than generated
    because map matching is sensitive to where the points are, and a track built
    in a test would answer differently on both sides for a reason that is the
    track's fault.
    """
    return (DATA / "bodies" / "match-andorra.gpx").read_bytes()
