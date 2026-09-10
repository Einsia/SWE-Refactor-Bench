"""The case corpus: what gets asked of both servers, and under which config.

This file defines REQUESTS ONLY.  There is not one hand-written status code,
header or body in it, and that is structural rather than stylistic: stage 2 runs
State A and the submission side by side in the same container and compares their
answers to each other.  A hand-written expectation would be a second, worse
implementation of GraphHopper, and when it disagreed with the real one it would
be the expectation that was wrong — failing correct submissions for a reason no
submission controls.

So nothing here knows what a route looks like.  Each case says only: with the
server configured like *this*, ask *that*, and the two sides must answer alike.

Cases carry a `suite`, which selects the graded module, and a `why`.  The `why`
is printed when a case fails, so a failure report says which contract broke
rather than only which byte differed.  Write it for someone who has never read
this file.

`volatile` is not declared per case.  Volatility is measured: every case is
asked twice on each side, and any JSON field that differs between a side's own
two answers is masked on both before comparison.  Timings and request ids drop
out that way without anyone maintaining a list of them, and — more to the point
— without a list that is quietly wrong about a field that only sometimes moves.
See harness/normalize.py.
"""
from __future__ import annotations

import hashlib
import json

# --- profiles ----------------------------------------------------------------
# A profile is a config file plus the OSM/GTFS input it imports.  Each one costs
# a full graph import on BOTH sides, so a profile only exists if it moves an axis
# of observable behaviour that no cheaper profile reaches.
#
# Two of them, for one reason each:
#
#   base  Andorra, three profiles (car/foot/bike), CH on car, LM on bike.  The
#         three routing back-ends are not interchangeable: CH refuses a
#         `custom_model` in the query and answers 400, LM accepts it, and
#         flexible accepts more still.  A single-profile config would grade one
#         dispatch path and silently skip the two whose error behaviour differs.
#
#   pt    beatty.osm plus reader-gtfs' sample-feed.  The PT stack is bound
#         through a different mechanism than the rest — three competing
#         PtRouter bindings in State A's HK2 binder, one of which wins by
#         configuration — so it is the part of the wiring most likely to be
#         dropped or mis-ported, and none of it is reachable from `base`.
#
# Deliberately NOT profiles:
#
#   elevation  Needs a network fetch or a baked SRTM tile.  Stage 2 runs with no
#              network, and a 2 GB tile in the image would buy one more numeric
#              field on the same code path.
#
#   turn_costs A longer CH preparation on the same dispatch surface.  It changes
#              the numbers both sides compute identically and reaches no branch
#              `base` misses.
PROFILES = {
    "base": {
        "config": "config-base.yml",
        "why": "three routing profiles across all three back-ends (CH, LM, "
               "flexible), which is what makes the 400-vs-200 dispatch on "
               "`custom_model` observable",
    },
    "pt": {
        "config": "config-pt.yml",
        "why": "the public-transit stack, bound through a different mechanism "
               "from everything in `base` and unreachable without a GTFS feed",
    },
}


def case(cid, suite, method, target, why, *, profile="base", headers=None,
         body=None, body_mode="json", port="app"):
    """One request, asked of both sides.

    body_mode chooses how the response body is compared:
      json   parse both, mask measured-volatile fields, compare structurally.
      exact  compare bytes.  For bodies with no volatility at all: a CSV
             header row, a translation map, `pong\\n`.
      ignore compare status and headers only.  For bodies that are legitimately
             unstable in ways the twice-asked measurement cannot see — a
             protobuf tile whose float packing is platform-dependent, a metrics
             dump that moves with the JVM.  Used sparingly, and every use says
             why in its own `why`.

    port chooses which connector: "app" (8989 upstream) or "admin" (8990).
    """
    assert body_mode in ("json", "exact", "ignore"), body_mode
    assert port in ("app", "admin"), port
    return {
        "id": cid, "suite": suite, "profile": profile, "method": method,
        "target": target, "headers": headers or {}, "body": body,
        "body_mode": body_mode, "port": port, "why": why,
    }


# Two points inside the Andorra extract, and a third outside it.  Coordinates
# are `lat,lon` in GraphHopper's `point` parameter.
AD_A = "42.5063,1.5218"     # Andorra la Vella
AD_B = "42.5432,1.5906"     # Encamp, ~7 km north-east
AD_C = "42.5300,1.5600"     # between the two, for a three-point route
OFF_MAP = "48.8566,2.3522"  # Paris: in no extract this suite imports


def _q(*pairs):
    """Query string from (key, value) pairs, order preserved.

    Order is preserved deliberately: `point` is a repeated parameter and the
    order of repeats decides the waypoint order, so a dict would silently
    reorder a route.  Values are passed through unencoded — every value in this
    corpus is already URL-safe, and encoding them here would hide the one case
    that tests how the server handles a raw `%2F`.
    """
    return "&".join(f"{k}={v}" for k, v in pairs)


