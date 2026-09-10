"""What the file server negotiates: ranges, conditionals, and CORS or its absence.

Almost every header in this module is written by `http.ServeContent` rather than by
this repository, which is exactly why it is graded.  A port that stops handing the
file to `ServeContent` -- writing the bytes itself, or reaching for `ServeFile`, or
sniffing the type in a wrapper -- keeps every status in the corpus and changes the
headers: `Accept-Ranges` disappears, a 206 becomes a 200 with the whole file, the
`Content-Range` on an unsatisfiable range goes missing, a conditional 304 becomes a
second full body.

The three `nocors` cases are here rather than in `routing` because CORS is a header
question: with the flag off the four `Access-Control-*` headers are absent, and the
interesting one is `nocors-404`, where the 404 body is identical to the CORS run's
and the header set is not.

Header *order* is graded, and this is the module where it earns its keep.  On a
range response net/http writes `Content-Range` before `Content-Length`; a
submission that sets its own `Content-Length` first produces both headers with both
recorded values in the other order.
"""
from __future__ import annotations

import pytest
import srbcheck
import srbfixtures

CASES = srbfixtures.cases_in("headers")

#: What `-enable_cors` actually governs, which is one header and not a family.
#: Named here because the assertion below is about an absence, and an absence
#: cannot be read off a recording that does not list what was looked for.
#:
#: `Access-Control-Allow-Methods` is deliberately NOT in this tuple, and that is
#: the whole subtlety of the flag.  The OPTIONS handler writes it unconditionally,
#: outside the `if s.EnableCORS` branch, so State A answers `nocors-options` with
#: 204 and that header present -- measured in the recording, not inferred.  A
#: tuple listing all four `Access-Control-*` headers asserts something State A
#: itself violates, which fails every correct submission on the one case that
#: proves the asymmetry was understood.  A hand-written rule that duplicates the
#: recording has to agree with it; where they disagree the recording is right.
CORS_HEADERS = ("access-control-allow-origin",)


@pytest.mark.parametrize("case_id", CASES)
class TestRecordedHeaders:
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


@pytest.mark.parametrize("case_id", [c for c in CASES if c.startswith("nocors-")])
def test_the_origin_header_is_absent_when_the_flag_is_off(pair, case_id):
    """No `Access-Control-Allow-Origin` under `-enable_cors=false`.

    A positive statement of something the recording only says by omission.  The
    per-case header comparison above already fails if this header appears, but it
    fails as "the header order differs" with a list to read; this fails naming the
    header and the flag, which is the sentence the reader needs.  It cannot pass
    where that one fails -- it is the same records, asked a narrower question,
    which is only true because the header it asks about is one the flag governs.
    """
    present = [h for h in CORS_HEADERS if pair.header(h)]
    assert not present, (
        f"-enable_cors=false and the response still carries {present}\n"
        f"{pair.render()}")
