"""The fw04 corpus: every profile, every case.

Scope. ChartMuseum v0.15.0 registers ZERO routes with its router. It installs
one catch-all (`NoRoute`) and dispatches with a hand-written `match()` that
splits the URL into a `:repo` of configurable depth -- because the framework it
uses cannot express a top-level wildcard (gin-gonic/gin#388, cited in the
source). Everything interesting about this service's HTTP surface therefore lives
in code the migration has to rewrite, and almost none of it is expressible as a
route table. The corpus is built to pin that down:

*   the depth ladder (0/1/2/3) and dynamic depth, including which URLs are
    ambiguous at each setting;
*   `/api/` detection, which is a prefix test EXCEPT for URLs ending .yaml/.tgz/
    .prov -- so a repo literally named "api" still serves charts;
*   context-path stripping, including that CORS keys off the UNSTRIPPED path;
*   `/health` matching before method dispatch, and `/static` matching by prefix;
*   which routes exist at all under --disable-api / --disable-delete /
    --web-template-path / --artifact-hub-repo-id;
*   the exact error bodies, which are `{"error": "..."}` with Go's own wording;
*   index.yaml bytes, which Helm clients cache by digest.

Every expectation in this file is a REQUEST, not an assertion. What the response
should be is captured from the pinned State A binary; nothing here encodes a
belief about the answer.
"""

from __future__ import annotations

from .profiles import Case, Profile, case_key

# --- frozen chart paths ------------------------------------------------------
# Relative to $CM_ORACLE_CHARTS. Packaged once at image build and never
# repackaged: index.yaml carries the digest of these exact bytes.
MY_001 = "testdata/charts/mychart/mychart-0.0.1.tgz"
MY_010 = "testdata/charts/mychart/mychart-0.1.0.tgz"
MY_020 = "testdata/charts/mychart/mychart-0.2.0.tgz"
MY_010_PROV = "testdata/charts/mychart/mychart-0.1.0.tgz.prov"
MY2_SNAP = "testdata/charts/mychart2/mychart2-0.1.0-SNAPSHOT-1.tgz"
MY2_SNAP_PROV = "testdata/charts/mychart2/mychart2-0.1.0-SNAPSHOT-1.tgz.prov"
OTHER_010 = "testdata/charts/otherchart/otherchart-0.1.0.tgz"
OTHER_010_PROV = "testdata/charts/otherchart/otherchart-0.1.0.tgz.prov"
BAD_100 = "testdata/badcharts/mybadchart/mybadchart-1.0.0.tgz"
BAD_100_PROV = "testdata/badcharts/mybadchart/mybadchart-1.0.0.tgz.prov"

#: A three-version, two-name repository. The workhorse seed.
FULL = f"{MY_001},{MY_010},{MY_020},{MY_010_PROV},{OTHER_010},{OTHER_010_PROV}"
#: Adds the prerelease, which sorts and filters differently.
FULL_PRE = f"{FULL},{MY2_SNAP},{MY2_SNAP_PROV}"

ALWAYS_COMPARED_HEADERS = (
    "content-type",
    "content-length",
    "www-authenticate",
    "access-control-allow-origin",
    "location",
    "allow",
)

PROFILES: list[Profile] = []


def profile(p: Profile) -> Profile:
    PROFILES.append(p)
    return p


# =============================================================================
# Shared case blocks
# =============================================================================

def server_info_cases(prefix: str = "") -> tuple[Case, ...]:
    """The three unauthenticated-ish info routes, at any context path."""
    return (
        Case("health", path=f"{prefix}/health",
             note="matched before method dispatch, ahead of everything else"),
        Case("health-slash", path=f"{prefix}/health/",
             note="RedirectTrailingSlash=false: must 404, not 301"),
        Case("health-head", "HEAD", f"{prefix}/health",
             note="/health matches on GET only; HEAD falls through to match()"),
        Case("info", path=f"{prefix}/info", body_mode="json",
             note="{'version': ...}; pinned by ldflags so it is comparable"),
        Case("welcome", path=f"{prefix}/", note="the built-in HTML welcome page"),
        Case("welcome-head", "HEAD", f"{prefix}/"),
        Case("welcome-post", "POST", f"{prefix}/", data=b"",
             note="no POST / route exists"),
        Case("nonexistent", path=f"{prefix}/nope"),
        Case("nonexistent-deep", path=f"{prefix}/a/b/c/d/e"),
        Case("favicon", path=f"{prefix}/favicon.ico"),
    )


def repo_read_cases(repo: str, *, prefix: str = "") -> tuple[Case, ...]:
    """Reads against a seeded repository, at whatever depth `repo` encodes."""
    r = f"/{repo}" if repo else ""
    p = f"{prefix}{r}"
    api = f"{prefix}/api{r}"
    return (
        Case("index", path=f"{p}/index.yaml", body_mode="index",
             note="the artifact Helm clients byte-compare and cache by digest"),
        Case("index-head", "HEAD", f"{p}/index.yaml", body_mode="ignore"),
        Case("index-trailing", path=f"{p}/index.yaml/"),
        Case("index-query", path=f"{p}/index.yaml?ignored=1", body_mode="index",
             note="the query string is not part of matching"),
        Case("tgz", path=f"{p}/charts/mychart-0.1.0.tgz", body_mode="len",
             note="frozen bytes: content-type and length must agree exactly"),
        Case("tgz-prov", path=f"{p}/charts/mychart-0.1.0.tgz.prov", body_mode="len"),
        Case("tgz-missing", path=f"{p}/charts/nope-9.9.9.tgz"),
        Case("tgz-nested", path=f"{p}/charts/sub/dir/mychart-0.1.0.tgz",
             note="one more path element than the route has params"),
        Case("tgz-traversal", path=f"{p}/charts/..%2F..%2Fetc%2Fpasswd",
             note="an encoded traversal in the :filename param"),
        Case("api-charts", path=f"{api}/charts", body_mode="json"),
        Case("api-charts-offset", path=f"{api}/charts?offset=1", body_mode="json"),
        Case("api-charts-limit", path=f"{api}/charts?limit=1", body_mode="json"),
        Case("api-charts-offset-limit", path=f"{api}/charts?offset=1&limit=1",
             body_mode="json"),
        Case("api-charts-limit-zero", path=f"{api}/charts?limit=0", body_mode="json",
             note="limit<=0 is rejected, unlike offset=0"),
        Case("api-charts-limit-neg", path=f"{api}/charts?limit=-3", body_mode="json"),
        Case("api-charts-limit-junk", path=f"{api}/charts?limit=abc", body_mode="json"),
        Case("api-charts-offset-neg", path=f"{api}/charts?offset=-1", body_mode="json"),
        Case("api-charts-offset-junk", path=f"{api}/charts?offset=x", body_mode="json"),
        Case("api-charts-offset-huge", path=f"{api}/charts?offset=999&limit=5",
             body_mode="json", note="offset past the end: empty, not an error"),
        Case("api-chart", path=f"{api}/charts/mychart", body_mode="json"),
        Case("api-chart-head", "HEAD", f"{api}/charts/mychart", body_mode="ignore"),
        Case("api-chart-missing", path=f"{api}/charts/nosuchchart", body_mode="json"),
        Case("api-chart-missing-head", "HEAD", f"{api}/charts/nosuchchart",
             body_mode="ignore"),
        Case("api-version", path=f"{api}/charts/mychart/0.1.0", body_mode="json"),
        Case("api-version-head", "HEAD", f"{api}/charts/mychart/0.1.0",
             body_mode="ignore"),
        Case("api-version-latest", path=f"{api}/charts/mychart/latest",
             body_mode="json", note="'latest' is rewritten to the empty version"),
        Case("api-version-missing", path=f"{api}/charts/mychart/9.9.9",
             body_mode="json"),
        Case("api-version-missing-head", "HEAD", f"{api}/charts/mychart/9.9.9",
             body_mode="ignore"),
        Case("api-templates", path=f"{api}/charts/mychart/0.1.0/templates",
             body_mode="json"),
        Case("api-values", path=f"{api}/charts/mychart/0.1.0/values",
             body_mode="exact"),
        Case("api-templates-missing", path=f"{api}/charts/mychart/9.9.9/templates",
             body_mode="json"),
        Case("api-values-missing", path=f"{api}/charts/mychart/9.9.9/values",
             body_mode="json"),
        Case("api-unknown-subresource", path=f"{api}/charts/mychart/0.1.0/nope"),
        Case("api-charts-trailing", path=f"{api}/charts/"),
        Case("api-root", path=f"{api}"),
        Case("api-only", path=f"{prefix}/api"),
        Case("api-delete-missing", "DELETE", f"{api}/charts/mychart/9.9.9",
             body_mode="json"),
        Case("api-put", "PUT", f"{api}/charts", data=b"{}",
             note="no PUT route exists at any depth"),
        Case("api-patch", "PATCH", f"{api}/charts/mychart", data=b"{}"),
        Case("api-options", "OPTIONS", f"{api}/charts",
             note="no OPTIONS handler: CORS preflight is not implemented"),
    )


# =============================================================================
# 1. The depth ladder
#
# `match()` slices `depth` path elements out of the URL to form :repo, starting
# after /api for API routes and at index 1 otherwise. Depth is the single most
# load-bearing parameter in the router, and a port that hard-codes route patterns
# instead of re-deriving them per request fails here first.
# =============================================================================