def _routing_cases():
    """/route: the endpoint the whole application exists to serve.

    Weighted heaviest in suite.toml because it is where a rewrite's mistakes
    land, and because it exercises the deepest parameter binding in the tree:
    repeated `point`, list-valued `details`, enums, booleans, a nested JSON
    body, and three content types off one method.
    """
    C = []
    a = C.append

    # --- the plain path, one per back-end -----------------------------------
    # Same question, three profiles, three different internals.  If a rewrite
    # binds `profile` to the wrong thing, exactly one of these survives.
    #
    # The back-end has to be selected explicitly, because the graded config
    # prepares CH for `car` and LM for `bike` and nothing else.  GraphHopper
    # cascades CH -> LM -> flexible, so `profile=bike` with no flag is a 400
    # naming the missing CH preparation, and `foot` needs `lm.disable` on top
    # because no LM was prepared for it either.  Asked without the flags all
    # three of these reached the same unreachable question, and the differential
    # then compared two identical error documents and passed -- see the note on
    # Pair.check in srbfixtures.py about what a green dot is worth.
    for prof, back, flags in (
            ("car", "CH", ()),
            ("bike", "LM", (("ch.disable", "true"),)),
            ("foot", "flexible", (("ch.disable", "true"),
                                  ("lm.disable", "true"))),
    ):
        a(case(f"route-plain-{prof}", "routing", "GET",
               "/route?" + _q(("point", AD_A), ("point", AD_B),
                              ("profile", prof), *flags),
               f"a two-point {prof} route, which upstream answers through the "
               f"{back} back-end; the three back-ends differ in what they "
               f"accept and in what they compute -- distance, weight and the "
               f"visited-node count all move between them -- so all three are "
               f"asked"))

    # --- repeated and multi-valued parameters -------------------------------
    a(case("route-three-points", "routing", "GET",
           "/route?" + _q(("point", AD_A), ("point", AD_C), ("point", AD_B),
                          ("profile", "car")),
           "three `point` repeats: the via-point must land in the middle, so a "
           "binding that collapses repeats to the first or last value answers "
           "a different route rather than an error"))
    a(case("route-details-multi", "routing", "GET",
           "/route?" + _q(("point", AD_A), ("point", AD_B), ("profile", "car"),
                          ("details", "road_class"), ("details", "surface"),
                          ("details", "max_speed")),
           "three `details` repeats: each adds a keyed array to the response, "
           "so a binding that keeps one drops two whole sections of the body"))
    a(case("route-details-single", "routing", "GET",
           "/route?" + _q(("point", AD_A), ("point", AD_B), ("profile", "car"),
                          ("details", "road_class")),
           "one `details` value where the parameter is list-typed — the case "
           "that separates 'binds a list' from 'binds a scalar and happens to "
           "work when there is one'"))

    # --- booleans and enums, including the falsey defaults -------------------
    a(case("route-no-instructions", "routing", "GET",
           "/route?" + _q(("point", AD_A), ("point", AD_B), ("profile", "car"),
                          ("instructions", "false")),
           "`instructions=false` removes a top-level array; a boolean bound as "
           "a string is truthy and silently keeps it"))
    a(case("route-no-points", "routing", "GET",
           "/route?" + _q(("point", AD_A), ("point", AD_B), ("profile", "car"),
                          ("calc_points", "false")),
           "`calc_points=false` changes the geometry to a stub, which is a "
           "different shape rather than a different number"))
    a(case("route-points-encoded-off", "routing", "GET",
           "/route?" + _q(("point", AD_A), ("point", AD_B), ("profile", "car"),
                          ("points_encoded", "false")),
           "`points_encoded=false` switches geometry from a polyline string to "
           "a GeoJSON coordinate array — same data, different type, and a "
           "common thing to lose when a serializer is re-registered"))
    a(case("route-elevation-off", "routing", "GET",
           "/route?" + _q(("point", AD_A), ("point", AD_B), ("profile", "car"),
                          ("elevation", "false")),
           "`elevation=false` is the default; asking for it explicitly proves "
           "an explicitly-passed default is bound, not just an absent one"))
    a(case("route-locale-de", "routing", "GET",
           "/route?" + _q(("point", AD_A), ("point", AD_B), ("profile", "car"),
                          ("locale", "de"), ("instructions", "true")),
           "`locale=de` translates instruction text through the translation "
           "map, so it also proves the i18n resources are on the classpath of "
           "whatever the submission built"))
    a(case("route-algorithm-alt", "routing", "GET",
           "/route?" + _q(("point", AD_A), ("point", AD_B), ("profile", "car"),
                          ("algorithm", "alternative_route")),
           "`algorithm=alternative_route` returns several paths instead of one, "
           "changing the response's cardinality"))

    # --- content negotiation off one method ---------------------------------
    # RouteResource declares three @Produces on the GET.  Which one answers is
    # decided by a query parameter upstream, not by Accept, and that is exactly
    # the kind of detail a port re-implements from memory and gets wrong.
    # Both GPX cases pin `gpx.millis`.  Without it the endpoint uses
    # System.currentTimeMillis() for the departure instant, which lands in the
    # <metadata><time> and in every <trkpt><time> as start + travel time — so the
    # body is not byte-stable against ITSELF and exact mode cannot grade it.
    # Pinning it rather than masking the timestamps is the better fix twice over:
    # the harness gains no new declared mask, and the times become GRADED, since
    # each one is then a fixed function of a bound parameter and the route's own
    # computed travel time.  A submission that ignores the parameter and stamps
    # its own clock fails, which is correct — upstream honours it.
    GPX_MILLIS = ("gpx.millis", "1700000000000")     # 2023-11-14T22:13:20Z
    a(case("route-gpx", "routing", "GET",
           "/route?" + _q(("point", AD_A), ("point", AD_B), ("profile", "car"),
                          ("type", "gpx"), GPX_MILLIS),
           "`type=gpx` answers XML from the same method that answers JSON; the "
           "media type and the body language both have to switch",
           body_mode="exact"))
    a(case("route-gpx-nowaypoints", "routing", "GET",
           "/route?" + _q(("point", AD_A), ("point", AD_B), ("profile", "car"),
                          ("type", "gpx"), ("gpx.include_waypoints", "false"),
                          GPX_MILLIS),
           "a dotted query parameter name (`gpx.include_waypoints`): legal in "
           "HTTP, and a trap for a binder that maps parameters onto fields",
           body_mode="exact"))

    # --- POST: the same endpoint reached through a JSON body ----------------
    a(case("route-post-basic", "routing", "POST",
           "/route",
           "the POST form of the same endpoint: a JSON body instead of query "
           "parameters, which is a second and completely separate binding path",
           headers={"Content-Type": "application/json"},
           body=json.dumps({"points": [[1.5218, 42.5063], [1.5906, 42.5432]],
                            "profile": "car"}).encode()))
    a(case("route-post-custom-model-lm", "routing", "POST",
           "/route",
           "a `custom_model` in the POST body against an LM profile, where it "
           "is accepted: nested object binding, and the answer differs from the "
           "same route without the model",
           headers={"Content-Type": "application/json"},
           body=json.dumps({
               "points": [[1.5218, 42.5063], [1.5906, 42.5432]],
               "profile": "bike",
               "ch.disable": True,
               "custom_model": {"speed": [
                   {"if": "road_class == PRIMARY", "multiply_by": "0.5"}]},
           }).encode()))
    # `heading` is refused outright in speed mode -- upstream answers 400 and
    # names issue #483 -- so both back-end flags belong in the body, or the case
    # asks about array-of-scalar binding and gets an error document instead.
    # `ch.disable` alone is not enough: it falls through to LM, which the graded
    # config prepared for `bike` and not for `car`.
    a(case("route-post-headings", "routing", "POST",
           "/route",
           "`headings` and `snap_preventions` are arrays of scalars in the body "
           "and change which edge each point snaps to",
           headers={"Content-Type": "application/json"},
           body=json.dumps({
               "points": [[1.5218, 42.5063], [1.5906, 42.5432]],
               "profile": "car", "headings": [0, 180],
               "snap_preventions": ["motorway", "ferry"],
               "ch.disable": True, "lm.disable": True,
           }).encode()))
    return C


