"""What a request that cannot be served gets.

The failure paths: a missing file, a path that escapes the served root, a
hidden file when hidden files are off, a malformed query, a directory operation
on a file.  All of them from the unflagged server, routed here by case id.

miniserve renders these as pages, not as bare status lines -- a 404 carries the
same shell, theme and footer as a listing does.  Two things go wrong in a port.
It answers the right status with the wrong body, usually a framework default
like axum's empty ``404 Not Found``; or it answers a *different* status, most
often turning a deliberate 400 into a 404 because the query parser failed
before the route matched.  Both are visible to a user and neither is visible in
a diff of the happy path.

Graded by 27 recorded cases, 23 rendered pages and 19 pages with per-boot asset
routes.  The assertions are in ``lib/battery.py`` and the routing that decides
which cases reach them is in ``lib/routing.py``; this file is the list of what
this surface is held to.
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
    test_status,
)