profile(Profile(
    id="depth0",
    note="the default: no :repo component, charts flat in the storage root",
    seeds=(f"={FULL}",),
    # Its writes are all expected refusals, but it still gets private
    # storage: if one is not refused, every later case here would read
    # corrupted storage and the cascade would hide the real defect.
    mutating=True,
    cases=server_info_cases() + repo_read_cases("") + (
        Case("index-with-repo", path="/myrepo/index.yaml",
             note="at depth 0 this is one element too many"),
        Case("api-charts-with-repo", path="/api/myrepo/charts"),
        Case("charts-listing", path="/charts",
             note="no route: /charts is not /:repo/charts at depth 0"),
        Case("statics", path="/static",
             note="/static matches by prefix, but no template path is set"),
        Case("static-file", path="/static/main.css"),
        Case("static-lookalike", path="/staticky",
             note="checkStaticRoute is HasPrefix, not a segment test"),
        Case("artifacthub", path="/artifacthub-repo.yml",
             note="route absent unless --artifact-hub-repo-id is given"),
    ),
))

profile(Profile(
    id="depth1",
    note="one path element of :repo -- the common multitenant setup",
    flags=("--depth=1",),
    seeds=(f"myrepo={FULL}", f"other={OTHER_010}"),
    # Its writes are all expected refusals, but it still gets private
    # storage: if one is not refused, every later case here would read
    # corrupted storage and the cascade would hide the real defect.
    mutating=True,
    cases=server_info_cases() + repo_read_cases("myrepo") + (
        Case("index-other", path="/other/index.yaml", body_mode="index"),
        Case("index-empty-repo", path="/emptyrepo/index.yaml", body_mode="index",
             note="an unseeded repo is an empty index, not a 404"),
        Case("index-root", path="/index.yaml",
             note="at depth 1 the repo element is missing"),
        Case("api-charts-root", path="/api/charts"),
        Case("index-too-deep", path="/a/b/index.yaml"),
        Case("repo-named-api", path="/api/index.yaml", body_mode="index",
             note="THE trap: checkApiRoute excludes *.yaml, so this is the "
                  "index of a repo literally named 'api', not an API route"),
        Case("repo-named-api-tgz", path="/api/charts/mychart-0.1.0.tgz",
             note="...and .tgz is excluded too, so this is repo 'api' as well"),
        Case("repo-named-api-prov", path="/api/charts/mychart-0.1.0.tgz.prov"),
        Case("repo-named-api-api", path="/api/api/charts", body_mode="json",
             note="whereas this one IS an API route, for repo 'api'"),
        Case("repo-named-charts", path="/charts/index.yaml", body_mode="index"),
        Case("repo-named-health", path="/health/index.yaml", body_mode="index",
             note="/health only short-circuits for the exact path"),
        Case("repo-named-static", path="/static/index.yaml",
             note="whereas /static short-circuits on PREFIX, so this one loses"),
        Case("repo-named-info", path="/info/index.yaml", body_mode="index"),
        Case("repo-dotted", path="/my.repo/index.yaml", body_mode="index"),
        Case("repo-encoded-slash", path="/my%2Frepo/index.yaml"),
        Case("repo-unicode", path="/%E6%B5%8B%E8%AF%95/index.yaml",
             body_mode="index"),
    ),
))

profile(Profile(
    id="depth2",
    note="org/repo -- :repo contains a slash, which is why match() exists",
    flags=("--depth=2",),
    seeds=(f"org1/repo1={FULL}", f"org2/repo2={OTHER_010}"),
    # Its writes are all expected refusals, but it still gets private
    # storage: if one is not refused, every later case here would read
    # corrupted storage and the cascade would hide the real defect.
    mutating=True,
    cases=server_info_cases() + repo_read_cases("org1/repo1") + (
        Case("index-other-org", path="/org2/repo2/index.yaml", body_mode="index"),
        Case("index-one-deep", path="/org1/index.yaml"),
        Case("index-three-deep", path="/org1/repo1/extra/index.yaml"),
        Case("api-one-deep", path="/api/org1/charts"),
        Case("api-three-deep", path="/api/org1/repo1/extra/charts"),
        Case("repo-named-api-api", path="/api/api/index.yaml", body_mode="index",
             note="repo 'api/api' at depth 2, not an API route"),
        Case("api-repo-named-api", path="/api/api/api/charts", body_mode="json"),
        Case("index-empty-repo", path="/neworg/newrepo/index.yaml",
             body_mode="index"),
    ),
))

profile(Profile(
    id="depth3",
    note="org/team/repo -- the deepest the source documents",
    flags=("--depth=3",),
    seeds=(f"org/team/repo={FULL}",),
    # Its writes are all expected refusals, but it still gets private
    # storage: if one is not refused, every later case here would read
    # corrupted storage and the cascade would hide the real defect.
    mutating=True,
    cases=server_info_cases() + repo_read_cases("org/team/repo") + (
        Case("index-two-deep", path="/org/team/index.yaml"),
        Case("index-four-deep", path="/org/team/repo/x/index.yaml"),
        Case("api-two-deep", path="/api/org/team/charts"),
    ),
))

profile(Profile(
    id="depth-negative",
    note="a negative depth makes match() return nil for everything except the "
         "short-circuited routes -- an edge the source handles explicitly",
    flags=("--depth=-1",),
    seeds=(f"={FULL}",),
    cases=(
        Case("health", path="/health"),
        Case("info", path="/info", body_mode="json"),
        Case("welcome", path="/"),
        Case("index", path="/index.yaml"),
        Case("api-charts", path="/api/charts", body_mode="json"),
        Case("static", path="/static"),
    ),
))

profile(Profile(
    id="depth-dynamic",
    note="depth is re-derived per request from the route pattern, so one server "
         "answers flat and nested URLs at once",
    flags=("--depth-dynamic",),
    seeds=(f"={FULL}", f"myrepo={OTHER_010}", f"org/repo={MY_020}"),
    cases=server_info_cases() + (
        Case("index-flat", path="/index.yaml", body_mode="index"),
        Case("index-one", path="/myrepo/index.yaml", body_mode="index"),
        Case("index-two", path="/org/repo/index.yaml", body_mode="index"),
        Case("index-three", path="/a/b/c/index.yaml", body_mode="index"),
        Case("index-four", path="/a/b/c/d/index.yaml", body_mode="index"),
        Case("api-charts-flat", path="/api/charts", body_mode="json"),
        Case("api-charts-one", path="/api/myrepo/charts", body_mode="json"),
        Case("api-charts-two", path="/api/org/repo/charts", body_mode="json"),
        Case("api-chart-flat", path="/api/charts/mychart", body_mode="json"),
        Case("api-chart-one", path="/api/myrepo/charts/otherchart", body_mode="json"),
        Case("api-version-two", path="/api/org/repo/charts/mychart/0.2.0",
             body_mode="json"),
        Case("tgz-flat", path="/charts/mychart-0.1.0.tgz", body_mode="len"),
        Case("tgz-one", path="/myrepo/charts/otherchart-0.1.0.tgz", body_mode="len"),
        Case("tgz-two", path="/org/repo/charts/mychart-0.2.0.tgz", body_mode="len"),
        Case("ambiguous-charts", path="/charts/index.yaml", body_mode="index",
             note="does this mean repo 'charts' or the flat /charts route? "
                  "getDepth() decides by first match in route order"),
        Case("api-ambiguous", path="/api/charts/charts", body_mode="json"),
        Case("api-templates-two", path="/api/org/repo/charts/mychart/0.2.0/templates",
             body_mode="json"),
    ),
))


# =============================================================================
# 2. Context path
#
# The prefix is stripped before matching, and the exact-equality branch
# (`url == contextPath` -> "/") is separate from the prefix branch. A request
# that does not carry the prefix at all is refused before any route is consulted.
# =============================================================================

profile(Profile(
    id="contextpath",
    note="every route moves under /cm; a request without the prefix matches "
         "nothing at all",
    flags=("--context-path=/cm",),
    seeds=(f"={FULL}",),
    # Its writes are all expected refusals, but it still gets private
    # storage: if one is not refused, every later case here would read
    # corrupted storage and the cascade would hide the real defect.
    mutating=True,
    cases=server_info_cases("/cm") + repo_read_cases("", prefix="/cm") + (
        Case("bare-health", path="/health",
             note="the short-circuit is INSIDE the strip, so this loses"),
        Case("bare-index", path="/index.yaml"),
        Case("bare-root", path="/"),
        Case("prefix-exact", path="/cm", body_mode="exact",
             note="url == contextPath is rewritten to '/', so this is the "
                  "welcome page -- not a redirect and not a 404"),
        Case("prefix-only-slash", path="/cm/"),
        Case("prefix-partial", path="/c/index.yaml"),
        Case("prefix-doubled", path="/cm/cm/index.yaml"),
        Case("prefix-substring", path="/cmx/index.yaml",
             note="HasPrefix again: /cmx is stripped to x, not refused"),
        Case("prefix-mid-path", path="/x/cm/index.yaml"),
    ),
))

profile(Profile(
    id="contextpath-depth2",
    note="prefix stripping composes with depth: strip first, then split",
    flags=("--context-path=/charts", "--depth=2"),
    seeds=(f"org1/repo1={FULL}",),
    cases=(
        Case("health", path="/charts/health"),
        Case("index", path="/charts/org1/repo1/index.yaml", body_mode="index"),
        Case("tgz", path="/charts/org1/repo1/charts/mychart-0.1.0.tgz",
             body_mode="len",
             note="the word 'charts' appears three times with three meanings"),
        Case("api-charts", path="/charts/api/org1/repo1/charts", body_mode="json"),
        Case("api-version", path="/charts/api/org1/repo1/charts/mychart/0.1.0",
             body_mode="json"),
        Case("bare", path="/org1/repo1/index.yaml"),
        Case("prefix-exact", path="/charts"),
    ),
))


# =============================================================================
# 3. Which routes exist
#
# routes.go assembles the route list conditionally. Under --disable-api the
# entire /api/ family is absent, which is a 404 from `match()` -- not a 405, and
# not a handler that refuses.
# =============================================================================