def _endpoint_cases():
    """The other twelve resources.

    Every one of them is a separate registration in State A's bundle, and the
    cheapest way for a rewrite to look finished while being incomplete is to
    port /route and leave the rest unregistered.  A 404 here is the signature of
    exactly that, and it is worth as much weight as several routing parameters.
    """
    C = []
    a = C.append

    a(case("info", "endpoints", "GET", "/info",
           "/info reports the configured profiles and the graph's bounding box: "
           "the endpoint a client calls to discover what the server can do, so "
           "its content depends on config binding as much as on dispatch"))
    a(case("health", "endpoints", "GET", "/health",
           "/health on the APPLICATION port — distinct from the admin port's "
           "/healthcheck, and easy to conflate when a framework brings its own "
           "health endpoint with an opinion about where it lives"))
    a(case("nearest", "endpoints", "GET",
           "/nearest?" + _q(("point", AD_A)),
           "/nearest snaps a coordinate to the graph; the only endpoint whose "
           "sole input is one `point`"))
    a(case("nearest-offmap", "endpoints", "GET",
           "/nearest?" + _q(("point", OFF_MAP)),
           "/nearest for a coordinate outside the extract: the not-found path "
           "of a lookup, which upstream reports as a body rather than a 404"))
    a(case("i18n-all", "endpoints", "GET", "/i18n",
           "/i18n with no locale returns the whole translation map, which is "
           "only present if the submission's build kept core's resource files",
           body_mode="exact"))
    a(case("i18n-de", "endpoints", "GET", "/i18n/de",
           "/i18n/{locale} is the one sub-path template in the tree; a rewrite "
           "that registers only the collection form answers 404 here",
           body_mode="exact"))
    a(case("i18n-unknown-locale", "endpoints", "GET", "/i18n/zz",
           "an unknown locale: upstream falls back rather than failing, and a "
           "port that validates the path variable turns that into an error",
           body_mode="exact"))

    # --- isochrone and SPT: same graph, different output shapes -------------
    a(case("isochrone-json", "endpoints", "GET",
           "/isochrone?" + _q(("point", AD_A), ("profile", "car"),
                              ("time_limit", "300")),
           "/isochrone returns polygons rather than a path — a different "
           "serializer and a different response root"))
    a(case("isochrone-buckets", "endpoints", "GET",
           "/isochrone?" + _q(("point", AD_A), ("profile", "car"),
                              ("time_limit", "600"), ("buckets", "3")),
           "`buckets=3` multiplies the polygon count, so a dropped integer "
           "parameter changes cardinality instead of raising"))
    a(case("isochrone-distance-limit", "endpoints", "GET",
           "/isochrone?" + _q(("point", AD_A), ("profile", "car"),
                              ("distance_limit", "2000")),
           "`distance_limit` and `time_limit` are mutually exclusive inputs to "
           "the same endpoint; this is the branch the other case does not take"))
    a(case("spt-csv", "endpoints", "GET",
           "/spt?" + _q(("point", AD_A), ("profile", "car"),
                        ("time_limit", "180")),
           "/spt answers text/csv by default from a method that also declares "
           "JSON — content negotiation with a non-JSON default, which is the "
           "opposite of every other endpoint here",
           body_mode="exact"))
    a(case("spt-columns", "endpoints", "GET",
           "/spt?" + _q(("point", AD_A), ("profile", "car"),
                        ("time_limit", "180"),
                        ("columns", "prev_node_id,edge_id,distance")),
           "a comma-separated `columns` list decides the CSV header and column "
           "order, so it grades an ordered projection rather than a set",
           body_mode="exact"))

    # --- the binary tile endpoints -------------------------------------------
    # 14/8261/6051 is the z14 tile over AD_A, so it covers the extract and the
    # answer is a populated tile: what is graded is the encoder's output and not
    # only the route to it.  `mvt-tile-empty` names a tile far outside the
    # extract, and the pair is what separates a working encoder from a route
    # that answers 200 with nothing in it.
    #
    # The bodies are compared.  A protobuf tile's float packing looks like
    # something neither side controls byte-for-byte across JITs, and measured it
    # is not: this tile hashes identically three times in one process and again
    # after a restart, which is the comparison stage 2 actually makes --
    # reference and submission are two separate server processes.  The encoder is
    # `no.ecc.vectortile.VectorTileEncoder`, a vendored class carrying no
    # framework annotations, so a port that rewrites the resource and keeps
    # calling it emits the same bytes.
    #
    # With bodies ignored, both cases would grade dispatch and media type alone,
    # and a submission that routed /mvt correctly and returned zero bytes would
    # pass each of them.
    a(case("mvt-tile", "endpoints", "GET", "/mvt/14/8261/6051.mvt",
           "/mvt/{z}/{x}/{y}.mvt: three path templates in one pattern, an "
           "extension in the last segment, and application/x-protobuf rather "
           "than JSON.  This tile covers the extract, so the answer is a "
           "populated tile and the encoder's output is graded rather than only "
           "the route to it",
           body_mode="exact"))
    a(case("mvt-tile-empty", "endpoints", "GET", "/mvt/14/1/1.mvt",
           "a tile with no data in it — same route, and the answer upstream is "
           "an empty tile rather than a 404.  Held against `mvt-tile`, which is "
           "populated: the pair separates a working encoder from a route that "
           "answers 200 with nothing in it",
           body_mode="exact"))

    # --- map matching --------------------------------------------------------
    # NOTE: /match is a web-bundle resource over the map-matching module's engine,
    # and it is the one resource whose path annotation is written fully qualified
    # (`@jakarta.ws.rs.Path("match")`), so a grep for `@Path(` does not find it.
    # The GPX fixture is committed in data/ rather than generated, so both sides
    # are asked about identical bytes.
    a(case("match-gpx", "endpoints", "POST",
           "/match?" + _q(("profile", "car")),
           "/match takes a GPX document as the request body and answers a route "
           "— the only endpoint whose input is XML, and a separate reader from "
           "everything else in the tree",
           headers={"Content-Type": "application/gpx+xml"},
           body="@match-andorra.gpx"))
    a(case("match-gpx-json-out", "endpoints", "POST",
           "/match?" + _q(("profile", "car"), ("type", "json")),
           "the same POST asking for JSON instead: one endpoint, two response "
           "languages, chosen by a query parameter",
           headers={"Content-Type": "application/gpx+xml"},
           body="@match-andorra.gpx"))

    # --- the Mapbox-compatible navigation API --------------------------------
    # /navigate is the endpoint most likely to be missed entirely.  It lives in a
    # separate Maven module, it is registered from the Application class rather
    # than from the bundle every other resource comes through, and it speaks a
    # foreign API shape (Mapbox Directions v5) that shares no field names with
    # the rest of the tree.
    #
    # It also has a property no other endpoint has: it reads the RAW request URI
    # off the servlet request and slices a fixed prefix off it by string length,
    # rather than taking its coordinates from a path parameter.  A port that
    # mounts the same handler under a different prefix, or that lets its framework
    # normalise the path before the handler sees it, answers these two cases
    # differently while every declared route still looks correct.
    #
    # Coordinates here are lon,lat -- the opposite order from /route's `point` --
    # and semicolon-separated, which is the Mapbox convention this resource
    # implements rather than a GraphHopper one.
    _NAV = "/navigate/directions/v5/gh/driving/1.5218,42.5063;1.5906,42.5432"
    _NAV_FLAGS = (("steps", "true"), ("voice_instructions", "true"),
                  ("banner_instructions", "true"), ("roundabout_exits", "true"),
                  ("geometries", "polyline6"))
    a(case("navigate-driving", "endpoints", "GET",
           _NAV + "?" + _q(*_NAV_FLAGS),
           "the Mapbox-shaped navigation response, with every flag the resource "
           "insists on set: a different serialisation of the same route, from a "
           "module registered outside the bundle",
           profile="base"))
    a(case("navigate-geometries-refused", "endpoints", "GET",
           _NAV + "?" + _q(*(_NAV_FLAGS[:-1] + (("geometries", "polyline"),))),
           "the resource throws IllegalArgumentException for any geometry format "
           "but polyline6, and a registered mapper turns that into a 400 with a "
           "JSON envelope — the error contract of a resource that validates by "
           "throwing rather than by annotation",
           profile="base"))
    return C


