"""Public transit, on its own graph.

Separate from `endpoints` for two reasons.  It needs a second server launch under
a second configuration -- the PT resources are registered only when the config
names a GTFS file, so under the base profile these paths do not exist at all --
and it is separately losable: a submission can pass every other module here with
the entire transit subsystem unregistered, because nothing else in the corpus
touches it.

The fixtures are the ones the repository tests with: the beatty OSM extract and
the sample GTFS feed, both committed upstream.
"""
from __future__ import annotations


def test_pt_route(pair):
    """A transit itinerary: legs of different types, with times attached.

    Structurally the most unlike a /route response in the tree -- it has trips,
    stops and transfers where routing has instructions -- so it exercises a
    serialiser a car-only migration never touches."""
    pair("pt-route").check()


def test_pt_route_arriving_by(pair):
    """`arrive_by` searches backwards from the arrival time.  Same resource, a
    different traversal, and an itinerary anchored at the other end of the
    journey: every time in the answer moves."""
    pair("pt-route-arrive-by").check()


def test_pt_route_profile_query(pair):
    """The profile-query form returns several departures rather than one, which
    makes the response an array where the plain form gives a single itinerary."""
    pair("pt-route-profile-query").check()


def test_pt_route_walk_only(pair):
    """A pair of points reachable on foot without boarding anything.  The transit
    router still answers, with a walk leg and `transfers: -1` -- the degenerate
    case that a port is likely to turn into an error, held next to
    `test_pt_route`'s boarded itinerary.  Its street-time limit is also the only
    ISO-8601 Duration parameter in the corpus."""
    pair("pt-route-walk-only").check()


def test_pt_isochrone(pair):
    """/isochrone-pt: reachability over the timetable rather than over the road
    network, and a separate resource class from /isochrone."""
    pair("pt-isochrone").check()


def test_pt_vector_tile(pair):
    """/pt-mvt shares its path template with /mvt but is a different resource
    bound only under the transit configuration."""
    pair("pt-mvt").check()


def test_info_reports_the_transit_configuration(pair):
    """/info under the PT profile.  The same endpoint the `endpoints` module asks
    about, asked again with transit configured -- because what changes in its
    answer is how the service describes a configuration it was given, which is
    the part a migration has to carry over."""
    pair("pt-info").check()


def test_every_pt_case_is_graded(cases_in):
    graded = sum(1 for name, obj in globals().items()
                 if name.startswith("test_") and callable(obj)) - 1
    expected = len(cases_in("pt"))
    assert graded == expected, (
        f"the corpus has {expected} transit case(s) and this module grades "
        f"{graded}."
    )