profile(Profile(
    id="disable-api",
    note="the chart-manipulation routes are never registered",
    flags=("--disable-api",),
    seeds=(f"={FULL}",),
    # Its writes are all expected refusals, but it still gets private
    # storage: if one is not refused, every later case here would read
    # corrupted storage and the cascade would hide the real defect.
    mutating=True,
    cases=(
        Case("health", path="/health"),
        Case("info", path="/info", body_mode="json"),
        Case("index", path="/index.yaml", body_mode="index",
             note="the Helm repository surface is unaffected"),
        Case("tgz", path="/charts/mychart-0.1.0.tgz", body_mode="len"),
        Case("api-charts", path="/api/charts"),
        Case("api-chart", path="/api/charts/mychart"),
        Case("api-chart-head", "HEAD", "/api/charts/mychart", body_mode="ignore"),
        Case("api-version", path="/api/charts/mychart/0.1.0"),
        Case("api-templates", path="/api/charts/mychart/0.1.0/templates"),
        Case("api-values", path="/api/charts/mychart/0.1.0/values"),
        Case("api-post", "POST", "/api/charts", upload=MY2_SNAP),
        Case("api-prov", "POST", "/api/prov", upload=MY2_SNAP_PROV),
        Case("api-delete", "DELETE", "/api/charts/mychart/0.1.0"),
    ),
))

profile(Profile(
    id="disable-delete",
    note="only the DELETE route is withheld; the rest of the API stays",
    flags=("--disable-delete",),
    seeds=(f"={FULL}",),
    # Its writes are all expected refusals, but it still gets private
    # storage: if one is not refused, every later case here would read
    # corrupted storage and the cascade would hide the real defect.
    mutating=True,
    cases=(
        Case("api-charts", path="/api/charts", body_mode="json"),
        Case("api-version", path="/api/charts/mychart/0.1.0", body_mode="json"),
        Case("api-delete", "DELETE", "/api/charts/mychart/0.1.0",
             note="404 from match(), because the route does not exist"),
        Case("api-delete-missing", "DELETE", "/api/charts/nope/9.9.9"),
        Case("index-after-delete-attempt", path="/index.yaml", body_mode="index",
             note="the refused delete must not have removed anything"),
    ),
))

profile(Profile(
    id="artifacthub-depth0",
    note="--artifact-hub-repo-id with a bare value keys the empty repo",
    flags=("--artifact-hub-repo-id=abc-123",),
    seeds=(f"={FULL}",),
    cases=(
        Case("artifacthub", path="/artifacthub-repo.yml", body_mode="exact"),
        Case("artifacthub-head", "HEAD", "/artifacthub-repo.yml", body_mode="ignore"),
        Case("artifacthub-post", "POST", "/artifacthub-repo.yml", data=b""),
        Case("index", path="/index.yaml", body_mode="index"),
    ),
))

profile(Profile(
    id="artifacthub-depth1",
    note="key=value form, one id per repo; an unlisted repo is a 404 from the "
         "handler rather than from match()",
    flags=("--depth=1", "--artifact-hub-repo-id=myrepo=id-for-myrepo",
           "--artifact-hub-repo-id=other=id-for-other"),
    seeds=(f"myrepo={FULL}", f"other={OTHER_010}"),
    cases=(
        Case("myrepo", path="/myrepo/artifacthub-repo.yml", body_mode="exact"),
        Case("other", path="/other/artifacthub-repo.yml", body_mode="exact"),
        Case("unlisted", path="/unlisted/artifacthub-repo.yml", body_mode="json"),
        Case("root", path="/artifacthub-repo.yml"),
    ),
))

profile(Profile(
    id="web-template",
    note="--web-template-path registers /static and swaps the welcome page for "
         "a rendered template",
    flags=("--web-template-path=/opt/webtemplate/template",),
    seeds=(f"={FULL}",),
    cases=(
        Case("welcome", path="/", body_mode="exact",
             note="the rendered index.html, not the built-in page"),
        Case("static-css", path="/static/main.css", body_mode="exact"),
        Case("static-dir", path="/static"),
        Case("static-missing", path="/static/nope.css"),
        Case("static-traversal", path="/static/../../etc/passwd",
             note="c.File() on a path built by string concatenation"),
        Case("static-prefix-lookalike", path="/staticky"),
        Case("static-post", "POST", "/static/main.css", data=b""),
        Case("index", path="/index.yaml", body_mode="index"),
    ),
))

profile(Profile(
    id="web-template-missing",
    note="a path with no index.html falls back to the built-in page, which the "
         "source does explicitly",
    flags=("--web-template-path=/opt/webtemplate/empty",),
    seeds=(),
    cases=(
        Case("welcome", path="/", body_mode="exact"),
        Case("static-css", path="/static/main.css"),
        Case("health", path="/health"),
    ),
))


# =============================================================================
# 4. Authorization
#
# Every route carries an action ("pull"/"push"/""), and rootHandler consults the
# authorizer BEFORE dispatching -- but only for routes whose action is non-empty,
# and only after `match()` has already produced the :repo, because :repo is the
# namespace the decision is made in. So a 404 outranks a 401: an unauthenticated
# request to a route that does not exist is a 404, not a challenge.
# =============================================================================

BASIC = ("--basic-auth-user=myuser", "--basic-auth-pass=mypass")
CRED = "myuser:mypass"

profile(Profile(
    id="basicauth",
    note="pull and push both guarded; /health and /info carry no action at all",
    flags=BASIC,
    seeds=(f"={FULL}",),
    # Its writes are all expected refusals, but it still gets private
    # storage: if one is not refused, every later case here would read
    # corrupted storage and the cascade would hide the real defect.
    mutating=True,
    cases=(
        Case("health-anon", path="/health",
             note="action is '', so no challenge even with an authorizer"),
        Case("info-anon", path="/info", body_mode="json"),
        Case("welcome-anon", path="/", headers_extra=("www-authenticate",),
             note="the welcome page IS a pull action"),
        Case("index-anon", path="/index.yaml", headers_extra=("www-authenticate",)),
        Case("index-auth", path="/index.yaml", auth=CRED, body_mode="index"),
        Case("index-wrong-pass", path="/index.yaml", auth="myuser:nope",
             headers_extra=("www-authenticate",)),
        Case("index-wrong-user", path="/index.yaml", auth="nobody:mypass",
             headers_extra=("www-authenticate",)),
        Case("index-empty-cred", path="/index.yaml", auth=":",
             headers_extra=("www-authenticate",)),
        Case("index-malformed", path="/index.yaml",
             headers=(("Authorization", "Basic !!!not-base64!!!"),),
             headers_extra=("www-authenticate",)),
        Case("index-bearer-instead", path="/index.yaml",
             headers=(("Authorization", "Bearer abcdef"),),
             headers_extra=("www-authenticate",)),
        Case("index-no-scheme", path="/index.yaml",
             headers=(("Authorization", "bXl1c2VyOm15cGFzcw=="),),
             headers_extra=("www-authenticate",)),
        Case("index-lowercase-scheme", path="/index.yaml",
             headers=(("Authorization", "basic bXl1c2VyOm15cGFzcw=="),),
             headers_extra=("www-authenticate",),
             note="RFC 7617 says the scheme is case-insensitive; whether this "
                  "implementation agrees is a captured fact, not an assumption"),
        Case("tgz-anon", path="/charts/mychart-0.1.0.tgz",
             headers_extra=("www-authenticate",)),
        Case("tgz-auth", path="/charts/mychart-0.1.0.tgz", auth=CRED,
             body_mode="len"),
        Case("api-charts-anon", path="/api/charts",
             headers_extra=("www-authenticate",)),
        Case("api-charts-auth", path="/api/charts", auth=CRED, body_mode="json"),
        Case("api-head-anon", "HEAD", "/api/charts/mychart", body_mode="ignore"),
        Case("api-head-auth", "HEAD", "/api/charts/mychart", auth=CRED,
             body_mode="ignore"),
        Case("post-anon", "POST", "/api/charts", upload=MY2_SNAP,
             headers_extra=("www-authenticate",)),
        Case("delete-anon", "DELETE", "/api/charts/mychart/0.1.0",
             headers_extra=("www-authenticate",)),
        Case("missing-route-anon", path="/nope",
             note="404 outranks 401: match() runs first"),
        Case("missing-route-deep-anon", path="/api/charts/mychart/0.1.0/nope"),
        Case("index-anon-then-auth", path="/index.yaml", auth=CRED,
             body_mode="index",
             note="a refused request must not have poisoned the cache"),
    ),
))

profile(Profile(
    id="basicauth-anonymous-get",
    note="--auth-anonymous-get downgrades pull to anonymous while push stays "
         "guarded, so the same URL answers differently by method",
    flags=BASIC + ("--auth-anonymous-get",),
    seeds=(f"={FULL}",),
    # Its writes are all expected refusals, but it still gets private
    # storage: if one is not refused, every later case here would read
    # corrupted storage and the cascade would hide the real defect.
    mutating=True,
    cases=(
        Case("index-anon", path="/index.yaml", body_mode="index"),
        Case("index-auth", path="/index.yaml", auth=CRED, body_mode="index"),
        Case("tgz-anon", path="/charts/mychart-0.1.0.tgz", body_mode="len"),
        Case("api-charts-anon", path="/api/charts", body_mode="json"),
        Case("api-chart-anon", path="/api/charts/mychart", body_mode="json"),
        Case("welcome-anon", path="/"),
        Case("post-anon", "POST", "/api/charts", upload=MY2_SNAP,
             headers_extra=("www-authenticate",),
             note="push is not a pull action, so this is still challenged"),
        Case("prov-anon", "POST", "/api/prov", upload=MY2_SNAP_PROV,
             headers_extra=("www-authenticate",)),
        Case("delete-anon", "DELETE", "/api/charts/mychart/0.1.0",
             headers_extra=("www-authenticate",)),
        Case("delete-auth-nonexistent", "DELETE", "/api/charts/nope/9.9.9",
             auth=CRED, body_mode="json"),
        Case("head-anon", "HEAD", "/api/charts/mychart", body_mode="ignore"),
    ),
))

