"""Multi-request flows: end-to-end conversations, not single round trips.

Everything here needs state carried across requests, so no captured response can
express it:

*   the digest handshake, computed client-side from the server's own challenge --
    which means the server's nonce, HA1/HA2 construction and response hash all
    have to agree with RFC 2617 for the second request to succeed;
*   the ``stale_after`` cookie countdown and nonce-replay detection;
*   redirect chains followed to their end;
*   cookies set on one request and echoed on the next.

These are also the tests that a hardcoded response table cannot satisfy: the
digest response hash depends on a nonce the server invented moments earlier.
"""

from __future__ import annotations

import hashlib
import os
import re

import pytest

from harness import client as hclient

pytestmark = pytest.mark.behaviour

_PARAM = re.compile(r'(\w+)=(?:"([^"]*)"|([^,\s]+))')


def parse_challenge(value: str) -> dict:
    assert value.lower().startswith("digest "), f"not Digest: {value!r}"
    return {
        m.group(1): m.group(2) if m.group(2) is not None else m.group(3)
        for m in _PARAM.finditer(value[7:])
    }


def _H(data: bytes, algorithm: str | None) -> str:
    if algorithm == "SHA-256":
        return hashlib.sha256(data).hexdigest()
    if algorithm == "SHA-512":
        return hashlib.sha512(data).hexdigest()
    return hashlib.md5(data).hexdigest()


def digest_header(challenge, *, user, passwd, method, uri, body=b"", nc="00000001",
                  cnonce=None, qop_choice=None):
    """Build a client-side Authorization header for a digest challenge.

    ``qop`` in a challenge is a *list* -- State A sends ``qop="auth, auth-int"``
    whenever the route's qop is unset or unrecognised. RFC 2617 says the client
    picks one member and names only that one in its response, so that is what
    this does; ``qop_choice`` overrides the pick.
    """
    algorithm = challenge.get("algorithm")
    realm = challenge.get("realm", "")
    advertised = [q.strip() for q in (challenge.get("qop") or "").split(",")
                  if q.strip()]
    qop = qop_choice or (advertised[0] if advertised else None)
    cnonce = cnonce or os.urandom(8).hex()

    ha1 = _H(f"{user}:{realm}:{passwd}".encode(), algorithm)
    if qop == "auth-int":
        ha2 = _H(
            f"{method}:{uri}:{_H(body, algorithm)}".encode(), algorithm
        )
    else:
        ha2 = _H(f"{method}:{uri}".encode(), algorithm)

    if qop in ("auth", "auth-int"):
        resp = _H(
            f"{ha1}:{challenge['nonce']}:{nc}:{cnonce}:{qop}:{ha2}".encode(),
            algorithm,
        )
    else:
        resp = _H(f"{ha1}:{challenge['nonce']}:{ha2}".encode(), algorithm)

    parts = [
        f'username="{user}"',
        f'realm="{realm}"',
        f'nonce="{challenge["nonce"]}"',
        f'uri="{uri}"',
        f'response="{resp}"',
    ]
    if "opaque" in challenge:
        parts.append(f'opaque="{challenge["opaque"]}"')
    if algorithm:
        parts.append(f"algorithm={algorithm}")
    if qop:
        parts.extend([f"qop={qop}", f"nc={nc}", f'cnonce="{cnonce}"'])
    return "Digest " + ", ".join(parts)


# ---------------------------------------------------------------------------
# The digest handshake, for every advertised variant
# ---------------------------------------------------------------------------

DIGEST_VARIANTS = [
    ("auth", "MD5", "/digest-auth/auth/user/passwd/MD5"),
    ("auth", "SHA-256", "/digest-auth/auth/user/passwd/SHA-256"),
    ("auth", "SHA-512", "/digest-auth/auth/user/passwd/SHA-512"),
    ("auth-int", "MD5", "/digest-auth/auth-int/user/passwd/MD5"),
    ("auth-int", "SHA-256", "/digest-auth/auth-int/user/passwd/SHA-256"),
    ("auth-int", "SHA-512", "/digest-auth/auth-int/user/passwd/SHA-512"),
    ("auth", None, "/digest-auth/auth/user/passwd"),
    (None, "MD5", "/digest-auth/bogus/user/passwd/MD5"),
]


