"""HTTP basic auth.

``--auth`` in all the spellings miniserve accepts: a plaintext password, a
``sha256:`` or ``sha512:`` digest, several accounts at once, and accounts read
from a file.  Graded on what an unauthenticated, wrongly authenticated and
correctly authenticated request each receive.

The interesting failures are not "auth is missing" -- that is loud.  They are
the 401 that arrives without a ``WWW-Authenticate`` header, so no browser ever
prompts; the digest comparison that succeeds against the wrong account; and the
middleware mounted so that one route (the favicon, the stylesheet, the upload
endpoint) answers without credentials.

Graded by 45 recorded cases, 35 rendered pages, 25 pages with per-boot asset
routes and 6 configurations.  The assertions are in ``lib/battery.py`` and the
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