def _pt_cases():
    """The public-transit stack, under the `pt` profile.

    Separate module and separate weight because it is separately losable: it is
    bound through its own mechanism, it needs a GTFS feed to be reachable at all,
    and a rewrite can pass every other module in this suite with the entire PT
    subsystem unregistered.
    """
    C = []
    a = C.append
    # A departure inside sample-feed's service calendar.  Fixed, not `now`:
    # a relative time would make both sides answer differently as the clock
    # crosses a service boundary mid-run.
    #
    # The feed's times are LOCAL to its agency (America/Los_Angeles) while these
    # are instants, so 08:00Z is 00:00 local on a Monday -- outside the 6:00-22:00
    # frequency windows that CITY runs in, and on a weekday, so the WE and SAT
    # services are out too.  That is worth stating because it decides which pair
    # can demonstrate what: at this instant the only Monday service reaching AMV
    # is a single block, and a single block cannot produce more than one
    # Pareto-optimal itinerary however wide a profile window asks.
    dep = "2007-01-01T08:00:00Z"
    # 16:00Z is 08:00 local, inside CITY's ten-minute headway window.
    dep_dense = "2007-01-01T16:00:00Z"

    # Two adjacent stops on North Ave, 595 m apart -- close enough that walking
    # always beats boarding, which is what makes them the walk-only pair and
    # disqualifies them from every case that needs a vehicle.
    NADAV = "36.914893,-116.76821"
    NANAA = "36.914944,-116.761472"
    # Far enough apart that transit wins: the router boards three blocks in
    # sequence and the response carries trip ids, route ids, stops and headsigns.
    BEATTY_AIRPORT = "36.868446,-116.784582"
    AMV = "36.641496,-116.40094"
    # The two ends of CITY's line, where several departures exist in one window.
    STAGECOACH = "36.915682,-116.751677"
    EMSI = "36.905697,-116.76218"

    # This case has to BOARD something.  Asked between the two North Ave stops it
    # answered a single walk leg with `transfers: -1` and no `trips` at all, so
    # the transit serialiser this module exists to grade -- trip_id, route_id,
    # stops, trip_headsign, feed_id, is_in_same_vehicle_as_previous, thirteen
    # keys a car-only port never writes -- was never reached by any graded case.
    a(case("pt-route", "pt", "GET",
           "/route-pt?" + _q(("point", BEATTY_AIRPORT), ("point", AMV),
                             ("pt.earliest_departure_time", dep),
                             ("profile", "pt")),
           "/route-pt is the transit router: a different resource, a different "
           "router implementation, and a timestamp parameter no other endpoint "
           "has.  These two stops are far enough apart that the answer boards "
           "three vehicles in sequence, so the itinerary carries trip ids, route "
           "ids, per-leg stop lists and headsigns -- the transit vocabulary "
           "nothing else in this corpus asks for", profile="pt"))
    # Stays on the walk-only pair, and deliberately.  Asked on the boarding pair
    # the backwards search answers ZERO itineraries -- an empty `paths` array,
    # which both sides would produce and compare equal on.  Here the answer is
    # one itinerary anchored at the arrival instant instead of the departure one,
    # so every time in it moves and the reversal is visible.
    a(case("pt-route-arrive-by", "pt", "GET",
           "/route-pt?" + _q(("point", NADAV), ("point", NANAA),
                             ("pt.earliest_departure_time", dep),
                             ("pt.arrive_by", "true"), ("profile", "pt")),
           "`pt.arrive_by=true` reverses the search direction — a boolean that "
           "changes which algorithm runs rather than which field is emitted, and "
           "the itinerary comes back anchored at the arrival instant rather than "
           "the departure one",
           profile="pt"))
    # The cardinality claim needs a pair AND an instant that can deliver it.
    # On the North Ave pair at 08:00Z this answered exactly one itinerary, the
    # same one the plain form gives, so the case asserted nothing about range
    # queries.  CITY's ends at 08:00 local answer two: the walk, and the first
    # departure that beats it.
    a(case("pt-route-profile-query", "pt", "GET",
           "/route-pt?" + _q(("point", STAGECOACH), ("point", EMSI),
                             ("pt.earliest_departure_time", dep_dense),
                             ("pt.profile", "true"), ("profile", "pt")),
           "`pt.profile=true` asks for a range query and answers several "
           "itineraries; the response's cardinality moves with it — one "
           "itinerary without the flag, two with it, so a port that binds the "
           "flag but not the range algorithm answers the wrong number of them",
           profile="pt"))
    # `3600` was rejected outright: this parameter is a java.time.Duration and
    # wants ISO-8601, so the case spent its whole life comparing one 400 against
    # another.  PT1H is the same intent, accepted.  It does not change the answer
    # here -- walking already wins between two stops 595 m apart -- and that is
    # the point of the case now: it is the walk-only shape, `transfers: -1` and a
    # single leg with no trip attached, held next to pt-route's boarded
    # itinerary.  It is also the corpus's only Duration-typed parameter, so a
    # port that binds Duration as a bare number of seconds fails here and
    # nowhere else.
    a(case("pt-route-walk-only", "pt", "GET",
           "/route-pt?" + _q(("point", NADAV), ("point", NANAA),
                             ("pt.earliest_departure_time", dep),
                             ("pt.limit_street_time", "PT1H"),
                             ("profile", "pt")),
           "the walking-only itinerary: the transit router still answers when "
           "boarding nothing beats walking, with one leg and no trip on it.  The "
           "street-time limit is an ISO-8601 Duration, the only one in this "
           "corpus, so binding it as a bare integer of seconds fails here alone",
           profile="pt"))
    # `result` takes `multipoint` or, for every other value, the multipolygon
    # default -- PtIsochroneResource branches on that one string and has no third
    # arm.  `multipoint` is the branch.
    a(case("pt-isochrone", "pt", "GET",
           "/isochrone-pt?" + _q(("point", NADAV),
                                 ("pt.earliest_departure_time", dep),
                                 ("time_limit", "3600"),
                                 ("result", "multipoint"), ("profile", "pt")),
           "/isochrone-pt is a fifth resource reachable only under this "
           "profile, and `result=multipoint` selects the non-default one of its "
           "two response shapes — a MultiPoint geometry where the default "
           "answers a MultiPolygon, so a port that ignores the parameter is "
           "caught here",
           profile="pt"))
    # 14/2877/6381 is the z14 tile over NADAV, the same point pt-isochrone asks
    # about.  14/3131/6415 was at 36.33N 111.20W -- northern Arizona, some 500 km
    # east of the beatty extract -- and answered an empty tile, so this case
    # graded the route to the resource and never the transit layer in it.
    a(case("pt-mvt", "pt", "GET",
           "/pt-mvt/14/2877/6381.mvt?" + _q(
               ("pt.earliest_departure_time", dep),
               ("time_limit", "3600"), ("profile", "pt")),
           "/pt-mvt is the transit tile endpoint: the same three-template "
           "pattern as /mvt on a different resource, so a rewrite that ports "
           "one pattern and not the other is caught here.  This tile covers the "
           "feed's stops, so the answer carries the transit layer rather than "
           "being an empty tile that any wired-up route can produce",
           profile="pt", body_mode="exact"))
    a(case("pt-info", "pt", "GET", "/info",
           "/info under the PT profile reports a different profile list than "
           "under `base` — the same endpoint proving config actually reached it",
           profile="pt"))
    return C


