"""Which request reaches which handler, and what the ones that reach none get.

This is the module the task turns on.  The retired router matched `/files` as a
*string* prefix; the replacement matches path *segments*, and every consequence of
that difference is a case here.  `GET /filesabc` reached the file handler and came
back 404 with the handler's own error body; on a segment-matching router it reaches
nothing at all and comes back 404 with a different body and no CORS header.  Those
two 404s are the same number and a different response, and telling them apart is
most of the work.

The other half is path cleaning.  The retired router cleaned `/files/../a.txt` to
`/a.txt` and redirected, before any handler ran; the replacement does its own
cleaning with its own status and its own `Location`.  Six cases pin what that looks
like, including the one that keeps a query string across the redirect.

Nothing here names a pattern, a registration or a handler.  Each test compares one
recorded response to one actual response, so a submission is free to arrive at
these responses any way it likes -- and a submission that gets them by special-
casing the exact strings in this corpus is what stage 3 exists to find.
"""
from __future__ import annotations

import pytest
import srbcheck
import srbfixtures

CASES = srbfixtures.cases_in("routing")


@pytest.mark.parametrize("case_id", CASES)
class TestRecordedRouting:
    """One class so the five checks read as five columns over the same case."""

    def test_status(self, pair, case_id):
        srbcheck.status(pair)

    def test_header_names_and_order(self, pair, case_id):
        srbcheck.header_names(pair)

    def test_header_values(self, pair, case_id):
        srbcheck.header_values(pair)

    def test_body_bytes(self, pair, case_id):
        srbcheck.body(pair)

    def test_content_length_matches_body(self, pair, case_id):
        srbcheck.length_is_honest(pair)


def test_the_corpus_still_covers_the_prefix_rule():
    """The cases that distinguish a string prefix from a path segment are present.

    A guard on the corpus, not on the submission.  These five ids are the only
    reason this module is weighted above the others, and a corpus edit that dropped
    one would leave the module passing 90 cases and grading nothing that the port
    could plausibly get wrong.  It fails here, in the module that owns them, rather
    than silently.
    """
    load_bearing = {"prefix-not-segment", "prefix-not-segment-deep",
                    "prefix-nearly", "prefix-files-suffix", "encoded-slash"}
    missing = sorted(load_bearing - set(CASES))
    assert not missing, (
        f"the routing corpus no longer covers {missing} -- these are the cases "
        f"that separate a string-prefix match from a segment match, and without "
        f"them this module grades only ordinary routing")
