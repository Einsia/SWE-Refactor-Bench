"""The rest of the public API: breadth over depth.

One or two cases per resource, enough to catch an endpoint that was not ported,
was ported under a different path, lost its content type, or answers with a
different envelope.  Depth belongs to `routing` and `errors`; what this module is
for is coverage of the 14 registered resources, so that a submission cannot serve
/route beautifully and quietly drop /spt.
"""
from __future__ import annotations


def test_info(pair):
    """/info is the service description: profiles, bbox, data date, version.

    The version and data-date fields come from the graph the harness built
    moments earlier on both sides, so they agree by construction rather than by
    masking."""
    pair("info").check()


def test_health(pair):
    """The application-port health resource, distinct from the admin port's."""
    pair("health").check()


def test_nearest_on_graph(pair):
    """Snapping a coordinate to the graph: the answer is a point and a distance."""
    pair("nearest").check()


def test_nearest_off_map(pair):
    """A coordinate in Paris against an Andorran graph.  Off-map is a normal answer
    for this resource, not an error, and which of the two it is counts."""
    pair("nearest-offmap").check()


def test_i18n_all_locales(pair):
    """/i18n returns the whole translation map -- every locale, every key.

    The single largest response in the corpus and the most complete check that
    the 49 translation resources are on the classpath."""
    pair("i18n-all").check()


def test_i18n_one_locale(pair):
    """/i18n/{locale} is a sub-resource path on the same class."""
    pair("i18n-de").check()


def test_i18n_unknown_locale(pair):
    """An unknown locale falls back rather than failing, and what it falls back
    TO is the behaviour being preserved."""
    pair("i18n-unknown-locale").check()


def test_isochrone_json(pair):
    """A reachability polygon as GeoJSON."""
    pair("isochrone-json").check()


def test_isochrone_buckets(pair):
    """Several nested polygons from one request: an array whose order is the
    bucket order."""
    pair("isochrone-buckets").check()


def test_isochrone_by_distance(pair):
    """distance_limit instead of time_limit -- the same resource with a different
    exclusive parameter, and exactly one of the two is allowed."""
    pair("isochrone-distance-limit").check()


def test_spt_as_csv(pair):
    """/spt's default representation is CSV, not JSON.  A migration that made
    everything JSON would pass every other case in this module."""
    pair("spt-csv").check()


def test_spt_selected_columns(pair):
    """`columns` chooses which fields the CSV carries, and their order is the
    order given."""
    pair("spt-columns").check()


def test_mvt_tile(pair):
    """A vector tile: protobuf bytes under a path with three template segments and
    a literal `.mvt` suffix.  Body ignored, media type and status compared -- the
    tile's float packing is not stable across runs, but serving HTML here would
    still fail."""
    pair("mvt-tile").check()


def test_mvt_tile_with_no_data(pair):
    """A tile outside the graph's extent answers with an empty tile, not a 404."""
    pair("mvt-tile-empty").check()


def test_match_gpx_in_gpx_out(pair):
    """/match is the only endpoint whose REQUEST is XML.  It consumes a GPX
    document, runs map matching, and answers in the same language."""
    pair("match-gpx").check()


def test_match_gpx_in_json_out(pair):
    """The same POST asking for JSON: one endpoint, two response languages,
    selected by a query parameter rather than by Accept."""
    pair("match-gpx-json-out").check()


def test_navigate_mapbox_shape(pair):
    """/navigate speaks Mapbox Directions v5 -- a foreign schema sharing no field
    names with the rest of the tree, from a module registered outside the bundle
    every other resource comes through.

    It also takes its coordinates by slicing a fixed prefix off the raw servlet
    request URI, so a port that mounts it elsewhere, or whose framework
    normalises the path first, answers this differently while every declared
    route still looks right."""
    pair("navigate-driving").check()


def test_navigate_refuses_other_geometries(pair):
    """The resource validates by throwing IllegalArgumentException, and a
    registered mapper turns that into a 400 with a JSON envelope.  Validation by
    exception rather than by annotation is a different contract to port."""
    pair("navigate-geometries-refused").check()


def test_every_endpoint_case_is_graded(cases_in):
    graded = sum(1 for name, obj in globals().items()
                 if name.startswith("test_") and callable(obj)) - 1
    expected = len(cases_in("endpoints"))
    assert graded == expected, (
        f"the corpus has {expected} endpoint case(s) and this module grades "
        f"{graded}.  An ungraded case costs the same wall clock and measures "
        f"nothing."
    )