def _error_cases():
    """Errors, weighted second-heaviest on purpose.

    A rewrite that gets the happy paths right and the errors wrong is the single
    most common outcome of a framework migration, because error handling is the
    part frameworks disagree about most and the part a developer never looks at
    while porting.  Dropwizard's Jersey stack maps a thrown exception to a JSON
    body with a specific shape and status; Spring's default is a different body
    with a different shape.  Both are "reasonable"; only one matches.
    """
    C = []
    a = C.append

    # --- 400s from parameter validation -------------------------------------
    a(case("err-no-point", "errors", "GET",
           "/route?" + _q(("profile", "car")),
           "no `point` at all: the emptiest possible bad request, and the one "
           "whose message a rewrite is most likely to replace with its "
           "framework's default"))
    a(case("err-one-point", "errors", "GET",
           "/route?" + _q(("point", AD_A), ("profile", "car")),
           "one point where two are needed — arity validation, distinct from "
           "absence"))
    a(case("err-malformed-point", "errors", "GET",
           "/route?" + _q(("point", "not-a-coordinate"), ("point", AD_B),
                          ("profile", "car")),
           "a `point` that cannot be parsed: the failure happens in a type "
           "converter rather than in application code, which is precisely where "
           "the two frameworks' defaults diverge"))
    a(case("err-point-out-of-range", "errors", "GET",
           "/route?" + _q(("point", "999,999"), ("point", AD_B),
                          ("profile", "car")),
           "a syntactically valid coordinate that is not on Earth: parses, then "
           "fails a range check further in"))
    a(case("err-unknown-profile", "errors", "GET",
           "/route?" + _q(("point", AD_A), ("point", AD_B),
                          ("profile", "helicopter")),
           "an unknown profile name: the error enumerates the configured "
           "profiles, so it also proves config reached the error path"))
    a(case("err-no-profile", "errors", "GET",
           "/route?" + _q(("point", AD_A), ("point", AD_B)),
           "no profile: a missing required parameter, which some frameworks "
           "report as 400 and others as 500"))
    a(case("err-unroutable", "errors", "GET",
           "/route?" + _q(("point", AD_A), ("point", OFF_MAP),
                          ("profile", "car")),
           "a point outside the imported extract: an application-level failure "
           "with a per-point message, not a validation failure"))
    a(case("err-bad-integer", "errors", "GET",
           "/isochrone?" + _q(("point", AD_A), ("profile", "car"),
                              ("time_limit", "abc")),
           "a non-numeric value for an int parameter: the framework's own "
           "converter fails before any application code runs"))
    a(case("err-bad-boolean", "errors", "GET",
           "/route?" + _q(("point", AD_A), ("point", AD_B), ("profile", "car"),
                          ("instructions", "maybe")),
           "a non-boolean for a boolean parameter — where the two stacks "
           "disagree most quietly: one coerces to false, one rejects"))
    a(case("err-unknown-details", "errors", "GET",
           "/route?" + _q(("point", AD_A), ("point", AD_B), ("profile", "car"),
                          ("details", "not_a_detail")),
           "a valid parameter with an invalid value, validated by the "
           "application against the graph's encoded values"))
    a(case("err-ch-custom-model", "errors", "POST",
           "/route",
           "a `custom_model` against a CH profile, which upstream REFUSES: the "
           "one case where the correct answer to a well-formed request is an "
           "error, and a rewrite that accepts it answers 200 where State A "
           "answers 400",
           headers={"Content-Type": "application/json"},
           body=json.dumps({
               "points": [[1.5218, 42.5063], [1.5906, 42.5432]],
               "profile": "car",
               "custom_model": {"speed": [
                   {"if": "true", "multiply_by": "0.9"}]},
           }).encode()))

    # --- malformed bodies ----------------------------------------------------
    a(case("err-post-malformed-json", "errors", "POST", "/route",
           "a body that is not JSON at all: the parse fails inside the message "
           "reader, the outermost layer either framework controls",
           headers={"Content-Type": "application/json"},
           body=b"{this is not json"))
    a(case("err-post-empty-body", "errors", "POST", "/route",
           "an empty body with a JSON content type — the degenerate case of the "
           "same reader",
           headers={"Content-Type": "application/json"}, body=b""))
    a(case("err-post-wrong-type", "errors", "POST", "/route",
           "a well-formed JSON body typed as text/plain: content-type "
           "negotiation failing rather than parsing, which is a 415 upstream",
           headers={"Content-Type": "text/plain"},
           body=json.dumps({"points": [[1.5218, 42.5063], [1.5906, 42.5432]],
                            "profile": "car"}).encode()))
    a(case("err-post-json-wrong-shape", "errors", "POST", "/route",
           "valid JSON of the wrong shape (a string where an array belongs): "
           "deserialization failing on types rather than on syntax",
           headers={"Content-Type": "application/json"},
           body=json.dumps({"points": "here and there",
                            "profile": "car"}).encode()))

    # --- dispatch-level errors ----------------------------------------------
    a(case("err-404-unknown", "errors", "GET", "/no-such-endpoint",
           "a path nothing is registered at: the 404 body is the framework's, "
           "and it is the single most visible difference between an "
           "unconfigured Spring Boot and a Dropwizard application"))
    a(case("err-404-near-miss", "errors", "GET", "/routes",
           "a path one character from a real one; a prefix-matching mistake "
           "answers 200 here where upstream answers 404"))
    a(case("err-405-wrong-method", "errors", "DELETE",
           "/route?" + _q(("point", AD_A), ("point", AD_B),
                          ("profile", "car")),
           "DELETE on a GET/POST resource: 405 with an Allow header, which a "
           "framework that registers one method per path turns into a 404"))
    a(case("err-405-on-info", "errors", "POST", "/info",
           "POST to a GET-only resource, on a resource with no POST method at "
           "all — the other shape of the same disagreement"))
    a(case("err-trailing-slash", "errors", "GET",
           "/route/?" + _q(("point", AD_A), ("point", AD_B),
                           ("profile", "car")),
           "a trailing slash: Jersey and Spring differ on whether this matches, "
           "and it is a one-character difference no submission would think to "
           "test"))
    a(case("err-encoded-slash", "errors", "GET", "/i18n/de%2Fen",
           "an encoded slash inside a path template: whether the container "
           "decodes before matching decides between a 200, a 404 and a 400"))
    a(case("err-double-slash-mid", "errors", "GET", "/i18n//de",
           "an empty path segment in the middle of a path — a normalization "
           "question the container answers, not the application"))
    return C


