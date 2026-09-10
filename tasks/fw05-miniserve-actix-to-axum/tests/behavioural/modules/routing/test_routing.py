"""Paths, prefixes, redirects and method dispatch.

Where a request lands: the SPA fallback, extensionless pretty URLs, an explicit
``--route-prefix`` with and without surrounding slashes, the per-boot
``--random-route``, the trailing-slash redirect, and which methods are answered
at all.

The prefix sessions are the ones that catch a port out, because the prefix has
to appear in three unrelated places at once -- the routes that match, the links
in every rendered page, and the ``Location`` of a redirect.  A port that mounts
its router under the prefix but builds hrefs without it serves a page whose
every link 404s, and passes any test that only looks at status codes.

Graded by 1 archive download, 60 recorded cases, 40 rendered pages, 31 pages
with per-boot asset routes and 5 configurations.  The assertions are in
``lib/battery.py`` and the routing that decides which cases reach them is in
``lib/routing.py``; this file is the list of what this surface is held to.
"""

from battery import (            # noqa: F401
    test_archive_fact,
    test_archive_unpacks,
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