profile(Profile(
    id="basicauth-depth2",
    note="the namespace the decision is made in is the matched :repo, so the "
         "authorizer cannot run before the router has split the URL",
    flags=BASIC + ("--depth=2",),
    seeds=(f"org1/repo1={FULL}",),
    cases=(
        Case("index-anon", path="/org1/repo1/index.yaml",
             headers_extra=("www-authenticate",)),
        Case("index-auth", path="/org1/repo1/index.yaml", auth=CRED,
             body_mode="index"),
        Case("index-anon-other-ns", path="/org9/repo9/index.yaml",
             headers_extra=("www-authenticate",)),
        Case("index-auth-other-ns", path="/org9/repo9/index.yaml", auth=CRED,
             body_mode="index"),
        Case("wrong-depth-anon", path="/org1/index.yaml",
             note="404 before 401, again"),
        Case("api-anon", path="/api/org1/repo1/charts",
             headers_extra=("www-authenticate",)),
        Case("api-auth", path="/api/org1/repo1/charts", auth=CRED,
             body_mode="json"),
    ),
))

profile(Profile(
    id="bearerauth",
    note="--bearer-auth issues an RFC 6750 challenge naming the realm, service "
         "and scope; the token itself is signed by an authorization server this "
         "environment does not run, so the unauthenticated path is what is graded",
    flags=("--bearer-auth", "--auth-realm=https://auth.example.com/token",
           "--auth-service=chartmuseum.example.com",
           "--auth-cert-path=/opt/testdata/testdata/bearerauth/server.pem"),
    seeds=(f"={FULL}",),
    # Its writes are all expected refusals, but it still gets private
    # storage: if one is not refused, every later case here would read
    # corrupted storage and the cascade would hide the real defect.
    mutating=True,
    cases=(
        Case("health", path="/health"),
        Case("info", path="/info", body_mode="json"),
        Case("index-anon", path="/index.yaml", headers_extra=("www-authenticate",),
             note="the challenge must carry realm, service and scope verbatim"),
        Case("tgz-anon", path="/charts/mychart-0.1.0.tgz",
             headers_extra=("www-authenticate",)),
        Case("api-charts-anon", path="/api/charts",
             headers_extra=("www-authenticate",)),
        Case("post-anon", "POST", "/api/charts", upload=MY2_SNAP,
             headers_extra=("www-authenticate",),
             note="a push scope, so the challenge differs from the pull one"),
        Case("delete-anon", "DELETE", "/api/charts/mychart/0.1.0",
             headers_extra=("www-authenticate",)),
        Case("garbage-token", path="/index.yaml",
             headers=(("Authorization", "Bearer not.a.jwt"),),
             headers_extra=("www-authenticate",)),
        Case("basic-instead", path="/index.yaml", auth=CRED,
             headers_extra=("www-authenticate",)),
        Case("welcome-anon", path="/", headers_extra=("www-authenticate",)),
        Case("missing-anon", path="/nope"),
    ),
))

profile(Profile(
    id="bearerauth-depth2",
    note="the challenge scope embeds the matched namespace, which is derived by "
         "the router",
    flags=("--bearer-auth", "--auth-realm=https://auth.example.com/token",
           "--auth-service=chartmuseum.example.com",
           "--auth-cert-path=/opt/testdata/testdata/bearerauth/server.pem",
           "--depth=2"),
    seeds=(f"org1/repo1={FULL}",),
    # Its writes are all expected refusals, but it still gets private
    # storage: if one is not refused, every later case here would read
    # corrupted storage and the cascade would hide the real defect.
    mutating=True,
    cases=(
        Case("index-anon", path="/org1/repo1/index.yaml",
             headers_extra=("www-authenticate",)),
        Case("index-anon-other", path="/org2/repo2/index.yaml",
             headers_extra=("www-authenticate",)),
        Case("api-anon", path="/api/org1/repo1/charts",
             headers_extra=("www-authenticate",)),
        Case("post-anon", "POST", "/api/org1/repo1/charts", upload=MY2_SNAP,
             headers_extra=("www-authenticate",)),
        Case("root-anon", path="/", headers_extra=("www-authenticate",),
             note="no :repo in the URL, so the namespace falls back to the "
                  "authorizer's default"),
    ),
))


# =============================================================================
# 5. CORS
#
# One header, set in rootHandler, on API routes only -- and `checkApiRoute` is
# given the UNSTRIPPED path, so a context path suppresses it. There is no
# preflight handler at all. This is a small feature with three separate traps in
# it, and a port that reaches for an off-the-shelf CORS middleware gets all three
# wrong at once.
# =============================================================================

profile(Profile(
    id="cors",
    note="Access-Control-Allow-Origin on /api/ only, regardless of Origin",
    flags=("--cors-alloworigin=*",),
    seeds=(f"={FULL}",),
    cases=(
        Case("api-charts", path="/api/charts", body_mode="json",
             headers_extra=("access-control-allow-origin",)),
        Case("api-charts-with-origin", path="/api/charts", body_mode="json",
             headers=(("Origin", "https://example.com"),),
             headers_extra=("access-control-allow-origin",),
             note="the header is unconditional: the request Origin is not read"),
        Case("api-chart", path="/api/charts/mychart", body_mode="json",
             headers_extra=("access-control-allow-origin",)),
        Case("api-missing", path="/api/charts/nope", body_mode="json",
             headers_extra=("access-control-allow-origin",),
             note="set before dispatch, so an error response carries it too"),
        Case("api-preflight", "OPTIONS", "/api/charts",
             headers=(("Origin", "https://example.com"),
                      ("Access-Control-Request-Method", "POST")),
             headers_extra=("access-control-allow-origin", "access-control-allow-methods",
                            "access-control-allow-headers"),
             note="no OPTIONS route exists, so this 404s with no CORS headers"),
        Case("index", path="/index.yaml", body_mode="index",
             headers_extra=("access-control-allow-origin",),
             note="NOT an API route: no CORS header"),
        Case("tgz", path="/charts/mychart-0.1.0.tgz", body_mode="len",
             headers_extra=("access-control-allow-origin",)),
        Case("health", path="/health",
             headers_extra=("access-control-allow-origin",)),
        Case("welcome", path="/", headers_extra=("access-control-allow-origin",)),
        Case("api-yaml-suffix", path="/api/index.yaml",
             headers_extra=("access-control-allow-origin",),
             note="checkApiRoute excludes *.yaml, so no header here either"),
    ),
))

profile(Profile(
    id="cors-origin",
    note="a specific origin is echoed verbatim, with no Vary and no credentials "
         "header",
    flags=("--cors-alloworigin=https://charts.example.com", "--depth=1"),
    seeds=(f"myrepo={FULL}",),
    cases=(
        Case("api-charts", path="/api/myrepo/charts", body_mode="json",
             headers_extra=("access-control-allow-origin", "vary",
                            "access-control-allow-credentials")),
        Case("api-charts-other-origin", path="/api/myrepo/charts", body_mode="json",
             headers=(("Origin", "https://evil.example.com"),),
             headers_extra=("access-control-allow-origin", "vary"),
             note="a mismatched Origin still gets the configured value"),
        Case("index", path="/myrepo/index.yaml", body_mode="index",
             headers_extra=("access-control-allow-origin",)),
    ),
))

profile(Profile(
    id="cors-contextpath",
    note="checkApiRoute sees the path BEFORE the context path is stripped, so "
         "the CORS header silently disappears under --context-path",
    flags=("--cors-alloworigin=*", "--context-path=/cm"),
    seeds=(f"={FULL}",),
    cases=(
        Case("api-charts", path="/cm/api/charts", body_mode="json",
             headers_extra=("access-control-allow-origin",),
             note="the URL starts /cm/, not /api/ -- captured, not assumed"),
        Case("api-chart", path="/cm/api/charts/mychart", body_mode="json",
             headers_extra=("access-control-allow-origin",)),
        Case("index", path="/cm/index.yaml", body_mode="index",
             headers_extra=("access-control-allow-origin",)),
    ),
))


# =============================================================================
# 6. Metrics
#
# --enable-metrics installs a Prometheus middleware that also registers /metrics.
# The counter's `url` label is rewritten back to the route template by a callback
# that reads the params the ROUTER produced, so the label cardinality depends on
# the migration getting param extraction right. Values are not compared -- only
# names, labels, and the exposition format.
# =============================================================================

profile(Profile(
    id="metrics",
    note="/metrics exists and the request counter labels URLs by route template",
    flags=("--enable-metrics", "--depth=1"),
    seeds=(f"myrepo={FULL}",),
    cases=(
        Case("warm-index", path="/myrepo/index.yaml", body_mode="index"),
        Case("warm-tgz", path="/myrepo/charts/mychart-0.1.0.tgz", body_mode="len"),
        Case("warm-api", path="/api/myrepo/charts", body_mode="json"),
        Case("warm-version", path="/api/myrepo/charts/mychart/0.1.0",
             body_mode="json"),
        Case("warm-missing", path="/myrepo/charts/nope-1.0.0.tgz"),
        Case("warm-404", path="/nope"),
        Case("metrics", path="/metrics", body_mode="metrics",
             note="metric names and label sets, not sample values"),
        Case("metrics-head", "HEAD", "/metrics", body_mode="ignore"),
        Case("metrics-post", "POST", "/metrics", data=b""),
        Case("metrics-again", path="/metrics", body_mode="metrics",
             note="scraping must not itself change the label set"),
        Case("index-after-metrics", path="/myrepo/index.yaml", body_mode="index"),
    ),
))