def _header_cases():
    """Headers, asked as their own module because they are separately losable.

    Compared as a name -> values map, never as an ordered sequence: HTTP does not
    order distinct header names, and both stacks are free to reorder them.  The
    hop-by-hop and volatile names are dropped by harness/normalize.py; what is
    left is what a client can actually depend on.
    """
    C = []
    a = C.append

    a(case("hdr-cors-get", "headers", "GET",
           "/route?" + _q(("point", AD_A), ("point", AD_B), ("profile", "car")),
           "the CORS response headers on a normal GET.  State A adds these from "
           "a servlet filter registered outside the resource layer, which is "
           "exactly the kind of cross-cutting registration a rewrite forgets",
           headers={"Origin": "https://example.org"}))
    a(case("hdr-cors-preflight", "headers", "OPTIONS",
           "/route?" + _q(("point", AD_A), ("point", AD_B), ("profile", "car")),
           "the CORS preflight: an OPTIONS request no resource method declares, "
           "answered by the filter alone",
           headers={"Origin": "https://example.org",
                    "Access-Control-Request-Method": "GET",
                    "Access-Control-Request-Headers": "content-type"}))
    a(case("hdr-cors-on-error", "headers", "GET",
           "/route?" + _q(("profile", "car")),
           "CORS headers on a 400: a filter that runs only on success drops "
           "them here, and a browser then reports a CORS error instead of the "
           "server's actual message",
           headers={"Origin": "https://example.org"}))
    a(case("hdr-cors-on-404", "headers", "GET", "/no-such-endpoint",
           "CORS headers on a container-level 404, where no application code "
           "runs at all — the deepest test of where the filter is registered",
           headers={"Origin": "https://example.org"}))
    a(case("hdr-gzip-accepted", "headers", "GET",
           "/route?" + _q(("point", AD_A), ("point", AD_B), ("profile", "car"),
                          ("details", "road_class"), ("details", "surface")),
           "`Accept-Encoding: gzip` on a body large enough to be worth "
           "compressing: whether the response is encoded, and whether Vary "
           "says so, are both server-configuration questions",
           headers={"Accept-Encoding": "gzip"}))
    a(case("hdr-gzip-refused", "headers", "GET",
           "/route?" + _q(("point", AD_A), ("point", AD_B), ("profile", "car")),
           "the same request with `identity` — the control for the case above",
           headers={"Accept-Encoding": "identity"}))
    a(case("hdr-accept-json", "headers", "GET",
           "/route?" + _q(("point", AD_A), ("point", AD_B), ("profile", "car")),
           "an explicit `Accept: application/json` against a method declaring "
           "three producible types",
           headers={"Accept": "application/json"}))
    # `Accept: application/xml` names a type this method DECLARES it produces, and
    # gets JSON anyway.  Jersey matches the method on Accept against @Produces,
    # then the method builds its own Response with an explicit
    # `.type(APPLICATION_JSON)` -- and the GPX branch is chosen by the `type` query
    # parameter, never by Accept (RouteResource: `writeGPX = "gpx".equals(type)`).
    # So the graded answer is 200 with `application/json`, which a submission whose
    # framework honours Accept for real will get wrong by returning XML or 406.
    #
    # json mode, not exact.  The body carries `info.took` -- the stopwatch -- so
    # comparing it byte-for-byte passes or fails on whether two sub-millisecond
    # durations round to the same integer.  The signal this case is for is the media
    # type, which harness.diff compares before the body either way.
    a(case("hdr-accept-xml", "headers", "GET",
           "/route?" + _q(("point", AD_A), ("point", AD_B), ("profile", "car")),
           "`Accept: application/xml` names a type @Produces declares, and the "
           "answer is still `application/json`, because the method sets the type "
           "itself and only `type=gpx` switches it",
           headers={"Accept": "application/xml"}))
    a(case("hdr-accept-unacceptable", "headers", "GET",
           "/route?" + _q(("point", AD_A), ("point", AD_B), ("profile", "car")),
           "an Accept nothing can satisfy: 406, and a stack that ignores Accept "
           "answers 200 with JSON",
           headers={"Accept": "application/vnd.nonsense"}))
    a(case("hdr-head-route", "headers", "HEAD",
           "/route?" + _q(("point", AD_A), ("point", AD_B), ("profile", "car")),
           "HEAD on a GET resource: same headers, no body, and the length "
           "header has to agree with the GET the client did not make"))
    a(case("hdr-head-info", "headers", "HEAD", "/info",
           "HEAD on a second resource, because HEAD is usually synthesized once "
           "and either works everywhere or nowhere"))
    return C


