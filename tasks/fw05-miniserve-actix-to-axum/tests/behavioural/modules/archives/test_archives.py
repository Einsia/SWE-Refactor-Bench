"""Tar, tar.gz and zip on demand.

Downloading a directory as a stream: the three formats, the subset of them a
flag permits, and how hidden files and symlinks are treated inside the archive.

Graded on the member list and the member contents rather than on bytes, because
two correct archivers disagree about tar block padding, zip extra fields and
compression level.  What that leaves is exactly the part that goes wrong: an
archive rooted at the wrong prefix, a symlink inlined where the baseline
skipped it, hidden files included when ``--hidden`` was off, or a stream that
simply does not unpack.

Graded by 23 archive downloads, 30 recorded cases, 5 rendered pages, 5 pages
with per-boot asset routes and 4 configurations.  The assertions are in
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