profile(Profile(
    id="metrics-disabled",
    note="the default: /metrics is not a route, so it falls through match()",
    flags=("--depth=1",),
    seeds=(f"myrepo={FULL}",),
    cases=(
        Case("metrics", path="/metrics"),
        Case("index", path="/myrepo/index.yaml", body_mode="index"),
    ),
))


# =============================================================================
# 7. Request size limiting
#
# The size limiter is middleware in State A, and it aborts mid-body. What the
# client sees is therefore a property of middleware ordering, not of a handler:
# the limiter runs BEFORE the authorizer and before the router's dispatch.
# =============================================================================

profile(Profile(
    id="max-upload-size",
    note="a 1 KiB cap, and every frozen chart fits under it -- so what this "
         "profile pins is that installing the limiter changed nothing about the "
         "requests it does not refuse. Measured, and the case names below say "
         "'too-large' for a reason that turned out not to hold: the frozen "
         "charts are 445-999 bytes, so all four uploads here are ACCEPTED. The "
         "refusal path cannot be captured this way -- a body big enough to trip "
         "any cap would have to be a chart the image does not ship -- so it is "
         "graded by the audit family instead, which synthesises bodies at "
         "run time and pins the 413 as a property "
         "(audit/test_retired_middleware.py). The names are kept because "
         "renaming a case rewrites its golden key for no gain.",
    flags=("--max-upload-size=1024",),
    seeds=(f"={FULL}",),
    mutating=True,
    cases=(
        Case("index-before", path="/index.yaml", body_mode="index"),
        Case("post-too-large", "POST", "/api/charts", upload=MY2_SNAP,
             note="under the cap after all, so it is accepted: 201"),
        Case("prov-too-large", "POST", "/api/prov", upload=MY2_SNAP_PROV),
        Case("post-small-invalid", "POST", "/api/charts", data=b"x" * 16,
             body_mode="json",
             note="under the cap, so it reaches the handler and fails there"),
        Case("post-empty", "POST", "/api/charts", data=b"", body_mode="json"),
        Case("get-still-works", path="/index.yaml", body_mode="index",
             note="the limiter must not have broken the read path"),
        Case("multipart-too-large", "POST", "/api/charts",
             multipart=(("chart", MY2_SNAP),)),
    ),
))

profile(Profile(
    id="max-upload-size-generous",
    note="a cap above the chart size, so the same upload succeeds -- the pair "
         "isolates the limiter from everything else that could reject a push",
    flags=("--max-upload-size=1048576",),
    seeds=(f"={FULL}",),
    mutating=True,
    cases=(
        Case("post-ok", "POST", "/api/charts", upload=MY2_SNAP, body_mode="json"),
        Case("index-after", path="/index.yaml", body_mode="index"),
        Case("prov-ok", "POST", "/api/prov", upload=MY2_SNAP_PROV, body_mode="json"),
        Case("api-charts-after", path="/api/charts", body_mode="json"),
    ),
))


# =============================================================================
# 8. Writes
#
# Push, provenance, delete, overwrite, and the storage/chart limits. Every
# profile here is mutating: the sequence is the contract, because an upload
# changes what the next index.yaml contains, and the index is regenerated from
# storage rather than tracked incrementally.
# =============================================================================

profile(Profile(
    id="push",
    note="the classic binary push route, and what the index looks like after",
    seeds=(f"={MY_010}",),
    mutating=True,
    cases=(
        Case("index-before", path="/index.yaml", body_mode="index"),
        Case("api-charts-before", path="/api/charts", body_mode="json"),
        Case("push-new", "POST", "/api/charts", upload=MY_020, body_mode="json"),
        Case("index-after-push", path="/index.yaml", body_mode="index",
             note="the new version must appear, correctly ordered"),
        Case("api-charts-after", path="/api/charts", body_mode="json"),
        Case("get-pushed", path="/charts/mychart-0.2.0.tgz", body_mode="len"),
        Case("api-pushed-version", path="/api/charts/mychart/0.2.0",
             body_mode="json"),
        Case("push-duplicate", "POST", "/api/charts", upload=MY_020,
             body_mode="json", note="409 without --allow-overwrite"),
        Case("push-duplicate-force", "POST", "/api/charts?force", upload=MY_020,
             body_mode="json",
             note="--disable-force-overwrite is off by default, so ?force wins"),
        Case("push-other-name", "POST", "/api/charts", upload=OTHER_010,
             body_mode="json"),
        Case("index-two-names", path="/index.yaml", body_mode="index"),
        Case("push-prerelease", "POST", "/api/charts", upload=MY2_SNAP,
             body_mode="json"),
        Case("index-with-prerelease", path="/index.yaml", body_mode="index",
             note="0.1.0-SNAPSHOT-1 -- prerelease ordering in the index"),
        Case("push-garbage", "POST", "/api/charts", data=b"not a gzip at all",
             body_mode="json",
             note="the error text comes from Go's gzip reader"),
        Case("push-empty", "POST", "/api/charts", data=b"", body_mode="json"),
        Case("push-gzip-not-tar", "POST", "/api/charts",
             data=bytes.fromhex("1f8b08000000000000ff0300000000000000000000"),
             body_mode="json", note="valid gzip, not a tar"),
        Case("push-traversal-name", "POST", "/api/charts", upload=BAD_100,
             body_mode="json",
             note="the chart's own name is ../../../../charts/org2/repo2/evil -- "
                  "this fixture exists because the helm CLI refuses to create it, "
                  "and the path check that stops it lives in the handler"),
        Case("index-after-traversal", path="/index.yaml", body_mode="index",
             note="the refused upload must not have written anything"),
        Case("prov-push", "POST", "/api/prov", upload=MY_010_PROV,
             body_mode="json"),
        Case("get-prov", path="/charts/mychart-0.1.0.tgz.prov", body_mode="len"),
        Case("prov-duplicate", "POST", "/api/prov", upload=MY_010_PROV,
             body_mode="json"),
        Case("prov-garbage", "POST", "/api/prov", data=b"nonsense", body_mode="json"),
        Case("prov-traversal", "POST", "/api/prov", upload=BAD_100_PROV,
             body_mode="json"),
        Case("index-final", path="/index.yaml", body_mode="index"),
    ),
))

profile(Profile(
    id="push-multipart",
    note="the form route: chart and provenance in one request, with a "
         "transactional rollback if either write fails",
    seeds=(),
    mutating=True,
    cases=(
        Case("index-empty", path="/index.yaml", body_mode="index"),
        Case("both", "POST", "/api/charts",
             multipart=(("chart", MY_010), ("prov", MY_010_PROV)),
             body_mode="json"),
        Case("index-after-both", path="/index.yaml", body_mode="index"),
        Case("get-chart", path="/charts/mychart-0.1.0.tgz", body_mode="len"),
        Case("get-prov", path="/charts/mychart-0.1.0.tgz.prov", body_mode="len"),
        Case("chart-only", "POST", "/api/charts", multipart=(("chart", MY_020),),
             body_mode="json"),
        Case("prov-only", "POST", "/api/charts", multipart=(("prov", OTHER_010_PROV),),
             body_mode="json"),
        Case("index-after-prov-only", path="/index.yaml", body_mode="index",
             note="a prov with no chart must not create an index entry"),
        Case("empty-form", "POST", "/api/charts", multipart=(),
             body_mode="json",
             note="the error names both configured form field names"),
        Case("wrong-field", "POST", "/api/charts",
             multipart=(("somethingelse", MY_020),), body_mode="json"),
        Case("duplicate", "POST", "/api/charts", multipart=(("chart", MY_010),),
             body_mode="json"),
        Case("duplicate-force", "POST", "/api/charts?force",
             multipart=(("chart", MY_010),), body_mode="json"),
        Case("traversal", "POST", "/api/charts", multipart=(("chart", BAD_100),),
             body_mode="json"),
        Case("index-final", path="/index.yaml", body_mode="index"),
    ),
))

profile(Profile(
    id="push-custom-form-fields",
    note="the form field names are configurable, and BOTH the default and the "
         "configured name are accepted -- the source tries four field names",
    flags=("--chart-post-form-field-name=mychartfield",
           "--prov-post-form-field-name=myprovfield"),
    seeds=(),
    mutating=True,
    cases=(
        Case("configured-names", "POST", "/api/charts",
             multipart=(("mychartfield", MY_010), ("myprovfield", MY_010_PROV)),
             body_mode="json"),
        Case("index-after", path="/index.yaml", body_mode="index"),
        Case("default-names-still-work", "POST", "/api/charts",
             multipart=(("chart", MY_020),), body_mode="json"),
        Case("index-after-default", path="/index.yaml", body_mode="index"),
        Case("empty-form", "POST", "/api/charts", multipart=(), body_mode="json",
             note="the 400 message interpolates the CONFIGURED names"),
    ),
))