@pytest.mark.parametrize("qop,algorithm,path", DIGEST_VARIANTS,
                         ids=[f"{q}-{a}" for q, a, _ in DIGEST_VARIANTS])
def test_digest_handshake_succeeds(qop, algorithm, path, base_url):
    """Challenge, then answer it correctly, and expect a 200.

    This only passes if the server's own hash computation matches RFC 2617 with
    the nonce it just issued -- it cannot be faked with a lookup table.

    Cookies are left to the client jar: the challenge sets ``stale_after`` and
    ``fake``, and re-sending them by hand would put ``stale_after`` in the
    ``Cookie`` header twice.
    """
    with hclient.make_client() as c:
        first = c.get(base_url + path)
        assert first.status_code == 401, (
            f"{path} should challenge with 401, got {first.status_code}"
        )
        challenge = parse_challenge(first.headers["www-authenticate"])

        auth = digest_header(
            challenge, user="user", passwd="passwd", method="GET", uri=path
        )
        second = c.get(base_url + path, headers={"Authorization": auth})
    assert second.status_code == 200, (
        f"{path}: a correctly computed digest response was rejected "
        f"({second.status_code}). challenge={challenge}\nbody={second.text[:300]}"
    )
    assert second.json() == {"authenticated": True, "user": "user"}


@pytest.mark.parametrize("choice", ["auth", "auth-int"])
def test_digest_accepts_either_qop_the_challenge_offered(choice, base_url):
    """When the challenge offers both qops, either one must authenticate.

    ``/digest-auth/bogus/...`` normalises its qop to ``None``, which makes the
    challenge advertise ``auth, auth-int``. The response hash is computed
    differently for each -- ``auth-int`` folds the entity body in -- so a server
    that only implements one of the two branches fails half of this.
    """
    path = "/digest-auth/bogus/user/passwd/MD5"
    with hclient.make_client() as c:
        first = c.get(base_url + path)
        challenge = parse_challenge(first.headers["www-authenticate"])
        assert challenge.get("qop") == "auth, auth-int", (
            f"expected both qops on offer, got {challenge.get('qop')!r}"
        )
        auth = digest_header(challenge, user="user", passwd="passwd",
                             method="GET", uri=path, qop_choice=choice)
        r = c.get(base_url + path, headers={"Authorization": auth})
    assert r.status_code == 200, (
        f"qop={choice} was offered but a correct response using it was refused "
        f"({r.status_code}): {r.text[:200]}"
    )


@pytest.mark.parametrize("path", [p for _, _, p in DIGEST_VARIANTS])
def test_digest_wrong_password_is_rejected(path, base_url):
    with hclient.make_client() as c:
        first = c.get(base_url + path)
        challenge = parse_challenge(first.headers["www-authenticate"])
        auth = digest_header(
            challenge, user="user", passwd="WRONG", method="GET", uri=path
        )
        second = c.get(base_url + path, headers={"Authorization": auth})
    assert second.status_code == 401, (
        f"{path} accepted a digest response computed with the wrong password "
        f"({second.status_code})"
    )


@pytest.mark.parametrize("path", [p for _, _, p in DIGEST_VARIANTS])
def test_digest_rejects_a_response_for_a_different_nonce(path, base_url):
    """A hash that is right for another nonce must not authenticate.

    Catches a server that compares anything other than the full digest -- for
    instance one that checks the username and shrugs at the rest.
    """
    with hclient.make_client() as c:
        first = c.get(base_url + path)
        challenge = parse_challenge(first.headers["www-authenticate"])
        forged = dict(challenge, nonce="0" * len(challenge["nonce"]))
        auth = digest_header(forged, user="user", passwd="passwd",
                             method="GET", uri=path)
        # ...but present it under the nonce the server actually issued.
        auth = auth.replace(f'nonce="{forged["nonce"]}"',
                            f'nonce="{challenge["nonce"]}"')
        r = c.get(base_url + path, headers={"Authorization": auth})
    assert r.status_code == 401, (
        f"{path} accepted a digest response hashed over a different nonce "
        f"({r.status_code})"
    )


