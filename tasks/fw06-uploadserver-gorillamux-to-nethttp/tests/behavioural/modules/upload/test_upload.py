"""What a write returns: the status, and the path it says it wrote.

Every case here answers with a JSON body naming a path, and that path is the part a
port gets subtly wrong.  `POST /upload` derives the stored name from the multipart
part; `PUT /files/x.txt` derives it from the target; both answer
`{"ok":true,"path":"/files/..."}` and the two derivations meet different rules.  A
submission that builds the answer from the request target where State A built it
from the cleaned filename returns a path that differs from the recording by a
`%2F` or a leading slash while every status matches.

Three cases are refusals rather than writes, and they are the ones a port is most
likely to turn into a different refusal: a multipart body with no file part, a file
part under the wrong field name, and a `PUT` to `/files` itself with no filename.
Each has a recorded status and a recorded body, and "some 4xx" is not what is being
graded.

`put-encoded-slash` is the write-side half of the routing module's `encoded-slash`.
The target contains `%2F`, and whether that becomes a directory separator or a
literal character in the stored filename decides both the path in the answer and
where the bytes land.

Overwrite is pinned twice, with `?overwrite=true` and with `?overwrite=1`, because
the flag is parsed by a boolish helper that accepts both -- and a port that reaches
for `strconv.ParseBool` accepts both as well but disagrees about `""`.
"""
from __future__ import annotations

import json

import pytest
import srbcheck
import srbfixtures

CASES = srbfixtures.cases_in("upload")


@pytest.mark.parametrize("case_id", CASES)
class TestRecordedUploads:
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


@pytest.mark.parametrize("case_id", CASES)
def test_the_answer_is_the_shape_a_client_parses(pair, case_id):
    """The response body parses as this server's result object.

    The byte comparison above is stricter than this and would fail first on any
    difference, so this can never fail alone -- what it adds is the sentence in the
    report.  A client of this API reads `ok` and `path`, or `ok` and `error`; a
    submission that answers with a bare string, an array, or a body wrapped in
    `{"data":...}` has broken every client, and that reads better as "the answer is
    no longer the documented shape" than as a byte offset.

    Only the *shape* is asserted here, and only when the recording had that shape:
    the values are the byte comparison's business.
    """
    try:
        want = json.loads(pair.expected_body)
    except ValueError:
        pytest.skip("this case's recorded body is not JSON; the byte check owns it")
    if not isinstance(want, dict) or "ok" not in want:
        pytest.skip("this case's recorded body is not a result object")

    try:
        got = json.loads(pair.actual_body)
    except ValueError:
        pytest.fail(f"the answer is not JSON: {pair.actual_body[:200]!r}\n"
                    f"{pair.render()}")
    assert isinstance(got, dict), (
        f"the answer is a {type(got).__name__}, not an object\n{pair.render()}")

    keys_wanted = set(want)
    missing = sorted(keys_wanted - set(got))
    assert not missing, (
        f"the answer has no {missing} -- a client reading this API reads "
        f"{sorted(keys_wanted)}\n{pair.render()}")
    assert isinstance(got["ok"], bool), (
        f"`ok` is {got['ok']!r}, not a boolean\n{pair.render()}")
