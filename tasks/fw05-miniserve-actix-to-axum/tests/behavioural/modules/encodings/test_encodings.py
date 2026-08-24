"""Content encodings on the wire.

What ``Accept-Encoding`` negotiates, and what the response then says about
itself.  Both directions are graded: with compression on, a client offering
gzip, deflate, brotli or zstd gets one of them and it decodes; with compression
off, the same requests get identity bodies.

The failure that matters is a port that advertises an encoding it did not
apply, or applies one and forgets to say so.  Either produces a body no client
can read, while the status line and the length both look reasonable.

Graded by 1 archive download, 16 recorded cases, 8 negotiated encodings, 12
rendered pages, 12 pages with per-boot asset routes and 2 configurations.  The
assertions are in ``lib/battery.py`` and the routing that decides which cases
reach them is in ``lib/routing.py``; this file is the list of what this surface
is held to.
"""

from battery import (            # noqa: F401
    test_archive_fact,
    test_archive_unpacks,
    test_body,
    test_body_length,
    test_decoded,
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