@pytest.mark.parametrize("algorithm", ["MD5", "SHA-256", "SHA-512"])
def test_digest_auth_int_covers_the_body(algorithm, base_url):
    """With qop=auth-int the body is part of the hash, so tampering must fail.

    The digest routes are GET-only (State A answers ``POST`` with 405), so the
    body travels on a GET -- unusual, but it is the only way to exercise the
    ``auth-int`` branch through this API, and httpbin's own hash covers
    ``request.data`` regardless of method.
    """
    path = f"/digest-auth/auth-int/user/passwd/{algorithm}"
    with hclient.make_client() as c:
        first = c.get(base_url + path)
        challenge = parse_challenge(first.headers["www-authenticate"])
        auth = digest_header(challenge, user="user", passwd="passwd",
                             method="GET", uri=path, body=b"payload")
        ok = c.request("GET", base_url + path, headers={"Authorization": auth},
                       content=b"payload")
    assert ok.status_code == 200, (
        f"a correct qop=auth-int response over the body it hashed was refused "
        f"({ok.status_code}): {ok.text[:200]}"
    )

    with hclient.make_client() as c:
        first = c.get(base_url + path)
        challenge = parse_challenge(first.headers["www-authenticate"])
        # Hash computed over b"payload" but a different body is sent.
        auth = digest_header(challenge, user="user", passwd="passwd",
                             method="GET", uri=path, body=b"payload")
        bad = c.request("GET", base_url + path, headers={"Authorization": auth},
                        content=b"tampered")
    assert bad.status_code == 401, (
        "qop=auth-int must hash the entity body: a request whose body does not "
        f"match the hash was accepted with {bad.status_code}"
    )


def test_digest_post_is_not_allowed(base_url):
    """The digest routes are GET-only; State A answers POST with 405.

    Worth pinning because a Starlette port declares its methods explicitly, and
    ``methods=["GET", "POST"]`` copied from a neighbouring route would widen the
    surface silently.
    """
    with hclient.make_client() as c:
        r = c.post(base_url + "/digest-auth/auth/user/passwd/MD5")
    assert r.status_code == 405, (
        f"POST to a digest route should be 405, got {r.status_code}"
    )


def test_digest_nonce_replay_is_stale(base_url):
    """Reusing a nonce the server has already seen triggers a stale challenge.

    The whole mechanism lives in the ``last_nonce`` cookie, and the server only
    writes that cookie on a *rejection*. So this drives it the way a real client
    would arrive there: fail once with the wrong password (which records the
    nonce), then present a perfectly correct response for that same nonce and
    watch it be refused as stale.
    """
    path = "/digest-auth/auth/user/passwd/MD5"
    with hclient.make_client() as c:
        first = c.get(base_url + path)
        challenge = parse_challenge(first.headers["www-authenticate"])

        wrong = digest_header(challenge, user="user", passwd="WRONG",
                              method="GET", uri=path)
        rejected = c.get(base_url + path, headers={"Authorization": wrong})
        assert rejected.status_code == 401
        assert c.cookies.get("last_nonce") == challenge["nonce"], (
            f"a rejected digest response must record the nonce it used in the "
            f"last_nonce cookie; got {c.cookies.get('last_nonce')!r} for nonce "
            f"{challenge['nonce']!r}"
        )

        # Correct credentials this time, but the nonce has been burned.
        auth = digest_header(challenge, user="user", passwd="passwd",
                             method="GET", uri=path)
        replay = c.get(base_url + path, headers={"Authorization": auth})
    assert replay.status_code == 401, (
        f"replaying a nonce already recorded in last_nonce should be refused, "
        f"got {replay.status_code}"
    )
    assert "stale=True" in replay.headers.get("www-authenticate", ""), (
        f"the re-challenge should be marked stale: "
        f"{replay.headers.get('www-authenticate')!r}"
    )