profile(Profile(
    id="delete",
    note="delete removes the chart and silently ignores a missing prov",
    seeds=(f"={FULL_PRE}",),
    mutating=True,
    cases=(
        Case("index-before", path="/index.yaml", body_mode="index"),
        Case("delete", "DELETE", "/api/charts/mychart/0.1.0", body_mode="json"),
        Case("index-after", path="/index.yaml", body_mode="index"),
        Case("get-deleted", path="/charts/mychart-0.1.0.tgz"),
        Case("get-deleted-prov", path="/charts/mychart-0.1.0.tgz.prov",
             note="the prov is deleted alongside, best-effort"),
        Case("api-deleted-version", path="/api/charts/mychart/0.1.0",
             body_mode="json"),
        Case("api-chart-still-there", path="/api/charts/mychart", body_mode="json"),
        Case("delete-again", "DELETE", "/api/charts/mychart/0.1.0", body_mode="json",
             note="the 404 body carries the storage layer's own error text"),
        Case("delete-no-prov", "DELETE", "/api/charts/mychart/0.0.1",
             body_mode="json", note="0.0.1 has a prov; 0.2.0 does not"),
        Case("delete-prerelease", "DELETE", "/api/charts/mychart2/0.1.0-SNAPSHOT-1",
             body_mode="json"),
        Case("index-after-prerelease", path="/index.yaml", body_mode="index"),
        Case("delete-missing-name", "DELETE", "/api/charts/nosuchchart/1.0.0",
             body_mode="json"),
        Case("delete-empty-version", "DELETE", "/api/charts/mychart/",
             body_mode="json"),
        Case("delete-latest", "DELETE", "/api/charts/mychart/latest",
             body_mode="json", note="'latest' is not resolved on the delete path"),
        Case("delete-traversal", "DELETE", "/api/charts/..%2F..%2Fmychart/0.2.0",
             body_mode="json"),
        Case("delete-last", "DELETE", "/api/charts/mychart/0.2.0", body_mode="json"),
        Case("index-nearly-empty", path="/index.yaml", body_mode="index"),
        Case("delete-other", "DELETE", "/api/charts/otherchart/0.1.0",
             body_mode="json"),
        Case("index-empty", path="/index.yaml", body_mode="index",
             note="an emptied repository is still a valid, servable index"),
        Case("api-charts-empty", path="/api/charts", body_mode="json"),
    ),
))

profile(Profile(
    id="delete-depth2",
    note="delete joins :repo into the storage path, so a depth mistake deletes "
         "from the wrong tenant",
    flags=("--depth=2",),
    seeds=(f"org1/repo1={FULL}", f"org2/repo2={FULL}"),
    mutating=True,
    cases=(
        Case("index-org1-before", path="/org1/repo1/index.yaml", body_mode="index"),
        Case("index-org2-before", path="/org2/repo2/index.yaml", body_mode="index"),
        Case("delete-org1", "DELETE", "/api/org1/repo1/charts/mychart/0.1.0",
             body_mode="json"),
        Case("index-org1-after", path="/org1/repo1/index.yaml", body_mode="index"),
        Case("index-org2-after", path="/org2/repo2/index.yaml", body_mode="index",
             note="the other tenant must be untouched"),
        Case("delete-wrong-depth", "DELETE", "/api/org1/charts/mychart/0.2.0",
             note="404 from match(), so nothing is deleted"),
        Case("index-org1-unchanged", path="/org1/repo1/index.yaml", body_mode="index"),
    ),
))

profile(Profile(
    id="allow-overwrite",
    note="--allow-overwrite turns the 409 into a 201 and replaces the bytes",
    flags=("--allow-overwrite",),
    seeds=(f"={MY_010}",),
    mutating=True,
    cases=(
        Case("push-same", "POST", "/api/charts", upload=MY_010, body_mode="json"),
        Case("index-after-same", path="/index.yaml", body_mode="index"),
        Case("push-same-again", "POST", "/api/charts", upload=MY_010,
             body_mode="json"),
        Case("prov-overwrite", "POST", "/api/prov", upload=MY_010_PROV,
             body_mode="json"),
        Case("prov-overwrite-again", "POST", "/api/prov", upload=MY_010_PROV,
             body_mode="json"),
        Case("multipart-overwrite", "POST", "/api/charts",
             multipart=(("chart", MY_010),), body_mode="json"),
        Case("index-final", path="/index.yaml", body_mode="index"),
    ),
))

profile(Profile(
    id="disable-force-overwrite",
    note="?force is honoured by default; this flag withdraws it, so the pair "
         "separates 'force works' from 'force is allowed'",
    flags=("--disable-force-overwrite",),
    seeds=(f"={MY_010}",),
    mutating=True,
    cases=(
        Case("push-dup", "POST", "/api/charts", upload=MY_010, body_mode="json"),
        Case("push-dup-force", "POST", "/api/charts?force", upload=MY_010,
             body_mode="json"),
        Case("push-dup-force-value", "POST", "/api/charts?force=true", upload=MY_010,
             body_mode="json", note="GetQuery only tests presence, not value"),
        Case("push-dup-force-false", "POST", "/api/charts?force=false",
             upload=MY_010, body_mode="json"),
        Case("multipart-dup-force", "POST", "/api/charts?force",
             multipart=(("chart", MY_010),), body_mode="json"),
        Case("index-final", path="/index.yaml", body_mode="index"),
    ),
))

profile(Profile(
    id="max-storage-objects",
    note="a hard object cap returns 507 Insufficient Storage -- an unusual code "
         "that a port is likely to normalise away",
    flags=("--max-storage-objects=2",),
    seeds=(f"={MY_010}",),
    mutating=True,
    cases=(
        Case("push-second", "POST", "/api/charts", upload=MY_020, body_mode="json"),
        Case("push-third", "POST", "/api/charts", upload=MY_001, body_mode="json",
             note="507, not 400 and not 413"),
        Case("prov-over-limit", "POST", "/api/prov", upload=MY_010_PROV,
             body_mode="json"),
        Case("index-after", path="/index.yaml", body_mode="index"),
        Case("delete-then-push", "DELETE", "/api/charts/mychart/0.2.0",
             body_mode="json"),
        Case("push-after-delete", "POST", "/api/charts", upload=MY_001,
             body_mode="json"),
        Case("index-final", path="/index.yaml", body_mode="index"),
    ),
))

profile(Profile(
    id="per-chart-limit",
    note="--per-chart-limit evicts the oldest version of the same chart name on "
         "push, which is a write path with its own locking",
    flags=("--per-chart-limit=2",),
    seeds=(f"={MY_001}",),
    mutating=True,
    cases=(
        Case("push-second", "POST", "/api/charts", upload=MY_010, body_mode="json"),
        Case("index-two", path="/index.yaml", body_mode="index"),
        Case("push-third", "POST", "/api/charts", upload=MY_020, body_mode="json",
             note="at the limit: the oldest must be evicted, not refused"),
        Case("index-after-evict", path="/index.yaml", body_mode="index"),
        Case("api-charts-after-evict", path="/api/charts", body_mode="json"),
        Case("get-evicted", path="/charts/mychart-0.0.1.tgz"),
        Case("push-other-name", "POST", "/api/charts", upload=OTHER_010,
             body_mode="json", note="the limit is per name, not per repo"),
        Case("index-final", path="/index.yaml", body_mode="index"),
    ),
))


# =============================================================================
# 9. What the index says
#
# Everything that changes index.yaml's CONTENT rather than which requests
# succeed. These are the cases where a plausible-looking port produces a
# well-formed index that helm then cannot pull from.
# =============================================================================

profile(Profile(
    id="chart-url",
    note="--chart-url rewrites urls[] to absolute, and the rewrite is applied "
         "in two places (a fresh index and a delta update) that must agree",
    flags=("--chart-url=https://charts.example.com/base",),
    seeds=(f"={FULL}",),
    mutating=True,
    cases=(
        Case("index", path="/index.yaml", body_mode="index",
             note="urls[] absolute, and the trailing slash rule applies"),
        Case("api-charts", path="/api/charts", body_mode="json",
             note="the API surface carries urls[] too"),
        Case("push", "POST", "/api/charts", upload=MY2_SNAP, body_mode="json"),
        Case("index-after-push", path="/index.yaml", body_mode="index",
             note="the delta path must rewrite the new entry the same way"),
        Case("delete", "DELETE", "/api/charts/mychart/0.1.0", body_mode="json"),
        Case("index-after-delete", path="/index.yaml", body_mode="index"),
        Case("get-chart-still-local", path="/charts/mychart-0.2.0.tgz",
             body_mode="len",
             note="--chart-url changes what the index ADVERTISES, not where "
                  "this server serves from"),
    ),
))

profile(Profile(
    id="chart-url-trailing-slash",
    note="the trailing slash is trimmed exactly once, by the outer server",
    flags=("--chart-url=https://charts.example.com/base/",),
    seeds=(f"={MY_010}",),
    cases=(
        Case("index", path="/index.yaml", body_mode="index"),
        Case("api-charts", path="/api/charts", body_mode="json"),
    ),
))

profile(Profile(
    id="chart-url-depth2",
    note="with depth the repo name is appended to --chart-url, so a tenant's "
         "advertised urls[] differ per tenant",
    flags=("--chart-url=https://charts.example.com", "--depth=2"),
    seeds=(f"org1/repo1={FULL}", f"org2/repo2={OTHER_010}"),
    cases=(
        Case("index-org1", path="/org1/repo1/index.yaml", body_mode="index"),
        Case("index-org2", path="/org2/repo2/index.yaml", body_mode="index"),
        Case("api-org1", path="/api/org1/repo1/charts", body_mode="json"),
    ),
))

profile(Profile(
    id="chart-url-contextpath",
    note="--chart-url and --context-path are concatenated for the ServerInfo, "
         "which is a third combination the two call sites must agree on",
    flags=("--chart-url=https://charts.example.com", "--context-path=/cm"),
    seeds=(f"={MY_010}",),
    cases=(
        Case("index", path="/cm/index.yaml", body_mode="index"),
        Case("info", path="/cm/info"),
        Case("api-charts", path="/cm/api/charts", body_mode="json"),
    ),
))

