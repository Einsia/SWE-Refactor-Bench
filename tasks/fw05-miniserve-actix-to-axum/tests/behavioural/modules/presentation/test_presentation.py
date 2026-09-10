"""Title, themes, QR code and footers.

The parts of the page that are not the file list: the ``<title>`` and its
default, the colour scheme and the picker that switches it, the QR code, the
wget footer and the version footer.

Each is a whole feature that a port can drop while rendering a page that looks
correct, which is why they are one surface rather than a footnote to the
listing. The QR code is the sharpest of them: it is a generated SVG path, so
matching the module count means the encoded text, the error-correction level
and the version all agree -- a port that regenerates it from a different URL
passes every other assertion and fails this one.

Graded by 36 recorded cases, 24 rendered pages, 21 pages with per-boot asset
routes and 8 configurations.  The assertions are in ``lib/battery.py`` and the
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
