"""The bytes a GET returns, and the type the server decided they were.

Ten reads of the fixture document root, each chosen for something the wire can
disagree about while the status stays 200.  The types come from net/http's own
sniffing: `README` has no extension and comes back as sniffed text, `tiny.png` as
`image/png`, `big.bin` as the fallback -- and a port that sets `Content-Type`
itself, or that opens the file and writes it rather than handing it to
`ServeContent`, changes those without changing anything a status check sees.

The three name cases are the ones a routing change is most likely to break while
leaving ordinary paths working.  `sp%20ace.txt` needs the target unescaped before it
becomes a filename; `odd+name.txt` needs the `+` left alone, because a `+` is a
space in a query string and a literal in a path; `odd%2Bname.txt` is the same file
reached the other way.  A submission that unescapes with the wrong function, or
twice, returns 404 for one of the three and 200 for the others.

Bodies are compared byte for byte, including the non-text fixtures, so a port that
truncates a read or writes an extra newline is caught by size before anyone has to
look at content.
"""
from __future__ import annotations

import os
import urllib.parse
from pathlib import Path

import pytest
import srbcheck
import srbfixtures

CASES = srbfixtures.cases_in("body")

#: The document root the `default` profile was launched against -- the verifier's
#: own directory, not anything in the submitted tree.
FIXTURE_ROOT = Path(os.environ.get("SRB_FIXTURE_ROOT", "/opt/fixtures/docroot"))


@pytest.mark.parametrize("case_id", CASES)
class TestRecordedBodies:
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
def test_the_body_is_the_file_on_disk(pair, case_id):
    """The returned bytes against the fixture file itself, not against the recording.

    Every other check in this suite compares two recordings, which means all of them
    share one assumption: that the recording is right.  This is the one check that
    does not.  It reads the file out of the verifier's own document root -- the
    directory the server was launched against -- and compares the response to it.

    Doing that requires deciding which file the target named, and that decision is
    the interesting part: `/files/sp%20ace.txt` is the file with a space in its
    name, `/files/odd+name.txt` is the file with a literal plus, and
    `/files/odd%2Bname.txt` is that same file reached the other way.  A port that
    unescapes with the wrong function, or twice, returns some other file's bytes or
    none -- and here that reads as "served the wrong file" rather than as a body
    diff against a recording.
    """
    rel = urllib.parse.unquote(pair.target.split("?", 1)[0])
    assert rel.startswith("/files/"), (
        f"this test assumes a /files/ read; {pair.target} is not one")
    on_disk = FIXTURE_ROOT / rel[len("/files/"):]
    if not on_disk.is_file():
        pytest.fail(f"the fixture root has no {on_disk} -- the corpus and the "
                    f"verifier's document root disagree, which is a verifier bug")
    want = on_disk.read_bytes()
    got = pair.actual_body
    assert got == want, (
        f"served {len(got)} byte(s) where {on_disk.name} holds {len(want)}; the "
        f"response is not this file's contents\n{pair.render()}")