def _asset_cases():
    """The bundled web UI.

    State A serves it from two AssetsBundle registrations, which is a mechanism
    with no Spring equivalent — so this is one of the few places where a rewrite
    has to make a real decision rather than translate an annotation.  Static
    serving is also the easiest thing to leave out entirely and never notice,
    since no API client touches it.
    """
    C = []
    a = C.append
    a(case("asset-maps-index", "assets", "GET", "/maps/",
           "the UI's index at its directory path: a welcome-file mapping, not a "
           "file lookup, and the case a naive static handler answers 404 to",
           body_mode="exact"))
    a(case("asset-maps-noslash", "assets", "GET", "/maps",
           "the same directory without the trailing slash — a redirect upstream, "
           "and the two stacks differ on its status code and Location"))
    a(case("asset-maps-missing", "assets", "GET", "/maps/no-such-file.js",
           "a missing file under a mapped prefix: 404 from the asset handler "
           "rather than from the router"))
    a(case("asset-root", "assets", "GET", "/",
           "the application root, which is a registered resource rather than an "
           "asset and must not be shadowed by the asset mapping"))
    return C


def _admin_cases():
    """The admin connector on its own port.

    Dropwizard puts these on a second port; Spring Boot's equivalents live under
    a management context with different paths and different bodies.  Nothing in
    the migration instruction says "drop the admin port", so a rewrite that does
    has lost a documented, externally-visible surface — and one that operators
    depend on more than they depend on any single route parameter.
    """
    C = []
    a = C.append
    a(case("admin-root", "admin", "GET", "/",
           "the admin port's index, which lists the tasks and endpoints it "
           "serves", port="admin", body_mode="exact"))
    a(case("admin-ping", "admin", "GET", "/ping",
           "/ping answers a fixed two-word body; the cheapest possible proof "
           "the port is alive and the least excusable thing to get wrong",
           port="admin", body_mode="exact"))
    a(case("admin-healthcheck", "admin", "GET", "/healthcheck",
           "/healthcheck reports one entry per registered check — the graph's "
           "and the deadlock detector's.  Compared structurally: the keys are "
           "the contract, the durations are not",
           port="admin"))
    a(case("admin-tasks", "admin", "GET", "/tasks",
           "/tasks lists the admin tasks by name; a rewrite that keeps the port "
           "and drops the tasks is visible only here",
           port="admin", body_mode="exact"))
    a(case("admin-metrics", "admin", "GET", "/metrics",
           "/metrics is a JSON document whose VALUES move constantly (heap, "
           "thread counts, timers).  Body ignored, status and media type "
           "compared: the graded question is whether the endpoint exists and "
           "answers JSON, and its numbers are unstable by design",
           port="admin", body_mode="ignore"))
    a(case("admin-threads", "admin", "GET", "/threads",
           "/threads dumps live stacks — as volatile as a body gets, so the "
           "status and content type are the contract and the dump is not",
           port="admin", body_mode="ignore"))
    # The next two are `ignore`, and the reason is the same for both: the admin
    # connector's 404 is the container's own HTML error page, and it names the
    # servlet that produced it with a JVM identity hash in it —
    # `AdminServlet-f1a45f8` against `AdminServlet-2f1ea80d`.  That hash is a
    # property of one JVM run and could not agree between two.  It is also not
    # JSON, so `json` mode was a construction error in this corpus and reported
    # itself as one.
    #
    # Ignoring the body costs these two cases nothing, because neither one's
    # signal is in the body.  admin-unknown grades the MEDIA TYPE — the admin
    # 404 is text/html where the application 404 is the API's JSON envelope —
    # and admin-app-path-on-admin grades the STATUS, 404 against the 200 a
    # collapsed pair of connectors would answer.  Both are still compared, along
    # with every header.  It is the same decision as admin-metrics and
    # admin-threads above, for the same reason.
    a(case("admin-unknown", "admin", "GET", "/no-such-admin-thing",
           "a 404 on the admin port, which is served by a different handler "
           "than the application port's 404: the graded difference is the media "
           "type, text/html from the container against the application port's "
           "JSON error envelope.  The body is the container's error page, which "
           "names the servlet instance with a per-JVM identity hash, so it is "
           "not comparable and is not compared",
           port="admin", body_mode="ignore"))
    a(case("admin-app-path-on-admin", "admin", "GET",
           "/route?" + _q(("point", AD_A), ("point", AD_B), ("profile", "car")),
           "an APPLICATION path asked on the ADMIN port: it must not answer.  "
           "A rewrite that collapses both connectors onto one port passes every "
           "other admin case and fails this one — on the STATUS, 404 against "
           "200, which is why the container's per-JVM error page not being "
           "comparable costs this case nothing",
           port="admin", body_mode="ignore"))
    a(case("admin-admin-path-on-app", "admin", "GET", "/ping",
           "and the converse — an admin path on the application port.  Together "
           "these two pin the separation itself rather than either side of it",
           port="app"))
    return C


