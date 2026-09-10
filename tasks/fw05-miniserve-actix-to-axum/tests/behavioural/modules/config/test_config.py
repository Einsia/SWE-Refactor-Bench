"""Configuration reaching the response.

Configuration that is only observable in what comes back: ``--header`` adding a
response header, ``--header`` overriding one miniserve sets itself, the
``MINISERVE_*`` environment aliases, and the precedence between an alias and an
explicit flag.

Small, and kept as its own surface because it is the one place where the
*plumbing* is what is being graded.  Every flag here has to survive the whole
path from clap through the config struct into the response, and a port that
reads it correctly and never applies it looks identical to one that never read
it -- except in these fifteen cases.

Graded by 1 archive download, 15 recorded cases, 7 rendered pages, 7 pages with
per-boot asset routes and 5 configurations.  The assertions are in
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
