"""Multipart upload and directory creation.

The only surface that writes to disk: ``multipart/form-data`` upload, directory
creation, and the eleven configurations that constrain them -- an allowed
directory list, an overwrite policy, a media-type restriction, uploads into
hidden or symlinked paths, and upload behind auth or a route prefix.

Two things are graded, and the second is the reason this weighs as much as it
does.  The response: a 303 back to the listing, or the specific error for a
rejected name, type or destination.  And the *filesystem*: the served tree is
digested at the end of each session without mtimes, so a port that returns the
baseline's exact 303 and writes the file to the wrong place, under the wrong
name or with the wrong bytes fails here and nowhere else.

Graded by 74 recorded cases, 46 rendered pages, 42 pages with per-boot asset
routes and 11 configurations.  The assertions are in ``lib/battery.py`` and the
routing that decides which cases reach them is in ``lib/routing.py``; this file
is the list of what this surface is held to.
"""

from battery import (            # noqa: F401
    test_body,
    test_body_length,
    test_entry_hrefs_resolve,
    test_header_names,
    test_headers,
    test_html_field,
    test_nonce_route_shape,
    test_query_links,
    test_session_starts,
    test_session_survives,
    test_session_tree,
    test_status,
)
