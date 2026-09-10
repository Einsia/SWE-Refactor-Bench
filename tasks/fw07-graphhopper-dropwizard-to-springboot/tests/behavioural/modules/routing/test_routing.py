"""The routing surface: /route, across vehicles, speedups and response shapes.

Every test here is the same shape -- take a case id, pull both sides' answers out
of the capture, compare them -- and that sameness is deliberate.  The judgement in
this module lives in `harness/corpus.py`, which decides which requests are worth
asking, and in `harness/diff.py`, which decides what counts as a difference.  A
test that ALSO decided what the right answer was would be a third place for the
two to disagree, and the first place a hand-written expectation would drift away
from the service it describes.

Written one function per case rather than parametrised over `cases_in("routing")`,
because a named failure is a diagnosis.  `test_bike_route_uses_landmarks` failing
tells a reader which behaviour broke; `test_case[14]` failing tells them to go
read a table.  The count is asserted separately, so a case added to the corpus and
not to this file is caught rather than silently ungraded.
"""
from __future__ import annotations


def test_car_route(pair):
    """The plainest route there is: two Andorran points, the CH-backed car profile."""
    pair("route-plain-car").check()


def test_bike_route_uses_landmarks(pair):
    """Bike is the LM profile.  CH and LM reach the same answer through different
    code, so a submission that bound only one of the two speedups fails here and
    passes the car case."""
    pair("route-plain-bike").check()


def test_foot_route_has_no_speedup(pair):
    """Foot is bound with neither CH nor LM, so this is the flexible-mode search.
    Three vehicles, three different code paths to the same response schema."""
    pair("route-plain-foot").check()


def test_route_through_a_via_point(pair):
    """Three points.  The response gains legs, and the instruction stream has to
    be stitched across them in the same order."""
    pair("route-three-points").check()


def test_path_details_single(pair):
    """`details` is a map from name to a list of [from, to, value] intervals.  It
    is the most structurally awkward field in the response and the easiest one to
    serialise as the wrong container."""
    pair("route-details-single").check()


def test_path_details_multiple(pair):
    """Two details requested at once: the map has two keys, and each interval list
    is indexed against the same point array."""
    pair("route-details-multi").check()


def test_route_without_instructions(pair):
    """instructions=false removes a field rather than emptying it."""
    pair("route-no-instructions").check()


def test_route_without_points(pair):
    """calc_points=false: the geometry goes away and the summary stays."""
    pair("route-no-points").check()


def test_route_with_points_unencoded(pair):
    """points_encoded=false changes `points` from a polyline STRING to an object
    with a coordinate array.  A field that changes type between two requests is
    the kind of thing a reimplementation gets right in one branch and wrong in
    the other."""
    pair("route-points-encoded-off").check()


def test_route_with_elevation_off(pair):
    """Asking for no elevation is a different request from not asking, and the
    graph here carries none either way -- so the answer is about how the
    parameter was bound, not about the terrain."""
    pair("route-elevation-off").check()


def test_route_localised_instructions(pair):
    """locale=de pulls German strings out of the translation map.  This case is
    also the one that fails loudly if the 49 translation files did not make it
    into the jar's resources -- which is exactly what an epoch-0 mtime and an
    incremental resource copy will do to them."""
    pair("route-locale-de").check()


def test_route_alternatives(pair):
    """algorithm=alternative_route returns several paths, and their ORDER is part
    of the answer: the best one is first."""
    pair("route-algorithm-alt").check()


def test_route_as_gpx(pair):
    """The GPX representation of a route: same resource, same method, different
    Accept -- and an XML document rather than JSON.  Compared as exact bytes
    modulo the timestamp the harness masks, because GPX is a schema and a
    reordered element is a different document."""
    pair("route-gpx").check()


def test_route_as_gpx_without_waypoints(pair):
    """gpx.waypoints toggles whether the document carries <wpt> elements at all.
    An XML response whose shape depends on a query parameter."""
    pair("route-gpx-nowaypoints").check()


def test_route_post(pair):
    """The POST resource is a separate Jersey method on the same path, taking a
    JSON body instead of query parameters."""
    pair("route-post-basic").check()


def test_route_post_custom_model_on_lm(pair):
    """A custom model sent inline, against the landmark profile -- the only way to
    reach the request-body deserialiser and the custom-model machinery in one
    request.  CH refuses custom models, LM accepts them, and the difference is
    part of the contract."""
    pair("route-post-custom-model-lm").check()


def test_route_post_with_headings(pair):
    """Per-point headings in the POST body: an array whose length has to match the
    point array, bound from JSON rather than from repeated query parameters."""
    pair("route-post-headings").check()


def test_every_routing_case_is_graded(cases_in):
    """A case added to the corpus and not to this file would be recorded, paid for
    in wall clock, and never compared.  This is the check that makes the one
    function per case convention hold instead of merely being the current state.
    """
    graded = {
        name.removeprefix("test_")
        for name, obj in globals().items()
        if name.startswith("test_") and callable(obj)
    }
    # Not a name comparison -- the test names are prose, not case ids.  Just the
    # count, which is what actually goes wrong when someone extends the corpus.
    expected = len(cases_in("routing"))
    assert len(graded) - 1 == expected, (
        f"the corpus has {expected} routing case(s) and this module has "
        f"{len(graded) - 1} grading test(s).  Add a test per case: an ungraded "
        f"case costs the same wall clock and measures nothing."
    )