def test_digest_fresh_nonce_recovers_after_a_stale_challenge(base_url):
    """The stale challenge is usable: its new nonce authenticates."""
    path = "/digest-auth/auth/user/passwd/MD5"
    with hclient.make_client() as c:
        first = c.get(base_url + path)
        ch1 = parse_challenge(first.headers["www-authenticate"])
        c.get(base_url + path, headers={"Authorization": digest_header(
            ch1, user="user", passwd="WRONG", method="GET", uri=path)})
        stale = c.get(base_url + path, headers={"Authorization": digest_header(
            ch1, user="user", passwd="passwd", method="GET", uri=path)})
        assert stale.status_code == 401
        ch2 = parse_challenge(stale.headers["www-authenticate"])
        assert ch2["nonce"] != ch1["nonce"], "the stale challenge reused its nonce"
        ok = c.get(base_url + path, headers={"Authorization": digest_header(
            ch2, user="user", passwd="passwd", method="GET", uri=path)})
    assert ok.status_code == 200, (
        f"the nonce from the stale challenge should authenticate, got "
        f"{ok.status_code}: {ok.text[:200]}"
    )


def test_digest_stale_after_zero_forces_a_challenge(base_url):
    """``stale_after=0`` in the route makes the very next attempt stale.

    The route sets the cookie itself, so no cookie has to be forged: State A
    puts ``stale_after=0`` on the challenge and then refuses the answer to it.
    """
    path = "/digest-auth/auth/user/passwd/MD5/0"
    with hclient.make_client() as c:
        first = c.get(base_url + path)
        assert first.status_code == 401
        assert first.cookies.get("stale_after") == "0", (
            f"the challenge should carry stale_after=0, got "
            f"{first.cookies.get('stale_after')!r}"
        )
        challenge = parse_challenge(first.headers["www-authenticate"])
        auth = digest_header(challenge, user="user", passwd="passwd",
                             method="GET", uri=path)
        r = c.get(base_url + path, headers={"Authorization": auth})
    assert r.status_code == 401, (
        f"stale_after=0 must refuse even a correct response, got "
        f"{r.status_code}"
    )
    assert "stale=True" in r.headers.get("www-authenticate", ""), (
        f"the refusal should be a stale challenge: "
        f"{r.headers.get('www-authenticate')!r}"
    )


@pytest.mark.parametrize("start", ["1", "2", "3"])
def test_digest_stale_after_counts_down(start, base_url):
    """A numeric stale_after cookie is decremented on each success."""
    path = f"/digest-auth/auth/user/passwd/MD5/{start}"
    with hclient.make_client() as c:
        first = c.get(base_url + path)
        assert first.cookies.get("stale_after") == start, (
            f"the challenge should set stale_after={start}, got "
            f"{first.cookies.get('stale_after')!r}"
        )
        challenge = parse_challenge(first.headers["www-authenticate"])
        auth = digest_header(challenge, user="user", passwd="passwd",
                             method="GET", uri=path)
        ok = c.get(base_url + path, headers={"Authorization": auth})
        assert ok.status_code == 200, (
            f"the first authenticated request should succeed, got "
            f"{ok.status_code}: {ok.text[:200]}"
        )
        assert c.cookies.get("stale_after") == str(int(start) - 1), (
            f"stale_after should count down to {int(start) - 1}, got "
            f"{c.cookies.get('stale_after')!r}"
        )