def all_cases() -> list[dict]:
    cases = (_routing_cases() + _endpoint_cases() + _pt_cases()
             + _error_cases() + _header_cases() + _asset_cases()
             + _admin_cases())
    seen = {}
    for c in cases:
        if c["id"] in seen:
            raise AssertionError(f"duplicate case id: {c['id']}")
        seen[c["id"]] = c
        if c["profile"] not in PROFILES:
            raise AssertionError(f"{c['id']}: unknown profile {c['profile']}")
    return cases


def cases_for(suite: str) -> list[dict]:
    return [c for c in all_cases() if c["suite"] == suite]


def fingerprint() -> str:
    """A digest over every field that decides what gets asked.

    Not decoration.  Stage 3's probe checker reads it to confirm an adversary's
    candidate is being judged against the same corpus this stage graded, and the
    suite image asserts it at build time.  `why` is excluded — it is commentary,
    and re-wording it should not read as a corpus change.
    """
    h = hashlib.sha256()
    for c in sorted(all_cases(), key=lambda c: c["id"]):
        body = c["body"]
        if isinstance(body, str):
            body = body.encode()
        h.update("\x1f".join([
            c["id"], c["suite"], c["profile"], c["method"], c["target"],
            c["body_mode"], c["port"],
            json.dumps(c["headers"], sort_keys=True),
        ]).encode())
        h.update(b"\x1e")
        h.update(body or b"")
        h.update(b"\x1d")
    for name, spec in sorted(PROFILES.items()):
        h.update(f"{name}\x1f{spec['config']}\x1e".encode())
    return h.hexdigest()[:16]
