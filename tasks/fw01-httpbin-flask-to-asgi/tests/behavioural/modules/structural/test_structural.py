"""Bodies that cannot be compared byte-for-byte, checked by structure instead.

The corpus cases carrying ``body_mode="ignore"`` have their status and headers
compared against State A by the corpus tests; what lands here is the body, for
one of three reasons:

*   it is genuinely per-request (``/uuid``, digest nonces);
*   it is a framework-rendered error page whose wording instruction.md does not
    put under contract (the 404/405/500 HTML);
*   it is large and structured, so a structural assertion says far more about
    correctness than a byte diff (``/spec.json``, the Swagger UI page).

Leaving those bodies entirely unchecked would let a submission return an empty
200 for ``/spec.json``. So each one gets an explicit test for the properties
that actually matter.
"""

from __future__ import annotations

import json
import re
import uuid as uuidmod

import pytest

from harness import normalize

pytestmark = pytest.mark.behaviour


def body_of(replay, case_id) -> bytes:
    if case_id in replay["errors"]:
        pytest.fail(f"request could not be completed: {replay['errors'][case_id]}")
    return normalize.decode_body(replay["responses"][case_id]["body"])


def text_of(replay, case_id) -> str:
    return body_of(replay, case_id).decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# Framework error pages
# ---------------------------------------------------------------------------

ERROR_PAGE_CASES = [
    ("nf-root-unknown", 404, "Not Found"),
    ("nf-deep", 404, "Not Found"),
    ("nf-trailing", 404, "Not Found"),
    ("nf-case", 404, "Not Found"),
    ("edge-query-on-404", 404, "Not Found"),
    ("edge-long-path", 404, "Not Found"),
    ("cors-404-origin", 404, "Not Found"),
    ("cookies-set-empty-value", 404, "Not Found"),
    ("redirect-negative", 404, "Not Found"),
    ("get-on-post", 405, "Method Not Allowed"),
    ("post-on-get", 405, "Method Not Allowed"),
    ("put-on-delete", 405, "Method Not Allowed"),
    ("patch-on-put", 405, "Method Not Allowed"),
    ("delete-on-patch", 405, "Method Not Allowed"),
    ("delay-notnum", 500, "Internal Server Error"),
]


@pytest.mark.parametrize("case_id,status,phrase", ERROR_PAGE_CASES,
                         ids=[c[0] for c in ERROR_PAGE_CASES])
def test_error_page_is_html_and_names_the_error(case_id, status, phrase, replay):
    """Error pages must stay recognisable HTML naming the right condition.

    The exact wording is out of contract (instruction.md says so), but an error
    page that is blank, or that leaks a traceback, is not equivalent output.
    """
    text = text_of(replay, case_id)
    assert text.strip(), f"{case_id}: {status} body is empty"
    low = text.lower()
    assert "<html" in low or "<!doctype" in low, (
        f"{case_id}: {status} response is not an HTML page: {text[:200]!r}"
    )
    assert str(status) in text or phrase.lower() in low, (
        f"{case_id}: the {status} page does not mention the error: {text[:200]!r}"
    )
    for leak in ("Traceback (most recent call last)", 'File "/', "__pycache__"):
        assert leak not in text, (
            f"{case_id}: the {status} page leaks internals ({leak!r}); "
            f"debug mode must stay off"
        )


# ---------------------------------------------------------------------------
# Plain-text guard rails
# ---------------------------------------------------------------------------

TEXT_ERROR_CASES = [
    ("status-bad-notint", "Invalid status code"),
    ("status-bad-empty-choice", "Invalid status code"),
    ("status-bad-weight", "Invalid status code"),
    ("drip-zero-bytes", "number of bytes must be positive"),
    ("drip-negative", "number of bytes must be positive"),
    ("range-zero-size", "number of bytes must be in the range (0, 102400]"),
    ("range-oversize", "number of bytes must be in the range (0, 102400]"),
]


@pytest.mark.parametrize("case_id,message", TEXT_ERROR_CASES,
                         ids=[c[0] for c in TEXT_ERROR_CASES])
def test_route_guard_message(case_id, message, replay, golden):
    """These messages come from httpbin's own code, so they are contractual."""
    got = text_of(replay, case_id)
    want = normalize.decode_body(golden[case_id]["body"]).decode("utf-8")
    assert got == want, (
        f"{case_id}: expected the route's own guard message "
        f"{want!r}, got {got!r}"
    )
    assert message in got


