"""HTTPS.

``--tls-cert`` and ``--tls-key`` with the three key encodings a user is likely
to have on disk -- PKCS#8, PKCS#1 and an EC key -- plus one session that turns
TLS on alongside everything else, because the interaction is where it breaks.

This surface is mostly a build question wearing a runtime hat.  TLS support is
behind a Cargo feature; a port that rewires ``Cargo.toml`` while migrating is
one edit away from a binary whose ``--tls-cert`` flag no longer exists, and
this is where that shows up as four sessions that cannot be started rather than
as a compile error.

Graded by 1 archive download, 15 recorded cases, 1 negotiated encoding, 9
rendered pages, 8 pages with per-boot asset routes and 4 configurations.  The
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
