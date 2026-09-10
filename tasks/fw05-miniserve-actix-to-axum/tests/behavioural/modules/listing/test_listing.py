"""Directory listings.

The rendered index: which rows, in which order, with which columns, under which
breadcrumbs.  This is the largest surface because it is what miniserve mostly
is, and the sessions here vary the parts of it a flag controls -- hidden files,
the directories-first ordering, each sort key and direction, a served
``index.html``, a rendered README, indexing switched off entirely, and the
three symlink policies.

A port that renders a listing at all gets the file names right.  What it gets
wrong quietly is everything beside them: the humanised size column (``1.4 KiB``
where miniserve writes ``1.40 KiB``), the timestamp format, the sort-link query
strings, the ``.symlink`` row class, and the ordering when two entries collide.

Graded by 4 archive downloads, 232 recorded cases, 207 rendered pages, 196
pages with per-boot asset routes and 15 configurations.  The assertions are in
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