def test_digest_stale_after_expires_at_zero(base_url):
    """Counting down to zero makes the following request stale.

    Two round trips on ``/1``: the first succeeds and leaves the cookie at 0,
    and a fresh nonce is then refused because the countdown, not the nonce, has
    run out.
    """
    path = "/digest-auth/auth/user/passwd/MD5/1"
    with hclient.make_client() as c:
        first = c.get(base_url + path)
        ch1 = parse_challenge(first.headers["www-authenticate"])
        ok = c.get(base_url + path, headers={"Authorization": digest_header(
            ch1, user="user", passwd="passwd", method="GET", uri=path)})
        assert ok.status_code == 200
        assert c.cookies.get("stale_after") == "0"
        expired = c.get(base_url + path, headers={"Authorization": digest_header(
            ch1, user="user", passwd="passwd", method="GET", uri=path)})
    assert expired.status_code == 401, (
        f"once stale_after reaches 0 the next request must be refused, got "
        f"{expired.status_code}"
    )
    assert "stale=True" in expired.headers.get("www-authenticate", "")
    assert c.cookies.get("stale_after") == "1", (
        f"the stale challenge should reset stale_after to the route's value, "
        f"got {c.cookies.get('stale_after')!r}"
    )


def test_digest_never_stale_survives_repeated_use(base_url):
    """``stale_after=never`` (the default) never expires by countdown."""
    path = "/digest-auth/auth/user/passwd/MD5"
    with hclient.make_client() as c:
        for attempt in range(4):
            first = c.get(base_url + path)
            challenge = parse_challenge(first.headers["www-authenticate"])
            auth = digest_header(challenge, user="user", passwd="passwd",
                                 method="GET", uri=path)
            r = c.get(base_url + path, headers={"Authorization": auth})
            assert r.status_code == 200, (
                f"attempt {attempt} with a fresh nonce was refused "
                f"({r.status_code}) although stale_after is 'never'"
            )
        assert c.cookies.get("stale_after") == "never", (
            f"stale_after should stay 'never', got "
            f"{c.cookies.get('stale_after')!r}"
        )


def test_digest_challenge_sets_its_cookies(base_url):
    """The challenge carries the two cookies the mechanism depends on.

    ``fake=fake_value`` is what ``require-cookie`` looks for and ``stale_after``
    is the countdown; both are part of the observable contract because the next
    request's outcome is decided by them.
    """
    with hclient.make_client() as c:
        r = c.get(base_url + "/digest-auth/auth/user/passwd/MD5")
    jar = dict(r.cookies)
    assert jar.get("fake") == "fake_value", (
        f"the challenge must set fake=fake_value, got {jar}"
    )
    assert jar.get("stale_after") == "never", (
        f"the challenge must set stale_after=never on the default route, "
        f"got {jar}"
    )


def test_digest_require_cookie_rejects_a_client_without_the_challenge_cookie(base_url):
    """``require-cookie=true``: a Cookie header without ``fake`` is a 403.

    Only the two refusal branches are pinned. State A's *success* path for this
    variant raises -- ``check_digest_auth`` concatenates ``request.query_string``,
    which is ``bytes`` in Werkzeug 2.x, so any digest request carrying a query
    string ends in a 500. That crash is not behaviour worth preserving and the
    corpus does not pin it, so nothing here asks for it either way.
    """
    path = "/digest-auth/auth/user/passwd/MD5?require-cookie=true"
    r = hclient.make_client().get(
        base_url + path,
        headers={"Cookie": "unrelated=1",
                 "Authorization": 'Digest username="user", realm="x", '
                                  'nonce="0", uri="/", response="0"'},
    )
    assert r.status_code == 403, (
        f"require-cookie=true must answer 403 when the challenge cookie is "
        f"missing, got {r.status_code}: {r.text[:200]}"
    )
    assert r.json() == {"errors": ["missing cookie set on challenge"]}
    assert r.cookies.get("fake") == "fake_value", (
        "the 403 must still hand out the cookie the client was missing, got "
        f"{dict(r.cookies)}"
    )