profile(Profile(
    id="disable-statefiles",
    note="statefiles are on by default; with them off the served index must be "
         "identical, which is the point -- the cache file is an optimisation, "
         "not part of the contract",
    flags=("--disable-statefiles",),
    seeds=(f"={FULL}",),
    mutating=True,
    cases=(
        Case("index", path="/index.yaml", body_mode="index"),
        Case("push", "POST", "/api/charts", upload=MY2_SNAP, body_mode="json"),
        Case("index-after-push", path="/index.yaml", body_mode="index"),
        Case("statefile-not-served", path="/index-cache.yaml",
             note="whatever this answers, it must answer the same with and "
                  "without the flag: the name is not a route"),
        Case("delete", "DELETE", "/api/charts/mychart/0.0.1", body_mode="json"),
        Case("index-final", path="/index.yaml", body_mode="index"),
    ),
))

profile(Profile(
    id="statefiles-served-name",
    note="the default-on counterpart: index-cache.yaml is written into the same "
         "storage the chart routes read from, so the pair pins whether a "
         "written statefile becomes visible over HTTP",
    seeds=(f"={FULL}",),
    mutating=True,
    cases=(
        Case("index", path="/index.yaml", body_mode="index"),
        Case("push", "POST", "/api/charts", upload=MY2_SNAP, body_mode="json"),
        Case("index-after-push", path="/index.yaml", body_mode="index"),
        Case("statefile-root", path="/index-cache.yaml"),
        Case("statefile-as-chart", path="/charts/index-cache.yaml"),
        Case("api-charts", path="/api/charts", body_mode="json",
             note="a statefile in storage must not become a chart entry"),
    ),
))

profile(Profile(
    id="index-limit",
    note="--index-limit bounds the fan-out that builds the index; the index it "
         "produces must be identical, and the bound must not deadlock",
    flags=("--index-limit=1",),
    seeds=(f"={FULL_PRE}",),
    cases=(
        Case("index", path="/index.yaml", body_mode="index"),
        Case("index-again", path="/index.yaml", body_mode="index"),
        Case("api-charts", path="/api/charts", body_mode="json"),
        Case("api-chart", path="/api/charts/mychart", body_mode="json"),
    ),
))

profile(Profile(
    id="enforce-semver2",
    note="a deprecated flag: it must still parse and the server must still "
         "boot, because a rewrite that drops a flag breaks every deployment "
         "that sets it",
    flags=("--enforce-semver2",),
    seeds=(f"={FULL_PRE}",),
    mutating=True,
    cases=(
        Case("index", path="/index.yaml", body_mode="index"),
        Case("push-semver-ok", "POST", "/api/charts", upload=MY2_SNAP,
             body_mode="json"),
        Case("api-charts", path="/api/charts", body_mode="json"),
        Case("index-final", path="/index.yaml", body_mode="index"),
    ),
))

profile(Profile(
    id="cache-interval",
    note="a nonzero --cache-interval switches the index off the "
         "regenerate-on-request path entirely (see the CacheInterval == 0 test "
         "in getIndexFile), which changes what an EMPTY repo answers",
    flags=("--cache-interval=24h",),
    seeds=(f"={FULL}",),
    cases=(
        Case("index", path="/index.yaml", body_mode="index",
             note="the boot-time scan populated it, so this is fully served"),
        Case("api-charts", path="/api/charts", body_mode="json"),
        Case("api-chart", path="/api/charts/mychart", body_mode="json"),
        Case("get-chart", path="/charts/mychart-0.1.0.tgz", body_mode="len"),
        Case("index-unknown-repo", path="/nosuchrepo/index.yaml",
             body_mode="index",
             note="depth 0: 'nosuchrepo' is not a repo, so this is a 404 -- "
                  "kept to pin that the flag does not change routing"),
    ),
))

profile(Profile(
    id="cache-interval-empty",
    note="the same flag against empty storage: with the timer on, an empty "
         "repo is NOT regenerated on request",
    flags=("--cache-interval=24h",),
    seeds=(),
    cases=(
        Case("index", path="/index.yaml", body_mode="index"),
        Case("api-charts", path="/api/charts", body_mode="json"),
        Case("api-chart", path="/api/charts/mychart", body_mode="json"),
    ),
))

profile(Profile(
    id="storage-timestamp-tolerance",
    note="the tolerance feeds GetObjectSliceDiff, which decides whether a "
         "rescan counts as a change at all",
    flags=("--storage-timestamp-tolerance=1s",),
    seeds=(f"={FULL}",),
    mutating=True,
    cases=(
        Case("index", path="/index.yaml", body_mode="index"),
        Case("push", "POST", "/api/charts", upload=MY2_SNAP, body_mode="json"),
        Case("index-after-push", path="/index.yaml", body_mode="index"),
        Case("index-again", path="/index.yaml", body_mode="index"),
    ),
))


# =============================================================================
# 10. TLS
#
# The launcher owns --tls-cert/--tls-key and points both sides at the same
# frozen self-signed pair. What is under test is that the server still speaks
# TLS after the rewrite: Gin's engine.RunTLS() has no direct chi equivalent, so
# this is a real hazard, and a submission that quietly serves plaintext on the
# same port fails at the handshake rather than on a body diff.
# =============================================================================

profile(Profile(
    id="tls",
    note="the whole read surface over https",
    tls=True,
    seeds=(f"={FULL}",),
    cases=(
        Case("health", path="/health"),
        Case("info", path="/info"),
        Case("welcome", path="/"),
        Case("index", path="/index.yaml", body_mode="index"),
        Case("chart", path="/charts/mychart-0.1.0.tgz", body_mode="len"),
        Case("prov", path="/charts/mychart-0.1.0.tgz.prov", body_mode="len"),
        Case("api-charts", path="/api/charts", body_mode="json"),
        Case("api-chart", path="/api/charts/mychart", body_mode="json"),
        Case("missing", path="/charts/nope-9.9.9.tgz"),
        Case("nonexistent", path="/nope"),
    ),
))

profile(Profile(
    id="tls-write",
    note="https plus a mutating sequence: a hand-rolled http.Server for TLS is "
         "easy to get wrong in a way only a request BODY reveals",
    tls=True,
    seeds=(f"={MY_010}",),
    mutating=True,
    cases=(
        Case("index-before", path="/index.yaml", body_mode="index"),
        Case("push", "POST", "/api/charts", upload=MY_020, body_mode="json"),
        Case("index-after", path="/index.yaml", body_mode="index"),
        Case("multipart", "POST", "/api/charts", multipart=(("chart", MY_001),),
             body_mode="json"),
        Case("delete", "DELETE", "/api/charts/mychart/0.1.0", body_mode="json"),
        Case("index-final", path="/index.yaml", body_mode="index"),
    ),
))

profile(Profile(
    id="tls-depth2-contextpath",
    note="TLS composed with the two routing features most likely to be "
         "reimplemented, because that is where a port's seams meet",
    tls=True,
    flags=("--depth=2", "--context-path=/cm"),
    seeds=(f"org1/repo1={FULL}",),
    cases=(
        Case("health", path="/cm/health"),
        Case("index", path="/cm/org1/repo1/index.yaml", body_mode="index"),
        Case("chart", path="/cm/org1/repo1/charts/mychart-0.1.0.tgz",
             body_mode="len"),
        Case("api", path="/cm/api/org1/repo1/charts", body_mode="json"),
        Case("outside-contextpath", path="/org1/repo1/index.yaml"),
        Case("wrong-depth", path="/cm/org1/index.yaml"),
    ),
))


# =============================================================================
# 11. Logging that reaches the client
#
# Most log flags only shape stderr, which the runner compares separately. Two
# of them are visible over HTTP: --log-health decides whether /health is logged
# (so the pair pins that /health keeps short-circuiting either way), and
# request logging is where a middleware port drops the request id that ends up
# in a response header.
# =============================================================================

profile(Profile(
    id="log-health",
    note="/health is answered before method dispatch and before logging; this "
         "flag moves it into the log without moving it out of that path",
    flags=("--log-health",),
    seeds=(f"={MY_010}",),
    cases=(
        *server_info_cases(),
        Case("index", path="/index.yaml", body_mode="index"),
        Case("health-again", path="/health"),
    ),
))

profile(Profile(
    id="debug",
    note="--debug raises the log level, which must not change a single "
         "response; it is also the mode in which a port is most likely to leak "
         "a stack trace into a 500 body",
    flags=("--debug",),
    seeds=(f"={FULL}",),
    mutating=True,
    cases=(
        *server_info_cases(),
        Case("index", path="/index.yaml", body_mode="index"),
        Case("api-charts", path="/api/charts", body_mode="json"),
        Case("push-garbage", "POST", "/api/charts", data=b"garbage",
             body_mode="json"),
        Case("push", "POST", "/api/charts", upload=MY2_SNAP, body_mode="json"),
        Case("delete-missing", "DELETE", "/api/charts/nope/1.0.0",
             body_mode="json"),
        Case("index-final", path="/index.yaml", body_mode="index"),
    ),
))

profile(Profile(
    id="log-latency-integer",
    note="a log-format flag with no HTTP surface: the corpus pins that it has "
         "none, so a rewrite cannot pay for a logging change with a header",
    flags=("--log-latency-integer", "--log-json"),
    seeds=(f"={MY_010}",),
    cases=(
        *server_info_cases(),
        Case("index", path="/index.yaml", body_mode="index"),
        Case("chart", path="/charts/mychart-0.1.0.tgz", body_mode="len"),
        Case("missing", path="/charts/nope-1.0.0.tgz"),
    ),
))


# =============================================================================
# 12. Combinations
#
# The features above interact. These profiles exist because a port can satisfy
# every single-feature profile by special-casing each one and still fall apart
# when two of them apply to the same request.
# =============================================================================

