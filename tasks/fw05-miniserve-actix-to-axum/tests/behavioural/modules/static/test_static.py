"""Serving a file.

Handing back the bytes of one file, and the header contract that comes with it:
the guessed content type, ``Accept-Ranges``, the ETag and ``Last-Modified``,
the disposition, and what a conditional or ranged request does to all of them.

This is where the two frameworks differ most mechanically.  actix-files answers
a range with 206 and a ``Content-Range``, refuses an unsatisfiable one with
416, honours ``If-None-Match`` with 304 and ``If-Match`` with 412, and derives
an ETag from the file's metadata.  tower-http's ``ServeDir`` emits no ETag at
all by default, which makes every conditional request unconditional -- a
difference no status code shows and every caching client sees.

Graded by 62 recorded cases, 8 rendered pages, 7 pages with per-boot asset
routes and 2 configurations.  The assertions are in ``lib/battery.py`` and the
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