def test_digest_require_cookie_challenges_a_cookieless_client(base_url):
    """With no Cookie header at all the request is challenged, not 403'd.

    The order matters: ``require-cookie`` folds into the same condition as a
    missing ``Authorization``, so "no cookies" produces a 401 challenge and only
    "cookies, but not the right one" produces the 403 above.
    """
    path = "/digest-auth/auth/user/passwd/MD5?require-cookie=true"
    r = hclient.make_client().get(
        base_url + path,
        headers={"Authorization": 'Digest username="user", realm="x", '
                                  'nonce="0", uri="/", response="0"'},
    )
    assert r.status_code == 401, (
        f"a request with no cookies at all must be challenged, got "
        f"{r.status_code}"
    )
    assert r.headers.get("www-authenticate", "").lower().startswith("digest "), (
        f"expected a digest challenge, got "
        f"{r.headers.get('www-authenticate')!r}"
    )


# ---------------------------------------------------------------------------
# Redirect chains
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("n", [1, 2, 3, 5])
def test_relative_redirect_chain_terminates_at_get(n, base_url):
    with hclient.make_client() as c:
        r = c.get(base_url + f"/redirect/{n}", follow_redirects=True)
    assert r.status_code == 200
    assert len(r.history) == n, (
        f"/redirect/{n} took {len(r.history)} hops, expected {n}"
    )
    assert r.json()["url"].endswith("/get"), (
        f"/redirect/{n} landed on {r.json().get('url')!r}, expected /get"
    )


@pytest.mark.parametrize("n", [1, 3, 5])
def test_absolute_redirect_chain(n, base_url):
    with hclient.make_client() as c:
        r = c.get(base_url + f"/absolute-redirect/{n}", follow_redirects=True)
    assert r.status_code == 200
    assert len(r.history) == n
    for hop in r.history:
        assert hop.headers["location"].startswith("http://"), (
            f"/absolute-redirect must send absolute Locations, got "
            f"{hop.headers['location']!r}"
        )


@pytest.mark.parametrize("n", [1, 3])
def test_relative_redirect_locations_stay_relative(n, base_url):
    """The app disables Werkzeug's autocorrect_location_header; a port must too."""
    with hclient.make_client() as c:
        r = c.get(base_url + f"/relative-redirect/{n}")
    assert r.status_code == 302
    loc = r.headers["location"]
    assert loc.startswith("/"), (
        f"/relative-redirect/{n} must keep a relative Location, got {loc!r}"
    )


def test_redirect_to_follows_through(base_url):
    with hclient.make_client() as c:
        r = c.get(base_url + "/redirect-to?url=/get", follow_redirects=True)
    assert r.status_code == 200
    assert r.json()["url"].endswith("/get")


# ---------------------------------------------------------------------------
# Cookies across requests
# ---------------------------------------------------------------------------

def test_cookie_set_then_echoed(base_url):
    with hclient.make_client() as c:
        r = c.get(base_url + "/cookies/set?k1=v1&k2=v2")
        assert r.status_code == 302
        echoed = c.get(base_url + "/cookies", cookies=dict(r.cookies))
    assert echoed.json()["cookies"] == {"k1": "v1", "k2": "v2"}, (
        f"cookies did not round-trip: {echoed.json()}"
    )


def test_cookie_delete_removes_it(base_url):
    with hclient.make_client() as c:
        c.get(base_url + "/cookies/set?gone=yes")
        r = c.get(base_url + "/cookies/delete?gone")
        assert r.status_code == 302
        after = c.get(base_url + "/cookies")
    assert "gone" not in after.json()["cookies"], (
        f"the cookie was not deleted: {after.json()}"
    )


def test_cookie_set_path_form(base_url):
    with hclient.make_client() as c:
        r = c.get(base_url + "/cookies/set/named/value")
        assert r.status_code == 302
        assert r.cookies.get("named") == "value"
        echoed = c.get(base_url + "/cookies", cookies={"named": "value"})
    assert echoed.json()["cookies"]["named"] == "value"


def test_cookies_redirect_target(base_url):
    """Setting a cookie redirects to /cookies, and following it shows the cookie."""
    with hclient.make_client() as c:
        r = c.get(base_url + "/cookies/set?trip=ok", follow_redirects=True)
    assert r.status_code == 200
    assert r.json()["cookies"].get("trip") == "ok", (
        f"following the redirect should show the new cookie: {r.json()}"
    )


