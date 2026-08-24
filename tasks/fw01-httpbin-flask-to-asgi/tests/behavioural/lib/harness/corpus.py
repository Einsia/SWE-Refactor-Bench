"""The request corpus: the single source of truth for the behavioural suite.

Every case in ``CASES`` is replayed twice in the lifetime of this benchmark:

1. At *task build* time, against the State A oracle (Flask/Werkzeug/gunicorn),
   to freeze a golden response into ``tests/golden/responses.json``.
2. At *grading* time, against the submitted State B service, whose response is
   compared with that golden.

Because both sides run the same corpus through the same normalisation, the
comparison is a genuine behavioural diff rather than a restatement of whatever
the reference implementation happened to do.

A case is a plain dict so it survives JSON round-tripping unchanged:

    id        unique, stable, becomes the pytest test id -- never renumber
    group     coarse family, used to slice the report
    method    HTTP method
    path      path only, no query string
    query     str | None -- raw query string, appended verbatim so that
              deliberately malformed queries survive intact
    headers   list[tuple[str, str]] -- a list, not a dict, so repeated header
              names can be exercised
    body      None | str | {"b64": "..."} -- raw request body
    body_mode how the response body is compared (see BODY_MODES)
    headers_extra  additional response header names to compare for this case
    headers_skip   response header names *not* to compare for this case
    nondet    response header names whose value is checked for shape only
    note      why the case exists, for whoever debugs a failure later
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Comparison vocabulary
# ---------------------------------------------------------------------------

#: How a response body is compared against its golden.
BODY_MODES = (
    "exact",        # byte-for-byte after scrubbing
    "json",         # parsed and compared as data, then also byte-compared
    "gzip-json",    # gunzip, then compare as JSON data
    "deflate-json", # inflate, then compare as JSON data
    "br-json",      # brotli-decompress, then compare as JSON data
    "len",          # length only (content is random-but-unseeded)
    "ignore",       # not compared here; a dedicated structural test covers it
)

#: Response headers compared for every case. Deliberately excludes Server,
#: Date, Connection, Keep-Alive, Transfer-Encoding and Content-Length: those
#: are server-framing artefacts that instruction.md declares out of contract.
SEMANTIC_HEADERS = (
    "content-type",
    "content-encoding",
    "location",
    "www-authenticate",
    "proxy-authenticate",
    "etag",
    "cache-control",
    "accept-ranges",
    "content-range",
    "vary",
    "link",
    "x-more-info",
    "access-control-allow-origin",
    "access-control-allow-credentials",
    "access-control-allow-methods",
    "access-control-max-age",
    "access-control-allow-headers",
    "allow",
    "refresh",
    "set-cookie",
)

#: Headers whose value is a comma-separated *set*: the members are contractual,
#: their order is not. Werkzeug builds Allow from a Python set, so even two runs
#: of the original app disagree on the order -- comparing the raw string would
#: fail a correct submission at random. Discovered by diffing two captures of
#: the unmodified State A app, not assumed.
SET_VALUED_HEADERS = frozenset({
    "allow",
    "access-control-allow-methods",
    "vary",
    "accept-ranges",
})

#: Header values that legitimately differ run to run: presence and shape are
#: checked, the value is not. Values are regexes anchored with fullmatch.
NONDET_HEADER_SHAPES = {
    "etag": r"[0-9a-f]{32}|range\d+|\"?[\w\-./]+\"?",
    "x-runtime": r"\d+(\.\d+)?",
    "set-cookie": r".+",
    "www-authenticate": r".+",
    "cache-control": r".+",
    "last-modified": r"[A-Z][a-z]{2}, \d{2} [A-Z][a-z]{2} \d{4} \d{2}:\d{2}:\d{2} GMT",
}


def _case(
    cid,
    group,
    method,
    path,
    query=None,
    headers=None,
    body=None,
    body_mode="exact",
    headers_extra=(),
    headers_skip=(),
    nondet=(),
    note="",
):
    return {
        "id": cid,
        "group": group,
        "method": method,
        "path": path,
        "query": query,
        "headers": [list(h) for h in (headers or ())],
        "body": body,
        "body_mode": body_mode,
        "headers_extra": list(headers_extra),
        "headers_skip": list(headers_skip),
        "nondet": list(nondet),
        "note": note,
    }


# A body that is not valid UTF-8: httpbin returns it as a base64 data: URL.
LATIN1_BODY = {"b64": "SGVsbG8g//79IHdvcmxk"}          # contains 0xFF 0xFE 0xFD
NUL_BODY = {"b64": "YQBiAGM="}                          # a\0b\0c

CASES = []
A = CASES.append


# ===========================================================================
# 1. HTTP methods -- the /get /post /put /patch /delete /anything family
# ===========================================================================

A(_case("get-plain", "methods", "GET", "/get",
        note="baseline request-inspection response"))
A(_case("get-query-single", "methods", "GET", "/get", query="a=1",
        note="single query value stays a bare string"))
A(_case("get-query-repeated", "methods", "GET", "/get", query="a=1&a=2&a=3",
        note="repeated query keys collapse to a list (semiflatten)"))
A(_case("get-query-mixed", "methods", "GET", "/get", query="a=1&b=2&b=3&c=",
        note="mixed single/repeated/empty in one query"))
A(_case("get-query-empty-value", "methods", "GET", "/get", query="a=",
        note="empty value is preserved, not dropped"))
A(_case("get-query-no-value", "methods", "GET", "/get", query="flag",
        note="valueless key"))
A(_case("get-query-plus", "methods", "GET", "/get", query="a=1+2",
        note="+ decodes to space in a query string"))
A(_case("get-query-pct20", "methods", "GET", "/get", query="a=1%202",
        note="%20 decodes to space"))
A(_case("get-query-unicode", "methods", "GET", "/get", query="q=%E4%B8%AD%E6%96%87",
        note="percent-encoded UTF-8 in a query value"))
A(_case("get-query-semicolon", "methods", "GET", "/get", query="a=1;b=2",
        note="semicolon is NOT a separator in modern Werkzeug"))
A(_case("get-query-amp-only", "methods", "GET", "/get", query="&&",
        note="degenerate query string"))
A(_case("get-query-eq-in-value", "methods", "GET", "/get", query="a=b=c",
        note="only the first = splits"))
A(_case("get-query-bracket", "methods", "GET", "/get", query="a%5B%5D=1&a%5B%5D=2",
        note="PHP-style array keys are not special-cased"))
A(_case("get-query-long", "methods", "GET", "/get", query="v=" + "x" * 512,
        note="long query value"))
A(_case("get-query-dup-and-single", "methods", "GET", "/get", query="x=1&y=2&x=3",
        note="interleaved duplicate keys keep document order"))

for meth in ("POST", "PUT", "PATCH", "DELETE"):
    low = meth.lower()
    A(_case(f"{low}-empty", "methods", meth, f"/{low}",
            note=f"{meth} with no body"))
    A(_case(f"{low}-form", "methods", meth, f"/{low}",
            headers=[("Content-Type", "application/x-www-form-urlencoded")],
            body="a=1&b=2",
            note=f"{meth} urlencoded form -> form dict, data emptied"))
    A(_case(f"{low}-form-repeated", "methods", meth, f"/{low}",
            headers=[("Content-Type", "application/x-www-form-urlencoded")],
            body="k=1&k=2&k=3",
            note="repeated form keys collapse to a list"))
    A(_case(f"{low}-json", "methods", meth, f"/{low}",
            headers=[("Content-Type", "application/json")],
            body='{"b":2,"a":[1,{"c":3}]}',
            note=f"{meth} JSON body -> parsed into json field, key order sorted"))
    A(_case(f"{low}-json-scalar", "methods", meth, f"/{low}",
            headers=[("Content-Type", "application/json")], body="42",
            note="a bare JSON scalar is still valid JSON"))
    A(_case(f"{low}-json-null", "methods", meth, f"/{low}",
            headers=[("Content-Type", "application/json")], body="null",
            note="JSON null is indistinguishable from absent -- must match"))
    A(_case(f"{low}-json-invalid", "methods", meth, f"/{low}",
            headers=[("Content-Type", "application/json")], body="{not json",
            note="malformed JSON leaves json=null and keeps data"))
    A(_case(f"{low}-text", "methods", meth, f"/{low}",
            headers=[("Content-Type", "text/plain")], body="hello world",
            note="non-form body lands in data verbatim"))
    A(_case(f"{low}-binary", "methods", meth, f"/{low}",
            headers=[("Content-Type", "application/octet-stream")],
            body=LATIN1_BODY,
            note="non-UTF-8 body is returned as a base64 data: URL"))
    A(_case(f"{low}-nul", "methods", meth, f"/{low}",
            headers=[("Content-Type", "application/octet-stream")],
            body=NUL_BODY,
            note="NUL bytes in the body"))
    A(_case(f"{low}-query-and-body", "methods", meth, f"/{low}",
            query="q=1&q=2",
            headers=[("Content-Type", "application/x-www-form-urlencoded")],
            body="f=a&f=b",
            note="args and form are reported independently"))
    A(_case(f"{low}-no-ctype", "methods", meth, f"/{low}", body="raw",
            note="body with no Content-Type at all"))

# Wrong method against a single-method route -> 405 with an Allow header.
A(_case("get-on-post", "methods", "GET", "/post", headers_extra=("allow",),
        body_mode="ignore",
        note="405: Allow is compared, the HTML error page is not in contract"))
A(_case("post-on-get", "methods", "POST", "/get", headers_extra=("allow",),
        body_mode="ignore", note="405 the other way round"))
A(_case("put-on-delete", "methods", "PUT", "/delete", headers_extra=("allow",),
        body_mode="ignore", note="405 on /delete"))
A(_case("patch-on-put", "methods", "PATCH", "/put", headers_extra=("allow",),
        body_mode="ignore", note="405 on /put"))
A(_case("delete-on-patch", "methods", "DELETE", "/patch", headers_extra=("allow",),
        body_mode="ignore", note="405 on /patch"))

# /anything mirrors every method and reports the URL it was reached by.
for meth in ("GET", "POST", "PUT", "PATCH", "DELETE"):
    low = meth.lower()
    A(_case(f"anything-{low}", "anything", meth, "/anything",
            note=f"/anything accepts {meth}"))
    A(_case(f"anything-sub-{low}", "anything", meth, "/anything/a/b/c",
            note="path converter captures the whole tail"))
A(_case("anything-query", "anything", "GET", "/anything", query="x=1&x=2",
        note="/anything reports args like /get"))
A(_case("anything-json", "anything", "POST", "/anything",
        headers=[("Content-Type", "application/json")], body='{"z":1,"a":2}',
        note="/anything parses JSON"))
A(_case("anything-trailing-slash", "anything", "GET", "/anything/",
        note="empty path tail"))
A(_case("anything-encoded", "anything", "GET", "/anything/a%2Fb",
        note="percent-encoded slash inside the path segment"))
A(_case("anything-unicode-path", "anything", "GET", "/anything/%E4%B8%AD",
        note="non-ASCII path segment"))
A(_case("anything-dots", "anything", "GET", "/anything/a/../b",
        note="dot segments are resolved by the client/server before routing"))


# ===========================================================================
# 2. Status codes
# ===========================================================================

for code in (200, 201, 202, 203, 204, 205, 206, 207, 208, 226):
    A(_case(f"status-{code}", "status", "GET", f"/status/{code}",
            body_mode="exact", note=f"2xx: {code}"))
for code in (300, 301, 302, 303, 304, 305, 306, 307, 308):
    A(_case(f"status-{code}", "status", "GET", f"/status/{code}",
            body_mode="exact",
            note=f"3xx: {code} -- several carry a Location and drop Content-Type"))
for code in (400, 401, 402, 403, 404, 405, 406, 407, 408, 409, 410, 411, 412,
             413, 414, 415, 416, 417, 418, 421, 422, 423, 424, 425, 426, 428,
             429, 431, 451):
    A(_case(f"status-{code}", "status", "GET", f"/status/{code}",
            body_mode="exact",
            # See status-407-proxy: the 407 handler's Proxy-Authenticate is
            # hop-by-hop and was removed by the server, not by the application.
            headers_skip=("proxy-authenticate",) if code == 407 else (),
            note=f"4xx: {code}"))
for code in (500, 501, 502, 503, 504, 505, 506, 507, 508, 510, 511):
    A(_case(f"status-{code}", "status", "GET", f"/status/{code}",
            body_mode="exact", note=f"5xx: {code}"))

# Status-code special cases spelled out in the source's status table.
A(_case("status-401-challenge", "status", "GET", "/status/401",
        note="401 emits WWW-Authenticate: Basic realm=\"Fake Realm\""))
A(_case("status-402-body", "status", "GET", "/status/402",
        note="402 has the shut-up-and-take-my-money body and X-More-Info"))
A(_case("status-406-json", "status", "GET", "/status/406",
        note="406 returns a JSON error body with its own content type"))
A(_case("status-407-proxy", "status", "GET", "/status/407",
        # The application sets Proxy-Authenticate, but it is a hop-by-hop
        # header: State A's server removed it before the response left the
        # process, so the client never saw it. Whether the replacement server
        # also removes it is not the application's business, so the header is
        # not compared -- a dedicated structural test checks that the handler
        # still sets it.
        headers_skip=("proxy-authenticate",),
        note="407 sets Proxy-Authenticate, which the server strips as a "
             "hop-by-hop header; only the status and the rest are compared"))
A(_case("status-418-teapot", "status", "GET", "/status/418",
        note="418 body art plus X-More-Info"))
A(_case("status-304-empty", "status", "GET", "/status/304",
        note="304 must carry no body"))
A(_case("status-204-empty", "status", "GET", "/status/204",
        note="204 must carry no body"))

for meth in ("POST", "PUT", "PATCH", "DELETE"):
    A(_case(f"status-{meth.lower()}-200", "status", meth, "/status/200",
            note=f"/status accepts {meth}"))
    A(_case(f"status-{meth.lower()}-418", "status", meth, "/status/418",
            note=f"{meth} keeps the special-case body"))

# Weighted choice: a single-choice weighted list is deterministic.
A(_case("status-weighted-single", "status", "GET", "/status/200:1",
        note="weighted form with one choice always yields it"))
A(_case("status-weighted-zero-weight", "status", "GET", "/status/200:1,500:0",
        note="a zero weight can never be chosen"))
A(_case("status-weighted-only-500", "status", "GET", "/status/500:5",
        note="weight is irrelevant when there is one choice"))
A(_case("status-bad-notint", "status", "GET", "/status/abc", body_mode="ignore",
        note="non-integer code -> 400 from the route's own guard"))
A(_case("status-bad-empty-choice", "status", "GET", "/status/200,",
        body_mode="ignore", note="trailing comma in the choice list"))
A(_case("status-bad-weight", "status", "GET", "/status/200:x",
        body_mode="ignore", note="non-numeric weight"))


# ===========================================================================
# 3. Request inspection
# ===========================================================================

A(_case("ip", "inspect", "GET", "/ip", note="origin is the peer address"))
A(_case("user-agent", "inspect", "GET", "/user-agent",
        headers=[("User-Agent", "swerefactor/1.0")],
        note="echoes the User-Agent header"))
A(_case("user-agent-absent", "inspect", "GET", "/user-agent",
        note="no User-Agent -> null"))
A(_case("user-agent-weird", "inspect", "GET", "/user-agent",
        headers=[("User-Agent", "a b/1.0 (x; y) \"q\"")],
        note="User-Agent with quotes and parens is not re-escaped"))
A(_case("headers-plain", "inspect", "GET", "/headers",
        note="baseline header dict"))
A(_case("headers-custom", "inspect", "GET", "/headers",
        headers=[("X-Custom", "value"), ("X-Another", "2")],
        note="custom headers appear title-cased"))
A(_case("headers-repeated", "inspect", "GET", "/headers",
        headers=[("X-Dup", "a"), ("X-Dup", "b")],
        note="repeated request headers are folded with a comma"))
A(_case("headers-lowercase-in", "inspect", "GET", "/headers",
        headers=[("x-lower-case", "v")],
        note="wire casing is normalised to Title-Case in the report"))
A(_case("headers-empty-value", "inspect", "GET", "/headers",
        headers=[("X-Empty", "")], note="empty header value is kept"))
A(_case("headers-spaces", "inspect", "GET", "/headers",
        headers=[("X-Spaced", "a  b   c")],
        note="internal runs of whitespace in a header value are preserved"))
A(_case("headers-env-stripped", "inspect", "GET", "/headers",
        headers=[("X-Forwarded-For", "1.2.3.4"), ("Via", "proxy"),
                 ("X-Request-Id", "abc"), ("Connect-Time", "1")],
        note="ENV_HEADERS are stripped when show_env is absent"))
A(_case("headers-env-shown", "inspect", "GET", "/headers", query="show_env=1",
        headers=[("X-Forwarded-For", "1.2.3.4"), ("Via", "proxy"),
                 ("X-Request-Id", "abc"), ("Connect-Time", "1")],
        note="show_env keeps the whole proxy header set"))
A(_case("headers-env-shown-0", "inspect", "GET", "/headers", query="show_env=0",
        headers=[("X-Forwarded-For", "1.2.3.4")],
        note="show_env=0 is still truthy: the key's presence is what counts"))
A(_case("get-env-stripped", "inspect", "GET", "/get",
        headers=[("X-Real-Ip", "9.9.9.9"), ("X-Varnish", "1")],
        note="/get filters ENV_HEADERS too"))
A(_case("get-env-shown", "inspect", "GET", "/get", query="show_env=1",
        headers=[("X-Real-Ip", "9.9.9.9"), ("X-Varnish", "1")],
        note="/get honours show_env"))
A(_case("get-xff-url", "inspect", "GET", "/get",
        headers=[("X-Forwarded-Proto", "https")],
        note="X-Forwarded-Proto rewrites the reported url scheme"))
A(_case("get-xf-protocol-url", "inspect", "GET", "/get",
        headers=[("X-Forwarded-Protocol", "https")],
        note="X-Forwarded-Protocol is the alternate spelling"))
A(_case("get-xf-ssl-url", "inspect", "GET", "/get",
        headers=[("X-Forwarded-Ssl", "on")],
        note="X-Forwarded-Ssl: on implies https"))
A(_case("get-xf-ssl-off", "inspect", "GET", "/get",
        headers=[("X-Forwarded-Ssl", "off")],
        note="X-Forwarded-Ssl: off does not change the scheme"))
A(_case("uuid", "inspect", "GET", "/uuid", body_mode="ignore",
        note="random uuid -- shape checked by a dedicated test"))


# ===========================================================================
# 4. Authentication
# ===========================================================================

import base64 as _b64


def _basic(user, pw):
    raw = f"{user}:{pw}".encode()
    return ("Authorization", "Basic " + _b64.b64encode(raw).decode())


A(_case("basic-ok", "auth", "GET", "/basic-auth/user/passwd",
        headers=[_basic("user", "passwd")], note="correct basic credentials"))
A(_case("basic-bad-pw", "auth", "GET", "/basic-auth/user/passwd",
        headers=[_basic("user", "wrong")],
        note="wrong password -> 401 + WWW-Authenticate"))
A(_case("basic-bad-user", "auth", "GET", "/basic-auth/user/passwd",
        headers=[_basic("other", "passwd")], note="wrong user -> 401"))
A(_case("basic-absent", "auth", "GET", "/basic-auth/user/passwd",
        note="no Authorization -> 401 challenge"))
A(_case("basic-malformed", "auth", "GET", "/basic-auth/user/passwd",
        headers=[("Authorization", "Basic !!!notbase64")],
        note="undecodable basic credentials -> 401"))
A(_case("basic-wrong-scheme", "auth", "GET", "/basic-auth/user/passwd",
        headers=[("Authorization", "Bearer tok")],
        note="a Bearer header does not satisfy basic auth"))
A(_case("basic-empty-pw", "auth", "GET", "/basic-auth/user/",
        headers=[_basic("user", "")], note="empty password in the route"))
A(_case("basic-unicode", "auth", "GET", "/basic-auth/us%C3%A9r/p%C3%A4ss",
        headers=[_basic("usér", "päss")],
        note="non-ASCII credentials, latin-1 on the wire"))
A(_case("basic-colon-in-pw", "auth", "GET", "/basic-auth/user/pa:ss",
        headers=[_basic("user", "pa:ss")],
        note="only the first colon separates user from password"))

A(_case("hidden-basic-ok", "auth", "GET", "/hidden-basic-auth/user/passwd",
        headers=[_basic("user", "passwd")], note="hidden basic, correct"))
A(_case("hidden-basic-bad", "auth", "GET", "/hidden-basic-auth/user/passwd",
        headers=[_basic("user", "wrong")],
        note="hidden basic failure is a 404, and emits NO challenge"))
A(_case("hidden-basic-absent", "auth", "GET", "/hidden-basic-auth/user/passwd",
        note="hidden basic without credentials is a 404"))

A(_case("bearer-ok", "auth", "GET", "/bearer",
        headers=[("Authorization", "Bearer sometoken")],
        note="bearer echoes the token"))
A(_case("bearer-absent", "auth", "GET", "/bearer",
        note="no Authorization -> 401 with WWW-Authenticate: Bearer"))
A(_case("bearer-wrong-scheme", "auth", "GET", "/bearer",
        headers=[("Authorization", "Basic abc")],
        note="prefix must be exactly 'Bearer '"))
A(_case("bearer-lowercase", "auth", "GET", "/bearer",
        headers=[("Authorization", "bearer tok")],
        note="the scheme check is case-sensitive"))
A(_case("bearer-no-space", "auth", "GET", "/bearer",
        headers=[("Authorization", "Bearer")],
        note="the prefix test is 'Bearer ' with a space, so this is a 401"))
A(_case("bearer-double-space", "auth", "GET", "/bearer",
        headers=[("Authorization", "Bearer  tok")],
        note="the extra space becomes part of the token"))
A(_case("bearer-spaces", "auth", "GET", "/bearer",
        headers=[("Authorization", "Bearer a b c")],
        note="token may contain spaces once split(' ', 1) has run"))

A(_case("deny", "auth", "GET", "/deny", note="the ASCII-art deny page"))
A(_case("robots", "auth", "GET", "/robots.txt", note="robots.txt body"))

# Digest challenges (unauthenticated first hits). The nonce/opaque are random,
# so the header value is shape-checked here and parsed structurally elsewhere.
for qop in ("auth", "auth-int"):
    A(_case(f"digest-challenge-{qop}", "auth", "GET",
            f"/digest-auth/{qop}/user/passwd",
            nondet=("www-authenticate", "set-cookie"), body_mode="ignore",
            note=f"digest challenge for qop={qop}"))
for algo in ("MD5", "SHA-256", "SHA-512"):
    A(_case(f"digest-challenge-{algo}", "auth", "GET",
            f"/digest-auth/auth/user/passwd/{algo}",
            nondet=("www-authenticate", "set-cookie"), body_mode="ignore",
            note=f"digest challenge advertises algorithm={algo}"))
A(_case("digest-challenge-badalgo", "auth", "GET",
        "/digest-auth/auth/user/passwd/BOGUS",
        nondet=("www-authenticate", "set-cookie"), body_mode="ignore",
        note="an unknown algorithm falls back to MD5"))
A(_case("digest-challenge-badqop", "auth", "GET",
        "/digest-auth/bogus/user/passwd",
        nondet=("www-authenticate", "set-cookie"), body_mode="ignore",
        note="an unknown qop is coerced to None"))
A(_case("digest-challenge-never-stale", "auth", "GET",
        "/digest-auth/auth/user/passwd/MD5/never",
        nondet=("www-authenticate", "set-cookie"), body_mode="ignore",
        note="non-numeric stale_after is passed through as-is"))


# ===========================================================================
# 5. Cookies
# ===========================================================================

A(_case("cookies-empty", "cookies", "GET", "/cookies",
        note="no Cookie header -> empty dict"))
A(_case("cookies-one", "cookies", "GET", "/cookies",
        headers=[("Cookie", "a=1")], note="one cookie"))
A(_case("cookies-many", "cookies", "GET", "/cookies",
        headers=[("Cookie", "a=1; b=2; c=3")], note="several cookies"))
A(_case("cookies-dup", "cookies", "GET", "/cookies",
        headers=[("Cookie", "a=1; a=2")],
        note="duplicate cookie names -- first/last wins must match"))
A(_case("cookies-empty-value", "cookies", "GET", "/cookies",
        headers=[("Cookie", "a=")], note="empty cookie value"))
A(_case("cookies-quoted", "cookies", "GET", "/cookies",
        headers=[("Cookie", 'a="quoted value"')],
        note="quoted cookie values are unquoted by the parser"))
A(_case("cookies-encoded", "cookies", "GET", "/cookies",
        headers=[("Cookie", "a=%E4%B8%AD")],
        note="percent-encoded cookie value is decoded"))
A(_case("cookies-spaces", "cookies", "GET", "/cookies",
        headers=[("Cookie", "a=1;b=2")],
        note="no space after the semicolon"))
A(_case("cookies-set-qs", "cookies", "GET", "/cookies/set",
        query="k1=v1&k2=v2", nondet=("set-cookie",),
        note="/cookies/set redirects to /cookies and sets each pair"))
A(_case("cookies-set-path", "cookies", "GET", "/cookies/set/name/value",
        nondet=("set-cookie",), note="path form of cookie setting"))
A(_case("cookies-set-empty-value", "cookies", "GET", "/cookies/set/name/",
        nondet=("set-cookie",), body_mode="ignore",
        note="empty value in the path form"))
A(_case("cookies-set-no-args", "cookies", "GET", "/cookies/set",
        note="/cookies/set with no query returns the cookie list, no redirect"))
A(_case("cookies-delete", "cookies", "GET", "/cookies/delete", query="k1=",
        nondet=("set-cookie",), note="deletion emits an expiring Set-Cookie"))
A(_case("cookies-delete-multi", "cookies", "GET", "/cookies/delete",
        query="a=&b=", nondet=("set-cookie",), note="delete several at once"))
A(_case("cookies-set-then-read", "cookies", "GET", "/cookies",
        headers=[("Cookie", "k1=v1; k2=v2")],
        note="reading back what /cookies/set would have written"))
A(_case("cookies-in-get", "cookies", "GET", "/get",
        headers=[("Cookie", "sess=abc")],
        note="/get reports Cookie as a header, not as a cookies dict"))


# ===========================================================================
# 6. Redirects
# ===========================================================================

for n in (1, 2, 3, 5, 10):
    A(_case(f"redirect-{n}", "redirect", "GET", f"/redirect/{n}",
            note=f"/redirect/{n} -- relative Location, no autocorrection"))
    A(_case(f"relative-redirect-{n}", "redirect", "GET",
            f"/relative-redirect/{n}", note="explicitly relative redirect"))
    A(_case(f"absolute-redirect-{n}", "redirect", "GET",
            f"/absolute-redirect/{n}", note="absolute Location with host"))
A(_case("redirect-0", "redirect", "GET", "/redirect/0",
        note="n<=0 is clamped to 1"))
A(_case("redirect-negative", "redirect", "GET", "/redirect/-1",
        body_mode="ignore", note="negative n does not match the int converter"))
A(_case("relative-redirect-0", "redirect", "GET", "/relative-redirect/0",
        note="clamping on the relative variant"))
A(_case("absolute-redirect-0", "redirect", "GET", "/absolute-redirect/0",
        note="clamping on the absolute variant"))
A(_case("redirect-to-simple", "redirect", "GET", "/redirect-to",
        query="url=http%3A%2F%2Fexample.com%2F",
        note="/redirect-to defaults to 302"))
for sc in (300, 301, 302, 303, 304, 305, 306, 307, 308):
    A(_case(f"redirect-to-{sc}", "redirect", "GET", "/redirect-to",
            query=f"url=http%3A%2F%2Fexample.com%2F&status_code={sc}",
            note=f"status_code={sc} is honoured inside 300..308"))
A(_case("redirect-to-out-of-range", "redirect", "GET", "/redirect-to",
        query="url=http%3A%2F%2Fexample.com%2F&status_code=200",
        note="a status_code outside 300..308 falls back to 302"))
A(_case("redirect-to-noturl", "redirect", "GET", "/redirect-to",
        query="url=%2Frelative%2Fpath",
        note="a relative target stays relative in Location"))
A(_case("redirect-to-missing-url", "redirect", "GET", "/redirect-to",
        body_mode="ignore",
        # State A answers 302 with the literal header `Location: None`, not 400:
        # there is no guard on the route, and `args["url"]` on a missing key
        # returns None because httpbin's CaseInsensitiveDict falls through by
        # design.  A port that raises on that None -- or that adds the 400 the
        # route looks like it should have -- fails this case and three others.
        note="missing url -> 302 with the literal header `Location: None`"))
A(_case("redirect-to-post", "redirect", "POST", "/redirect-to",
        query="url=http%3A%2F%2Fexample.com%2F",
        note="/redirect-to accepts POST"))
A(_case("redirect-to-form", "redirect", "POST", "/redirect-to",
        headers=[("Content-Type", "application/x-www-form-urlencoded")],
        body="url=http%3A%2F%2Fexample.com%2F&status_code=307",
        # Not what the form asked for, and the capture is the authority: State A
        # reads `url` from the *query only* (`args_dict = request.args.items()`),
        # so a form-encoded url is not seen and this answers 302 /
        # `Location: None` -- not the 307 in the body.  `redirect-to-post` is the
        # contrast: same method, url in the query, and it redirects properly.
        note="the form is NOT read: url comes from the query, so this is "
             "302 / `Location: None` despite the form's url and status_code"))
A(_case("redirect-to-unicode", "redirect", "GET", "/redirect-to",
        query="url=http%3A%2F%2Fexample.com%2F%E4%B8%AD",
        note="non-ASCII in the redirect target"))


# ===========================================================================
# 7. Response formats and templated pages
# ===========================================================================

A(_case("json-doc", "formats", "GET", "/json", note="the slideshow document"))
A(_case("xml-doc", "formats", "GET", "/xml", note="sample.xml verbatim"))
A(_case("html-page", "formats", "GET", "/html", note="the Moby Dick excerpt"))
A(_case("forms-post", "formats", "GET", "/forms/post", note="the HTML form"))
A(_case("legacy-page", "formats", "GET", "/legacy",
        note="the legacy landing page"))
A(_case("encoding-utf8", "formats", "GET", "/encoding/utf8",
        note="the UTF-8 demo file, byte for byte"))
A(_case("robots-txt", "formats", "GET", "/robots.txt", note="robots.txt"))
A(_case("deny-page", "formats", "GET", "/deny", note="the deny page"))
A(_case("image-default", "formats", "GET", "/image", note="no Accept -> PNG"))
A(_case("image-png", "formats", "GET", "/image/png", note="PNG bytes"))
A(_case("image-jpeg", "formats", "GET", "/image/jpeg", note="JPEG bytes"))
A(_case("image-webp", "formats", "GET", "/image/webp", note="WEBP bytes"))
A(_case("image-svg", "formats", "GET", "/image/svg", note="SVG bytes"))
A(_case("image-accept-png", "formats", "GET", "/image",
        headers=[("Accept", "image/png")], note="Accept negotiation -> png"))
A(_case("image-accept-jpeg", "formats", "GET", "/image",
        headers=[("Accept", "image/jpeg")], note="-> jpeg"))
A(_case("image-accept-webp", "formats", "GET", "/image",
        headers=[("Accept", "image/webp")], note="-> webp"))
A(_case("image-accept-svg", "formats", "GET", "/image",
        headers=[("Accept", "image/svg+xml")], note="-> svg"))
A(_case("image-accept-star", "formats", "GET", "/image",
        headers=[("Accept", "image/*")], note="image/* -> png"))
A(_case("image-accept-any", "formats", "GET", "/image",
        headers=[("Accept", "*/*")],
        note="*/* is NOT image/* -- this is a 406 in the original"))
A(_case("image-accept-html", "formats", "GET", "/image",
        headers=[("Accept", "text/html")], note="unsupported Accept -> 406"))
A(_case("image-accept-multi", "formats", "GET", "/image",
        headers=[("Accept", "text/html,image/webp;q=0.9")],
        note="substring matching means webp wins regardless of q"))
A(_case("image-accept-case", "formats", "GET", "/image",
        headers=[("Accept", "IMAGE/PNG")], note="Accept is lower-cased first"))
A(_case("favicon", "formats", "GET", "/static/favicon.ico", body_mode="ignore",
        # Werkzeug's send_file builds the ETag from mtime-size-INODE, which is
        # not reproducible across containers -- not even by unmodified State A.
        # The bytes and the content type are contractual; those two values are
        # not, so a dedicated structural test checks the file instead.
        headers_skip=("etag", "cache-control"),
        note="the static mount serves the favicon"))


# ===========================================================================
# 8. Response inspection: headers, cache, etag
# ===========================================================================

A(_case("response-headers-one", "respinspect", "GET", "/response-headers",
        query="X-Test=1", note="echoes a requested header into the response"))
A(_case("response-headers-many", "respinspect", "GET", "/response-headers",
        query="A=1&B=2", note="several headers at once"))
A(_case("response-headers-repeated", "respinspect", "GET", "/response-headers",
        query="X-Rep=1&X-Rep=2",
        note="a repeated key becomes a repeated response header AND a list"))
A(_case("response-headers-ctype-override", "respinspect", "GET",
        "/response-headers", query="Content-Type=text%2Fplain",
        note="overriding Content-Type changes the response's own type"))
A(_case("response-headers-post", "respinspect", "POST", "/response-headers",
        query="X-Test=1", note="/response-headers also accepts POST"))
A(_case("response-headers-empty", "respinspect", "GET", "/response-headers",
        note="no query -> just the self-describing body"))
A(_case("response-headers-unicode", "respinspect", "GET", "/response-headers",
        query="X-U=%E4%B8%AD", body_mode="ignore",
        # A header value that cannot be encoded for the wire: the request fails
        # and the status is what is contractual. Which component notices, and
        # therefore whose error page arrives, is a property of the server rather
        # than of the application -- State A's own server substituted a page of
        # its own here, complete with its own content type and without the CORS
        # headers the application adds -- so neither is compared.
        headers_skip=("content-type", "access-control-allow-origin",
                      "access-control-allow-credentials"),
        note="a non-ASCII response header value cannot be sent: 500. The error "
             "page and its headers come from whichever layer refuses the "
             "value, so only the status is compared"))
A(_case("cache-no-conditional", "respinspect", "GET", "/cache",
        nondet=("etag", "last-modified"), headers_extra=("last-modified",),
        note="unconditional /cache adds Last-Modified (now) and a random ETag; "
             "both are shape-checked, never value-compared"))
A(_case("cache-if-modified-since", "respinspect", "GET", "/cache",
        headers=[("If-Modified-Since", "Sat, 01 Jan 2000 00:00:00 GMT")],
        note="a conditional request short-circuits to 304"))
A(_case("cache-if-none-match", "respinspect", "GET", "/cache",
        headers=[("If-None-Match", '"abc"')], note="If-None-Match -> 304"))
for secs in (0, 1, 30, 60, 3600):
    A(_case(f"cache-control-{secs}", "respinspect", "GET", f"/cache/{secs}",
            note=f"Cache-Control: public, max-age={secs}"))
A(_case("etag-plain", "respinspect", "GET", "/etag/abc",
        note="no conditional headers -> 200 with the ETag echoed"))
A(_case("etag-if-none-match-hit", "respinspect", "GET", "/etag/abc",
        headers=[("If-None-Match", "abc")], note="matching If-None-Match -> 304"))
A(_case("etag-if-none-match-star", "respinspect", "GET", "/etag/abc",
        headers=[("If-None-Match", "*")], note="* matches anything -> 304"))
A(_case("etag-if-none-match-miss", "respinspect", "GET", "/etag/abc",
        headers=[("If-None-Match", "other")], note="no match -> normal 200"))
A(_case("etag-if-none-match-list", "respinspect", "GET", "/etag/abc",
        headers=[("If-None-Match", '"x", "abc"')],
        note="multi-value If-None-Match is split and unquoted"))
A(_case("etag-if-match-hit", "respinspect", "GET", "/etag/abc",
        headers=[("If-Match", "abc")], note="matching If-Match -> 200"))
A(_case("etag-if-match-star", "respinspect", "GET", "/etag/abc",
        headers=[("If-Match", "*")], note="* satisfies If-Match"))
A(_case("etag-if-match-miss", "respinspect", "GET", "/etag/abc",
        headers=[("If-Match", "other")], note="failed If-Match -> 412"))
A(_case("etag-both", "respinspect", "GET", "/etag/abc",
        headers=[("If-None-Match", "abc"), ("If-Match", "other")],
        note="If-None-Match is evaluated first (elif), so this is a 304"))
A(_case("etag-quoted-path", "respinspect", "GET", "/etag/%22quoted%22",
        headers=[("If-None-Match", '"quoted"')],
        note="a quoted etag in the path"))


# ===========================================================================
# 9. Dynamic data
# ===========================================================================

for n, seed in ((0, 1), (1, 1), (16, 1), (256, 1), (1024, 7), (4096, 42),
                (100, 0), (100, 999999), (65536, 3)):
    A(_case(f"bytes-{n}-seed{seed}", "dynamic", "GET", f"/bytes/{n}",
            query=f"seed={seed}",
            note=f"seeded PRNG: {n} bytes with seed {seed} must be identical"))
A(_case("bytes-oversize", "dynamic", "GET", "/bytes/999999", query="seed=1",
        note="capped at 100KB, and the cap applies before seeding"))
A(_case("bytes-unseeded", "dynamic", "GET", "/bytes/32", body_mode="len",
        note="unseeded -> only the length is contractual"))
A(_case("bytes-seed-case", "dynamic", "GET", "/bytes/32", query="SEED=5",
        note="the params dict is case-insensitive, so SEED works"))

for n, seed in ((1, 1), (128, 1), (1024, 2), (10240, 3)):
    A(_case(f"stream-bytes-{n}-seed{seed}", "dynamic", "GET",
            f"/stream-bytes/{n}", query=f"seed={seed}",
            headers_skip=("content-length",),
            note="streamed but identical bytes; must not declare a length"))
A(_case("stream-bytes-chunked", "dynamic", "GET", "/stream-bytes/100",
        query="seed=1&chunk_size=7", headers_skip=("content-length",),
        note="chunk_size changes framing, not the byte stream"))
A(_case("stream-bytes-chunk-zero", "dynamic", "GET", "/stream-bytes/10",
        query="seed=1&chunk_size=0", headers_skip=("content-length",),
        note="chunk_size is floored at 1"))
A(_case("stream-bytes-oversize", "dynamic", "GET", "/stream-bytes/999999",
        query="seed=1", headers_skip=("content-length",),
        note="100KB cap on the streaming variant"))

for n in (1, 2, 5, 10, 100):
    A(_case(f"stream-{n}", "dynamic", "GET", f"/stream/{n}",
            headers_skip=("content-length",),
            note=f"{n} newline-delimited JSON objects, id 0..{n - 1}"))
A(_case("stream-0", "dynamic", "GET", "/stream/0",
        headers_skip=("content-length",), note="n<=0 is clamped to 1"))
A(_case("stream-oversize", "dynamic", "GET", "/stream/500",
        headers_skip=("content-length",), note="clamped to 100 lines"))
A(_case("stream-with-query", "dynamic", "GET", "/stream/2", query="a=1&a=2",
        headers_skip=("content-length",),
        note="each streamed object embeds the same args"))

for value in ("SFRUUEJJTiBpcyBhd2Vzb21l", "aGVsbG8=", "", "____"):
    A(_case(f"base64-{value or 'empty'}", "dynamic", "GET",
            f"/base64/{value or '='}",
            note="urlsafe base64 decode, with a fixed fallback on failure"))
A(_case("base64-invalid", "dynamic", "GET", "/base64/!!!!",
        note="undecodable input returns the canned hint string"))
A(_case("base64-nonutf8", "dynamic", "GET", "/base64/__8A",
        note="decodes to bytes that are not UTF-8 -> fallback"))
A(_case("base64-padded", "dynamic", "GET", "/base64/YQ%3D%3D",
        note="percent-encoded padding"))

A(_case("links-1", "dynamic", "GET", "/links/1/0", note="one link page"))
A(_case("links-3-0", "dynamic", "GET", "/links/3/0",
        note="offset 0 is plain text, the others are anchors"))
A(_case("links-3-1", "dynamic", "GET", "/links/3/1", note="offset in the middle"))
A(_case("links-10-9", "dynamic", "GET", "/links/10/9", note="offset at the end"))
A(_case("links-0-0", "dynamic", "GET", "/links/0/0", note="n is clamped to >=1"))
A(_case("links-300-0", "dynamic", "GET", "/links/300/0",
        note="n is clamped to <=200"))
A(_case("links-redirect", "dynamic", "GET", "/links/5",
        note="the two-arg form redirects to offset 0"))
A(_case("links-offset-beyond", "dynamic", "GET", "/links/3/99",
        note="an offset past the end simply never matches"))

A(_case("delay-0", "dynamic", "GET", "/delay/0", note="zero delay"))
A(_case("delay-1", "dynamic", "GET", "/delay/1", note="one second"))
A(_case("delay-fractional", "dynamic", "GET", "/delay/0.25",
        note="fractional delays are allowed"))
A(_case("delay-clamped", "dynamic", "GET", "/delay/999", body_mode="ignore",
        note="clamped to 10s -- excluded from the replay to keep grading quick"))
A(_case("delay-post", "dynamic", "POST", "/delay/0",
        headers=[("Content-Type", "application/json")], body='{"a":1}',
        note="/delay mirrors the request like /get"))
A(_case("delay-notnum", "dynamic", "GET", "/delay/abc", body_mode="ignore",
        note="non-numeric delay -> 500 from float()"))

A(_case("drip-default", "dynamic", "GET", "/drip", query="duration=0&numbytes=10",
        note="instant drip"))
A(_case("drip-code", "dynamic", "GET", "/drip",
        query="duration=0&numbytes=5&code=418", note="drip with a custom code"))
A(_case("drip-delay", "dynamic", "GET", "/drip",
        query="duration=0&numbytes=4&delay=0", note="explicit zero delay"))
A(_case("drip-zero-bytes", "dynamic", "GET", "/drip", query="numbytes=0",
        body_mode="ignore", note="numbytes<=0 -> 400 with a plain-text error"))
A(_case("drip-negative", "dynamic", "GET", "/drip", query="numbytes=-1",
        body_mode="ignore", note="negative numbytes -> 400"))


# ===========================================================================
# 10. Compression
# ===========================================================================

A(_case("gzip", "compress", "GET", "/gzip", body_mode="gzip-json",
        note="gzip: Content-Encoding plus a gzip member that inflates to JSON"))
A(_case("deflate", "compress", "GET", "/deflate", body_mode="deflate-json",
        note="deflate: zlib stream"))
A(_case("brotli", "compress", "GET", "/brotli", body_mode="br-json",
        note="brotli: br stream"))
A(_case("gzip-with-query", "compress", "GET", "/gzip", query="a=1",
        body_mode="gzip-json", note="the inner payload still reports args"))
A(_case("deflate-with-header", "compress", "GET", "/deflate",
        headers=[("X-Custom", "v")], body_mode="deflate-json",
        note="the inner payload still reports headers"))
A(_case("brotli-with-cookie", "compress", "GET", "/brotli",
        headers=[("Cookie", "c=1")], body_mode="br-json",
        note="brotli response for a request carrying a cookie"))


# ===========================================================================
# 11. Range requests
# ===========================================================================

A(_case("range-full", "range", "GET", "/range/26", note="no Range header -> 200"))
A(_case("range-first-10", "range", "GET", "/range/100",
        headers=[("Range", "bytes=0-9")], note="a leading sub-range -> 206"))
A(_case("range-middle", "range", "GET", "/range/100",
        headers=[("Range", "bytes=10-19")], note="a middle sub-range"))
A(_case("range-open-ended", "range", "GET", "/range/100",
        headers=[("Range", "bytes=50-")], note="open-ended range"))
A(_case("range-suffix", "range", "GET", "/range/100",
        headers=[("Range", "bytes=-10")], note="suffix range"))
A(_case("range-whole", "range", "GET", "/range/100",
        headers=[("Range", "bytes=0-99")],
        note="a range covering everything is a 200, not a 206"))
A(_case("range-single-byte", "range", "GET", "/range/100",
        headers=[("Range", "bytes=5-5")], note="one byte"))
A(_case("range-past-end", "range", "GET", "/range/100",
        headers=[("Range", "bytes=90-200")],
        note="last_byte_pos is clamped to the resource size"))
A(_case("range-unsatisfiable", "range", "GET", "/range/100",
        headers=[("Range", "bytes=200-300")], note="-> 416 with Content-Range"))
A(_case("range-inverted", "range", "GET", "/range/100",
        headers=[("Range", "bytes=50-10")], note="first>last -> 416"))
A(_case("range-garbage", "range", "GET", "/range/100",
        headers=[("Range", "not-a-range")],
        note="an unparseable Range is ignored -> full 200"))
A(_case("range-wrong-unit", "range", "GET", "/range/100",
        headers=[("Range", "items=0-9")], note="a non-bytes unit is ignored"))
A(_case("range-multi", "range", "GET", "/range/100",
        headers=[("Range", "bytes=0-9,20-29")],
        note="multipart ranges are not supported; only the first is honoured"))
A(_case("range-empty-suffix", "range", "GET", "/range/100",
        headers=[("Range", "bytes=-")], note="degenerate suffix form"))
A(_case("range-zero-size", "range", "GET", "/range/0", body_mode="ignore",
        note="numbytes<=0 -> 404 with an ETag and Accept-Ranges"))
A(_case("range-oversize", "range", "GET", "/range/999999", body_mode="ignore",
        note="numbytes>100KB -> 404"))
A(_case("range-chunked", "range", "GET", "/range/100", query="chunk_size=7",
        note="chunk_size changes framing only"))
A(_case("range-duration", "range", "GET", "/range/50", query="duration=0",
        note="duration=0 means no pacing"))
A(_case("range-if-range", "range", "GET", "/range/100",
        headers=[("Range", "bytes=0-9"), ("If-Range", "range100")],
        note="If-Range is not implemented and must stay unimplemented"))


# ===========================================================================
# 12. CORS and OPTIONS
# ===========================================================================

A(_case("options-get", "cors", "OPTIONS", "/get",
        headers_extra=("allow",), body_mode="ignore",
        note="preflight for a simple route"))
A(_case("options-post", "cors", "OPTIONS", "/post",
        headers_extra=("allow",), body_mode="ignore", note="preflight for /post"))
A(_case("options-anything", "cors", "OPTIONS", "/anything",
        headers_extra=("allow",), body_mode="ignore",
        note="preflight for the multi-method route"))
A(_case("options-with-origin", "cors", "OPTIONS", "/get",
        headers=[("Origin", "https://example.com")], headers_extra=("allow",),
        body_mode="ignore", note="Allow-Origin mirrors the Origin header"))
A(_case("options-request-headers", "cors", "OPTIONS", "/get",
        headers=[("Origin", "https://example.com"),
                 ("Access-Control-Request-Headers", "X-A, X-B")],
        headers_extra=("allow",), body_mode="ignore",
        note="Allow-Headers echoes Access-Control-Request-Headers"))
A(_case("cors-get-origin", "cors", "GET", "/get",
        headers=[("Origin", "https://example.com")],
        note="a normal GET also gets the CORS headers"))
A(_case("cors-get-no-origin", "cors", "GET", "/get",
        note="without Origin, Allow-Origin falls back to *"))
A(_case("cors-error-origin", "cors", "GET", "/status/418",
        headers=[("Origin", "https://example.com")],
        note="CORS headers are added to error responses too"))
A(_case("cors-404-origin", "cors", "GET", "/definitely-not-a-route",
        headers=[("Origin", "https://example.com")], body_mode="ignore",
        note="even a 404 carries the CORS headers"))


# ===========================================================================
# 13. OpenAPI / docs surface
# ===========================================================================

A(_case("spec-json", "spec", "GET", "/spec.json", body_mode="ignore",
        note="compared structurally against the frozen spec, not byte-wise"))
A(_case("swagger-ui-root", "spec", "GET", "/", body_mode="ignore",
        note="the UI page; only its content type and references are contractual"))


# ===========================================================================
# 14. Not-found and edge cases
# ===========================================================================

A(_case("nf-root-unknown", "edge", "GET", "/no-such-endpoint",
        body_mode="ignore", note="404 for an unknown path"))
A(_case("nf-deep", "edge", "GET", "/a/b/c/d", body_mode="ignore",
        note="404 for a deep unknown path"))
A(_case("nf-trailing", "edge", "GET", "/get/", body_mode="ignore",
        note="a trailing slash on a strict route is a 404"))
A(_case("nf-case", "edge", "GET", "/GET", body_mode="ignore",
        note="routing is case-sensitive"))
A(_case("edge-double-slash", "edge", "GET", "//get",
        # NOT a 404, and the only 200 in this block: the old stack's URL map
        # merges the doubled slash, so this reaches /get and answers 200 with
        # /get's JSON -- whose own `url` field reads `http://__HOST__/get`, the
        # merge visible in the payload.  A router that treats `//get` as a
        # distinct path 404s here.
        #
        # The default `exact`, not `ignore`: the surrounding `ignore`s are all
        # 207-byte framework 404 pages, which is structural's second stated
        # reason.  This body is a deterministic, fully scrubbed /get echo -- it
        # fits none of structural's three reasons, and structural never named it,
        # so under `ignore` its body was compared by nothing at all.  `exact`
        # rather than `json` to match the /get family it echoes, `edge-host-header`
        # included; a lone `json` case here would be the only one in the corpus.
        note="the old stack merges the doubled slash: 200 with /get's body, "
             "whose url reads /get"))
A(_case("edge-long-path", "edge", "GET", "/" + "a" * 300, body_mode="ignore",
        note="a very long path"))
A(_case("edge-query-on-404", "edge", "GET", "/nope", query="a=1",
        body_mode="ignore", note="404 with a query string"))
A(_case("edge-head-get", "edge", "HEAD", "/get",
        note="HEAD returns the headers of GET with no body"))
A(_case("edge-head-json", "edge", "HEAD", "/json", note="HEAD on /json"))
A(_case("edge-head-image", "edge", "HEAD", "/image/png",
        note="HEAD on a binary route"))
A(_case("edge-head-status", "edge", "HEAD", "/status/418", note="HEAD on /status"))
A(_case("edge-host-header", "edge", "GET", "/get",
        headers=[("Host", "example.com")],
        note="the reported url follows the Host header"))
A(_case("edge-expect-continue", "edge", "POST", "/post",
        headers=[("Content-Type", "text/plain")], body="x",
        note="a small POST that some servers would 100-continue"))
A(_case("edge-accept-encoding-identity", "edge", "GET", "/get",
        headers=[("Accept-Encoding", "identity")],
        note="the app must not auto-compress /get"))
A(_case("edge-accept-encoding-gzip", "edge", "GET", "/get",
        headers=[("Accept-Encoding", "gzip")],
        note="Accept-Encoding: gzip must NOT make /get compressed"))
A(_case("edge-if-modified-since-get", "edge", "GET", "/get",
        headers=[("If-Modified-Since", "Sat, 01 Jan 2000 00:00:00 GMT")],
        note="conditional headers are inert outside /cache"))
A(_case("edge-large-body", "edge", "POST", "/post",
        headers=[("Content-Type", "text/plain")], body="y" * 20000,
        note="a 20KB body round-trips"))
A(_case("edge-many-headers", "edge", "GET", "/headers",
        headers=[(f"X-H{i}", str(i)) for i in range(20)],
        note="20 custom headers"))
A(_case("edge-utf8-header", "edge", "GET", "/headers",
        headers=[("X-Latin", "café")],
        note="a non-ASCII header value: latin-1 on the wire, per RFC 7230"))
A(_case("edge-high-byte-header", "edge", "GET", "/headers",
        headers=[("X-Bytes", "ÿþ")],
        note="header bytes above 0x7f survive the round trip"))


# ===========================================================================
# 15. Multipart uploads
# ===========================================================================

_MP_BOUNDARY = "----SWERefactorBoundary7MA4YWxkTrZu0gW"


def _multipart(parts):
    """Build a multipart/form-data body. parts: list of (name, filename, value)."""
    out = []
    for name, filename, value in parts:
        out.append(f"--{_MP_BOUNDARY}\r\n")
        if filename is None:
            out.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n')
        else:
            out.append(
                f'Content-Disposition: form-data; name="{name}"; '
                f'filename="{filename}"\r\n'
                "Content-Type: application/octet-stream\r\n\r\n"
            )
        out.append(value)
        out.append("\r\n")
    out.append(f"--{_MP_BOUNDARY}--\r\n")
    return "".join(out)


_MP_CT = ("Content-Type", f"multipart/form-data; boundary={_MP_BOUNDARY}")

A(_case("mp-single-field", "multipart", "POST", "/post", headers=[_MP_CT],
        body=_multipart([("a", None, "1")]), note="one plain field"))
A(_case("mp-two-fields", "multipart", "POST", "/post", headers=[_MP_CT],
        body=_multipart([("a", None, "1"), ("b", None, "2")]),
        note="two plain fields"))
A(_case("mp-repeated-field", "multipart", "POST", "/post", headers=[_MP_CT],
        body=_multipart([("k", None, "1"), ("k", None, "2")]),
        note="repeated field names collapse to a list"))
A(_case("mp-single-file", "multipart", "POST", "/post", headers=[_MP_CT],
        body=_multipart([("f", "a.txt", "file contents")]),
        note="a file part lands in files, not form"))
A(_case("mp-file-and-field", "multipart", "POST", "/post", headers=[_MP_CT],
        body=_multipart([("f", "a.txt", "data"), ("a", None, "1")]),
        note="files and form are reported separately"))
A(_case("mp-two-files", "multipart", "POST", "/post", headers=[_MP_CT],
        body=_multipart([("f1", "a.txt", "A"), ("f2", "b.txt", "B")]),
        note="two files"))
A(_case("mp-same-name-files", "multipart", "POST", "/post", headers=[_MP_CT],
        body=_multipart([("f", "a.txt", "A"), ("f", "b.txt", "B")]),
        note="two files under one name collapse to a list"))
A(_case("mp-empty-field", "multipart", "POST", "/post", headers=[_MP_CT],
        body=_multipart([("a", None, "")]), note="an empty field value"))
A(_case("mp-empty-file", "multipart", "POST", "/post", headers=[_MP_CT],
        body=_multipart([("f", "empty.txt", "")]), note="an empty file"))
A(_case("mp-utf8-value", "multipart", "POST", "/post", headers=[_MP_CT],
        body=_multipart([("a", None, "中文")]),
        note="UTF-8 in a field value"))
A(_case("mp-utf8-filename", "multipart", "POST", "/post", headers=[_MP_CT],
        body=_multipart([("f", "中.txt", "x")]),
        note="UTF-8 in a filename"))
A(_case("mp-binary-file", "multipart", "POST", "/post", headers=[_MP_CT],
        body=_multipart([("f", "b.bin", "\x00\x01\xff")]),
        note="a file whose bytes are not UTF-8 -> base64 data: URL"))
A(_case("mp-put", "multipart", "PUT", "/put", headers=[_MP_CT],
        body=_multipart([("a", None, "1")]), note="multipart on PUT"))
A(_case("mp-patch", "multipart", "PATCH", "/patch", headers=[_MP_CT],
        body=_multipart([("a", None, "1")]), note="multipart on PATCH"))
A(_case("mp-anything", "multipart", "POST", "/anything", headers=[_MP_CT],
        body=_multipart([("a", None, "1")]), note="multipart on /anything"))
A(_case("mp-no-boundary", "multipart", "POST", "/post",
        headers=[("Content-Type", "multipart/form-data")],
        body=_multipart([("a", None, "1")]), body_mode="ignore",
        note="no boundary parameter: Werkzeug parses nothing and form stays "
             "empty, so the body is compared structurally"))


# ===========================================================================
# Indexes
# ===========================================================================

CASES_BY_ID = {c["id"]: c for c in CASES}
GROUPS = sorted({c["group"] for c in CASES})

assert len(CASES_BY_ID) == len(CASES), "duplicate case id in the corpus"
