"""The bundled web UI.

Four cases and the smallest weight on the stage, here because a service whose API
is flawless but which no longer serves the page a browser loads has not been
migrated.  Static content is a framework feature in both stacks and a DIFFERENT
feature in each -- Dropwizard mounts an AssetsBundle over a classpath directory,
the target framework has its own resource handling with its own defaults for index
documents, directory requests and cache headers -- so each of these is a decision
the submission had to make rather than inherit.
"""
from __future__ import annotations


def test_maps_index(pair):
    """`/maps/` serves the UI's index document.  The one case a human would
    check by hand, and the one an automated suite is most likely to omit."""
    pair("asset-maps-index").check()


def test_maps_without_trailing_slash(pair):
    """`/maps` against a mount at `/maps/`.  A redirect, a serve, or a 404 --
    whichever it is, a browser typing the short form depends on it."""
    pair("asset-maps-noslash").check()


def test_missing_asset(pair):
    """A path under the asset root with no file behind it.  Whether that answers
    with the asset handler's 404 or falls through to the API's error envelope is
    the boundary between the two dispatch trees."""
    pair("asset-maps-missing").check()


def test_root_redirects_to_the_ui(pair):
    """`/` is a resource in its own right -- RootResource, registered from the
    Application class -- and all it does is redirect to `maps/`.  Three
    observations in one: that it exists, its status, and its Location."""
    pair("asset-root").check()


def test_every_asset_case_is_graded(cases_in):
    graded = sum(1 for name, obj in globals().items()
                 if name.startswith("test_") and callable(obj)) - 1
    expected = len(cases_in("assets"))
    assert graded == expected, (
        f"the corpus has {expected} asset case(s) and this module grades "
        f"{graded}."
    )