# ---------------------------------------------------------------------------
# Basic and bearer auth
# ---------------------------------------------------------------------------

def test_basic_auth_round_trip(base_url):
    with hclient.make_client() as c:
        denied = c.get(base_url + "/basic-auth/alice/secret")
        assert denied.status_code == 401
        assert denied.headers["www-authenticate"].startswith("Basic ")
        ok = c.get(base_url + "/basic-auth/alice/secret",
                   auth=("alice", "secret"))
    assert ok.status_code == 200
    assert ok.json() == {"authenticated": True, "user": "alice"}


def test_hidden_basic_auth_hides_the_realm(base_url):
    with hclient.make_client() as c:
        denied = c.get(base_url + "/hidden-basic-auth/alice/secret")
        assert denied.status_code == 404, (
            "hidden basic auth must answer 404, not 401"
        )
        assert "www-authenticate" not in denied.headers, (
            "hidden basic auth must not advertise a challenge"
        )
        ok = c.get(base_url + "/hidden-basic-auth/alice/secret",
                   auth=("alice", "secret"))
    assert ok.status_code == 200


@pytest.mark.parametrize("status", [200, 401])
def test_status_code_auth(status, base_url):
    """``/status/<code>/basic-auth`` style: the code is chosen by credentials."""
    with hclient.make_client() as c:
        if status == 200:
            r = c.get(base_url + "/basic-auth/u/p", auth=("u", "p"))
        else:
            r = c.get(base_url + "/basic-auth/u/p", auth=("u", "wrong"))
    assert r.status_code == status


def test_bearer_auth_round_trip(base_url):
    with hclient.make_client() as c:
        denied = c.get(base_url + "/bearer")
        assert denied.status_code == 401
        assert denied.headers["www-authenticate"] == "Bearer"
        ok = c.get(base_url + "/bearer",
                   headers={"Authorization": "Bearer sometoken"})
    assert ok.status_code == 200
    assert ok.json() == {"authenticated": True, "token": "sometoken"}


# ---------------------------------------------------------------------------
# Conditional requests
# ---------------------------------------------------------------------------

def test_etag_round_trip_gives_304(base_url):
    with hclient.make_client() as c:
        first = c.get(base_url + "/cache")
        assert first.status_code == 200
        etag = first.headers["etag"]
        second = c.get(base_url + "/cache", headers={"If-None-Match": etag})
    assert second.status_code == 304, (
        f"a matching If-None-Match must give 304, got {second.status_code}"
    )
    assert second.content == b"", "a 304 must have an empty body"


def test_last_modified_round_trip_gives_304(base_url):
    with hclient.make_client() as c:
        first = c.get(base_url + "/cache")
        lm = first.headers["last-modified"]
        second = c.get(base_url + "/cache", headers={"If-Modified-Since": lm})
    assert second.status_code == 304


def test_range_request_returns_the_right_slice(base_url):
    with hclient.make_client() as c:
        whole = c.get(base_url + "/range/100")
        part = c.get(base_url + "/range/100",
                     headers={"Range": "bytes=10-19"})
    assert part.status_code == 206
    assert part.headers["content-range"] == "bytes 10-19/100"
    assert part.content == whole.content[10:20], (
        "the 206 slice does not match the corresponding bytes of the full body"
    )


def test_range_suffix_and_open_ended(base_url):
    with hclient.make_client() as c:
        whole = c.get(base_url + "/range/50").content
        suffix = c.get(base_url + "/range/50", headers={"Range": "bytes=-10"})
        open_ended = c.get(base_url + "/range/50",
                           headers={"Range": "bytes=40-"})
    assert suffix.content == whole[-10:], "a suffix range returned wrong bytes"
    assert open_ended.content == whole[40:], "an open range returned wrong bytes"


def test_unsatisfiable_range_is_416(base_url):
    with hclient.make_client() as c:
        r = c.get(base_url + "/range/10", headers={"Range": "bytes=50-60"})
    assert r.status_code == 416
    assert r.headers.get("content-range") == "bytes */10"