# ---------------------------------------------------------------------------
# /uuid
# ---------------------------------------------------------------------------

def test_uuid_is_a_fresh_random_uuid4(replay, golden, http, base_url):
    raw = body_of(replay, "uuid")
    assert not normalize.json_style_violations(raw), (
        f"/uuid must keep the pretty-printed JSON shape: "
        f"{normalize.json_style_violations(raw)}"
    )
    data = json.loads(raw)
    assert list(data) == ["uuid"], f"/uuid payload keys changed: {list(data)}"
    parsed = uuidmod.UUID(data["uuid"])
    assert parsed.version == 4, f"/uuid returned a v{parsed.version} UUID"
    assert str(parsed) == data["uuid"], "expected the canonical hyphenated form"

    # A hardcoded constant would satisfy everything above.
    seen = {data["uuid"]}
    for _ in range(4):
        seen.add(http.get(base_url + "/uuid").json()["uuid"])
    assert len(seen) == 5, f"/uuid is not random: {seen}"


# ---------------------------------------------------------------------------
# Digest challenges
# ---------------------------------------------------------------------------

DIGEST_CHALLENGES = [
    ("digest-challenge-auth", {"qop": "auth", "algorithm": None}),
    ("digest-challenge-auth-int", {"qop": "auth-int", "algorithm": None}),
    ("digest-challenge-MD5", {"qop": "auth", "algorithm": "MD5"}),
    ("digest-challenge-SHA-256", {"qop": "auth", "algorithm": "SHA-256"}),
    ("digest-challenge-SHA-512", {"qop": "auth", "algorithm": "SHA-512"}),
    ("digest-challenge-badalgo", {"qop": "auth", "algorithm": "MD5"}),
    ("digest-challenge-never-stale", {"qop": "auth", "algorithm": "MD5"}),
]

_DIGEST_PARAM = re.compile(r'(\w+)=(?:"([^"]*)"|([^,\s]+))')


def _parse_challenge(value: str) -> dict:
    assert value.startswith("Digest "), f"not a Digest challenge: {value!r}"
    return {
        m.group(1): m.group(2) if m.group(2) is not None else m.group(3)
        for m in _DIGEST_PARAM.finditer(value[len("Digest "):])
    }


@pytest.mark.parametrize("case_id,expected", DIGEST_CHALLENGES,
                         ids=[c[0] for c in DIGEST_CHALLENGES])
def test_digest_challenge_parameters(case_id, expected, replay, golden):
    """The nonce and opaque are random, but every other parameter is fixed."""
    got_hdr = replay["responses"][case_id]["headers"].get("www-authenticate")
    assert got_hdr, f"{case_id}: no WWW-Authenticate header"
    got = _parse_challenge(got_hdr[0])
    want = _parse_challenge(golden[case_id]["headers"]["www-authenticate"][0])

    for key in sorted(set(got) | set(want)):
        if key in ("nonce", "opaque"):
            continue
        assert got.get(key) == want.get(key), (
            f"{case_id}: challenge parameter {key} is {got.get(key)!r}, "
            f"State A sent {want.get(key)!r}\n  got:     {got_hdr[0]}\n"
            f"  State A: {golden[case_id]['headers']['www-authenticate'][0]}"
        )
    if expected["algorithm"]:
        assert got.get("algorithm") == expected["algorithm"]
    assert got.get("qop") == expected["qop"]

    # State A hashes the nonce and the opaque with the *requested* algorithm, so
    # their width follows it: 32 hex digits for MD5, 64 for SHA-256, 128 for
    # SHA-512. A port that hardcodes md5 here still authenticates correctly --
    # nothing checks the nonce's construction -- but stops telling the truth
    # about the algorithm it advertises.
    hex_width = {"MD5": 32, "SHA-256": 64, "SHA-512": 128}[
        got.get("algorithm") or "MD5"
    ]
    for key in ("nonce", "opaque"):
        assert re.fullmatch(r"[0-9a-f]{%d}" % hex_width, got.get(key, "")), (
            f"{case_id}: {key}={got.get(key)!r} is not a {hex_width}-hex-digit "
            f"token, which is what algorithm={got.get('algorithm')!r} implies"
        )


