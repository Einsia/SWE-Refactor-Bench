"""What the token check answers, and -- more to the point -- when it runs.

On the retired router, middleware registered with `Use` ran only after a route had
already matched.  So an unauthenticated request to a path that matched no route
never reached the token check and came back 404, while an unauthenticated request
to a path that *did* match came back 401.  Two of the cases here are that pair:
`/nope` is 404 and `/filesabc` is 401, both with no token, and `/filesabc` is 401
only because a string-prefix match counted as a match.

That makes this module the one place where ordering is observable from outside.  A
submission that wraps its whole mux in an authentication handler answers 401 to
both; one that checks the token inside each handler answers 404 to both.  Both are
defensible designs and both differ from what was recorded, which is the only thing
being graded.

Three more orderings are pinned: a method the route does not allow is 405 rather
than 401, so method matching also precedes the check; a request needing a
redirect gets the redirect rather than a challenge, so cleaning precedes it; and
`OPTIONS /upload` answers without a token at all.
"""
from __future__ import annotations

import pytest
import srbcheck
import srbfixtures

CASES = srbfixtures.cases_in("auth")


@pytest.mark.parametrize("case_id", CASES)
class TestRecordedAuth:
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


def test_a_read_only_token_never_wrote_anything(pairs):
    """The read-only token's two rejections, read together.

    Stated once here as well as case by case above, because it is the one property
    of this module a reader would want confirmed as a property rather than as two
    unrelated cases: a token in the read-only set is refused for both write verbs,
    and both refusals are the recorded refusal rather than merely non-2xx.  A
    submission that answered 500 to both would pass a "did not succeed" check.
    """
    for cid in ("auth-ro-cannot-write", "auth-ro-cannot-post"):
        p = pairs.get(cid)
        if p is None or p.actual is None:
            pytest.skip(f"{cid} was not measured; the per-case tests report why")
        assert p.actual["status"] == p.expected["status"], (
            f"a read-only token was answered {p.actual['status']} where "
            f"{p.expected['status']} was recorded\n{p.render()}")
        assert p.actual_body == p.expected_body, (
            f"the refusal body for a read-only token differs\n{p.render()}")