profile(Profile(
    id="everything-read",
    note="depth, context path, auth, CORS, metrics, templates and ArtifactHub "
         "all at once, read-only",
    flags=("--depth=2", "--context-path=/cm", "--cors-alloworigin=*",
           "--enable-metrics", "--basic-auth-user=user",
           "--basic-auth-pass=pass", "--auth-anonymous-get",
           "--artifact-hub-repo-id=org1/repo1=abc123",
           "--web-template-path=/opt/webtemplate/template"),
    seeds=(f"org1/repo1={FULL}", f"org2/repo2={OTHER_010}"),
    # Its writes are all expected refusals, but it still gets private
    # storage: if one is not refused, every later case here would read
    # corrupted storage and the cascade would hide the real defect.
    mutating=True,
    cases=(
        Case("health", path="/cm/health"),
        Case("info", path="/cm/info"),
        Case("welcome", path="/cm/", headers_extra=("Access-Control-Allow-Origin",)),
        Case("static", path="/cm/static/main.css"),
        Case("metrics", path="/cm/metrics", body_mode="metrics"),
        Case("index-anon", path="/cm/org1/repo1/index.yaml", body_mode="index",
             headers_extra=("Access-Control-Allow-Origin",)),
        Case("index-auth", path="/cm/org1/repo1/index.yaml", auth="user:pass",
             body_mode="index"),
        Case("artifacthub", path="/cm/org1/repo1/artifacthub-repo.yml"),
        Case("artifacthub-other", path="/cm/org2/repo2/artifacthub-repo.yml"),
        Case("api-anon", path="/cm/api/org1/repo1/charts", body_mode="json"),
        Case("api-post-anon", "POST", "/cm/api/org1/repo1/charts",
             upload=MY2_SNAP,
             note="anonymous-get covers GET only, so this is a 401 and the "
                  "body is not read"),
        Case("wrong-depth", path="/cm/org1/index.yaml"),
        Case("outside-contextpath", path="/org1/repo1/index.yaml"),
        Case("preflight", "OPTIONS", "/cm/org1/repo1/index.yaml",
             headers=(("Origin", "https://example.com"),
                      ("Access-Control-Request-Method", "GET")),
             headers_extra=("Access-Control-Allow-Origin",)),
    ),
))

profile(Profile(
    id="everything-write",
    note="the same stack, mutating, authenticated: the combination a real "
         "deployment runs",
    flags=("--depth=2", "--context-path=/cm", "--cors-alloworigin=*",
           "--enable-metrics", "--basic-auth-user=user",
           "--basic-auth-pass=pass", "--allow-overwrite",
           "--chart-url=https://charts.example.com"),
    seeds=(f"org1/repo1={MY_010}", f"org2/repo2={MY_010}"),
    mutating=True,
    cases=(
        Case("index-before", path="/cm/org1/repo1/index.yaml", auth="user:pass",
             body_mode="index"),
        Case("metrics-before", path="/cm/metrics", auth="user:pass",
             body_mode="metrics"),
        Case("push", "POST", "/cm/api/org1/repo1/charts", auth="user:pass",
             upload=MY_020, body_mode="json"),
        Case("push-overwrite", "POST", "/cm/api/org1/repo1/charts",
             auth="user:pass", upload=MY_020, body_mode="json"),
        Case("index-after", path="/cm/org1/repo1/index.yaml", auth="user:pass",
             body_mode="index", note="urls[] carry --chart-url plus the repo"),
        Case("other-tenant-untouched", path="/cm/org2/repo2/index.yaml",
             auth="user:pass", body_mode="index"),
        Case("prov", "POST", "/cm/api/org1/repo1/prov", auth="user:pass",
             upload=MY_010_PROV, body_mode="json"),
        Case("delete", "DELETE", "/cm/api/org1/repo1/charts/mychart/0.1.0",
             auth="user:pass", body_mode="json"),
        Case("index-final", path="/cm/org1/repo1/index.yaml", auth="user:pass",
             body_mode="index"),
        Case("metrics-after", path="/cm/metrics", auth="user:pass",
             body_mode="metrics",
             note="the per-repo chart gauge must have followed every write"),
    ),
))

profile(Profile(
    id="depth-dynamic-everything",
    note="dynamic depth removes the arithmetic that every other profile pins, "
         "so it is the one place a port can be accidentally right",
    flags=("--depth-dynamic", "--context-path=/cm", "--enable-metrics"),
    seeds=(f"={MY_010}", f"one={MY_020}", f"a/b={OTHER_010}",
           f"a/b/c/d={MY_001}"),
    cases=(
        Case("root-index", path="/cm/index.yaml", body_mode="index"),
        Case("one-index", path="/cm/one/index.yaml", body_mode="index"),
        Case("two-index", path="/cm/a/b/index.yaml", body_mode="index"),
        Case("four-index", path="/cm/a/b/c/d/index.yaml", body_mode="index"),
        Case("partial-index", path="/cm/a/index.yaml", body_mode="index",
             note="'a' is a prefix of a deeper repo, not a repo with charts"),
        Case("api-two", path="/cm/api/a/b/charts", body_mode="json"),
        Case("api-four", path="/cm/api/a/b/c/d/charts", body_mode="json"),
        Case("chart-four", path="/cm/a/b/c/d/charts/mychart-0.0.1.tgz",
             body_mode="len"),
        Case("metrics", path="/cm/metrics", body_mode="metrics",
             note="one series per discovered repo"),
        Case("health", path="/cm/health"),
    ),
))

profile(Profile(
    id="disable-all",
    note="every disabling flag at once: the smallest surface the server can "
         "present, and the one where an over-eager router registers routes "
         "that should not exist",
    flags=("--disable-api", "--disable-delete", "--disable-metrics",
           "--disable-statefiles"),
    seeds=(f"={FULL}",),
    # Its writes are all expected refusals, but it still gets private
    # storage: if one is not refused, every later case here would read
    # corrupted storage and the cascade would hide the real defect.
    mutating=True,
    cases=(
        *server_info_cases(),
        Case("index", path="/index.yaml", body_mode="index"),
        Case("chart", path="/charts/mychart-0.1.0.tgz", body_mode="len"),
        Case("prov", path="/charts/mychart-0.1.0.tgz.prov", body_mode="len"),
        Case("api-charts", path="/api/charts"),
        Case("api-post", "POST", "/api/charts", upload=MY2_SNAP),
        Case("api-delete", "DELETE", "/api/charts/mychart/0.1.0"),
        Case("metrics", path="/metrics"),
        Case("static", path="/static/main.css"),
        Case("artifacthub", path="/artifacthub-repo.yml"),
    ),
))


# =============================================================================
# Accessors
# =============================================================================

def by_id(profile_id: str) -> Profile:
    for p in PROFILES:
        if p.id == profile_id:
            return p
    raise KeyError(profile_id)


def all_cases() -> list[tuple[str, Case]]:
    return [(p.id, c) for p in PROFILES for c in p.cases]


def fingerprint() -> str:
    """A digest of every request in the corpus, and of how it is compared.

    Case ids alone are not enough: editing a case's path, flags or seeds while
    keeping its id would leave the golden file looking complete while grading a
    request that was never captured. The digest is stored in the golden file and
    checked before grading, so that mistake fails loudly instead of quietly.
    """
    import hashlib

    h = hashlib.sha256()
    for p in PROFILES:
        h.update(f"P|{p.id}|{p.flags}|{p.seeds}|{p.mutating}|{p.tls}\n".encode())
        for c in p.cases:
            h.update(
                f"C|{c.id}|{c.method}|{c.path}|{c.data!r}|{c.multipart}|"
                f"{c.upload}|{c.headers}|{c.auth}|{c.body_mode}|{c.prefix_len}|"
                f"{c.headers_extra}|{c.headers_skip}\n".encode())
    return h.hexdigest()[:16]


def _selfcheck() -> None:
    """Cheap structural invariants, run on import.

    A corpus that silently contains two profiles with one id, or a case that
    uploads a chart the image does not ship, produces a golden file with a
    missing entry and a grading run that fails for the wrong reason. Failing at
    import is louder and lands in the capture, not the grade.
    """
    ids = [p.id for p in PROFILES]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise ValueError(f"duplicate profile ids: {dupes}")
    for p in PROFILES:
        cids = [c.id for c in p.cases]
        cdupes = sorted({i for i in cids if cids.count(i) > 1})
        if cdupes:
            raise ValueError(f"{p.id}: duplicate case ids: {cdupes}")
        if not p.cases:
            raise ValueError(f"{p.id}: no cases")
        for c in p.cases:
            if not c.path.startswith("/"):
                raise ValueError(f"{p.id}::{c.id}: path must be absolute")
            charts = [c.upload] if c.upload else [ch for _, ch in c.multipart]
            for ch in charts:
                if not ch.startswith("testdata/"):
                    raise ValueError(
                        f"{p.id}::{c.id}: {ch!r} is not under testdata/")
        # A request that could plausibly land forces a private, freshly seeded
        # storage. "Could plausibly land" is deliberately decided from the
        # REQUEST alone -- carries a chart body, or deletes a chart -- because
        # deciding it from the expected response would mean encoding the answer
        # here, and because marking an extra profile mutating costs only a
        # directory. A bodiless POST / or a POST to a route that does not exist
        # cannot write, so it does not force one.
        risky = [c for c in p.cases
                 if (c.upload or c.multipart
                     or (c.method == "DELETE" and "/charts/" in c.path))]
        if risky and not p.mutating:
            raise ValueError(
                f"{p.id}: {risky[0].id} could write to storage but the profile "
                f"is not marked mutating")


_selfcheck()