def test_digest_challenge_badqop_advertises_both_qops(replay, golden):
    """An unrecognised qop is not passed through, and not dropped either.

    State A normalises anything that is not ``auth``/``auth-int`` to ``None``,
    and a ``None`` qop makes the challenge advertise the full set --
    ``qop="auth, auth-int"``. Echoing the client's bogus token back, or omitting
    the parameter altogether, are both changes of contract.
    """
    got = _parse_challenge(
        replay["responses"]["digest-challenge-badqop"]["headers"]
        ["www-authenticate"][0]
    )
    assert got.get("qop") == "auth, auth-int", (
        f"an unrecognised qop must widen the challenge to both values, "
        f"got qop={got.get('qop')!r} in {got}"
    )


def test_digest_nonce_is_fresh_per_request(http, base_url):
    nonces = set()
    for _ in range(4):
        r = http.get(base_url + "/digest-auth/auth/user/passwd")
        nonces.add(_parse_challenge(r.headers["www-authenticate"])["nonce"])
    assert len(nonces) == 4, f"the digest nonce is being reused: {nonces}"


# ---------------------------------------------------------------------------
# Redirect edge case
# ---------------------------------------------------------------------------

def test_redirect_to_without_url(replay, golden):
    """``/redirect-to`` with no url: State A redirects to the literal "None".

    An odd behaviour, and exactly the kind of thing a rewrite quietly changes.
    It is observable, so it is contractual.
    """
    got = replay["responses"]["redirect-to-missing-url"]
    want = golden["redirect-to-missing-url"]
    assert got["status"] == want["status"]
    assert got["headers"].get("location") == want["headers"].get("location"), (
        f"Location is {got['headers'].get('location')!r}, "
        f"State A sent {want['headers'].get('location')!r}"
    )


# ---------------------------------------------------------------------------
# The favicon, served from the static directory
# ---------------------------------------------------------------------------

def test_favicon_bytes_match_the_reference(replay, golden):
    """The static mount must serve the real icon, not a stand-in.

    Compared against what State A served, not against the file in the submitted
    tree.  Reading the tree would ask a question one step off the one that matters:
    it would pass a submission that shipped a 40-byte placeholder and served it
    faithfully, and it would need the submission's own directory layout to be what
    it expected.  The bytes State A put on the wire are the contract; where a
    submission keeps them is its business.
    """
    got = body_of(replay, "favicon")
    want = normalize.decode_body(golden["favicon"]["body"])
    assert got == want, (
        f"/static/favicon.ico served {len(got)}B, State A served {len(want)}B"
    )


def test_favicon_has_a_cache_validator(replay):
    """send_file's ETag/Last-Modified values are not reproducible, but a static
    file handler is still expected to offer some validator."""
    headers = replay["responses"]["favicon"]["raw_headers"]
    names = {n for n, _ in headers}
    assert names & {"etag", "last-modified"}, (
        f"/static/favicon.ico offers no cache validator; headers: {sorted(names)}"
    )


# ---------------------------------------------------------------------------
# OPTIONS preflight
# ---------------------------------------------------------------------------

OPTIONS_CASES = ["options-get", "options-post", "options-anything",
                 "options-with-origin", "options-request-headers"]


@pytest.mark.parametrize("case_id", OPTIONS_CASES)
def test_options_body_is_empty(case_id, replay):
    body = body_of(replay, case_id)
    assert body == b"", f"{case_id}: OPTIONS returned a {len(body)}B body"


def test_options_allow_lists_only_real_methods(replay, golden):
    """``Allow`` is built from a set upstream, so members are compared, not order."""
    for case_id in OPTIONS_CASES:
        got = replay["responses"][case_id]["headers"].get("allow")
        want = golden[case_id]["headers"].get("allow")
        assert got == want, f"{case_id}: Allow is {got!r}, State A sent {want!r}"


def test_multipart_without_boundary(replay, golden):
    """No boundary parameter: the form stays empty rather than erroring."""
    got = json.loads(body_of(replay, "mp-no-boundary"))
    want = json.loads(normalize.decode_body(golden["mp-no-boundary"]["body"]))
    for key in ("form", "files", "data", "args"):
        assert got.get(key) == want.get(key), (
            f"multipart with no boundary: {key} is {got.get(key)!r}, "
            f"State A produced {want.get(key)!r}"
        )
