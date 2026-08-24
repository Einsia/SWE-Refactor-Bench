"""Every request this stage makes, and how each response is compared.

One list, used three times: to record State A's answers, to replay the same
requests against the submission, and to enumerate the tests. No expected value
lives in a test file -- the tests read ``data/recorded-responses.json``, which
this list produced.

A request plus the answer it got is an *exchange*, and an exchange is the unit
everything here is counted in. Exchanges are grouped into sessions (see
``sessions.py``) because json-server is stateful: a POST changes what the next
GET sees, so replay order is part of the ground truth and a mutating group cannot
share a server with anything else.

Nothing in this file reads the submission. The requests are the same requests
whatever is answering them, which is what makes the two recordings comparable.
"""

from __future__ import annotations

from .sessions import Case, Session

JSON = (("Accept", "application/json"),)
GZIP = (("Accept-Encoding", "gzip"),)


def _c(cid: str, path: str, **kw) -> Case:
    return Case(id=cid, path=path, **kw)


# ---------------------------------------------------------------------------
# Session: read -- the default configuration, read-only requests only.
# ---------------------------------------------------------------------------

def _read_cases() -> list[Case]:
    cases: list[Case] = []
    add = cases.append

    # -- whole collections ---------------------------------------------------
    for res in ("posts", "comments", "authors", "users", "notes"):
        add(_c(f"list-{res}", f"/{res}"))
    add(_c("list-posts-trailing-slash", "/posts/"))

    # -- singular resources --------------------------------------------------
    add(_c("singular-profile", "/profile"))
    add(_c("singular-settings", "/settings"))

    # -- item lookups --------------------------------------------------------
    for pid in (1, 2, 7, 12, 23, 24):
        add(_c(f"item-post-{pid}", f"/posts/{pid}"))
    add(_c("item-post-missing", "/posts/9999"))
    add(_c("item-post-zero", "/posts/0"))
    add(_c("item-post-negative", "/posts/-1"))
    add(_c("item-post-noninteger", "/posts/abc"))
    add(_c("item-post-float", "/posts/1.5"))
    add(_c("item-comment-5", "/comments/5"))
    add(_c("item-author-3", "/authors/3"))
    add(_c("item-user-11", "/users/11"))
    # notes carry string ids, so lookup must not coerce to a number
    for nid in ("n-a", "n-c", "n-e"):
        add(_c(f"item-note-{nid}", f"/notes/{nid}"))
    add(_c("item-note-numeric-lookalike", "/notes/1"))

    # -- equality filters ----------------------------------------------------
    add(_c("filter-category-tech", "/posts?category=tech"))
    add(_c("filter-category-life", "/posts?category=life"))
    add(_c("filter-category-missing", "/posts?category=nosuch"))
    add(_c("filter-views-exact", "/posts?views=149"))
    add(_c("filter-published-true", "/posts?published=true"))
    add(_c("filter-published-false", "/posts?published=false"))
    add(_c("filter-authorId-2", "/posts?authorId=2"))
    add(_c("filter-multi-value-id", "/posts?id=1&id=3&id=5"))
    add(_c("filter-multi-value-category", "/posts?category=tech&category=life"))
    add(_c("filter-two-fields", "/posts?category=tech&published=true"))
    add(_c("filter-unknown-field", "/posts?nosuchfield=1"))
    add(_c("filter-empty-value", "/posts?category="))
    add(_c("filter-on-comments-postId", "/comments?postId=1"))
    add(_c("filter-comments-flagged", "/comments?flagged=false"))
    add(_c("filter-authors-active", "/authors?active=true"))
    add(_c("filter-authors-inactive", "/authors?active=false"))
    add(_c("filter-array-member", "/posts?tags=ops",
           note="tags is an array; equality does not look inside it"))
    add(_c("filter-users-city", "/users?city=Osaka"))
    add(_c("filter-users-age", "/users?age=31"))

    # -- deep (dotted) queries ----------------------------------------------
    add(_c("deep-meta-reviewer", "/posts?meta.reviewer=ada"))
    add(_c("deep-meta-rating", "/posts?meta.rating=3.2"))
    add(_c("deep-meta-slug", "/posts?meta.slug=sourdough-at-altitude"))
    add(_c("deep-settings-flag", "/posts?meta.nosuch=x"))
    add(_c("deep-missing-root", "/posts?author.name=ada"))
    add(_c("deep-users-role", "/users?roles=reader"))

    # -- range and negation operators ---------------------------------------
    add(_c("op-views-gte", "/posts?views_gte=600"))
    add(_c("op-views-lte", "/posts?views_lte=100"))
    add(_c("op-views-gte-lte", "/posts?views_gte=200&views_lte=400"))
    add(_c("op-views-ne", "/posts?views_ne=149"))
    add(_c("op-id-gte", "/posts?id_gte=20"))
    add(_c("op-category-ne", "/posts?category_ne=tech"))
    add(_c("op-title-like", "/posts?title_like=the"))
    add(_c("op-title-like-anchored", "/posts?title_like=^A"))
    add(_c("op-title-like-regex", "/posts?title_like=s$"))
    add(_c("op-title-like-nomatch", "/posts?title_like=zzzz"))
    add(_c("op-body-like", "/comments?body_like=the"))
    add(_c("op-likes-gte", "/comments?likes_gte=40"))
    add(_c("op-gte-on-string", "/notes?id_gte=n-c"))
    add(_c("op-rating-gte-nested", "/posts?meta.rating_gte=4.5"))
    add(_c("op-two-ops-one-field", "/posts?views_ne=149&views_gte=100"))
    add(_c("op-lte-nonnumeric", "/posts?views_lte=abc"))

    # -- full text search ----------------------------------------------------
    add(_c("q-simple", "/posts?q=slow"))
    add(_c("q-case-insensitive", "/posts?q=SLOW"))
    add(_c("q-across-fields", "/posts?q=sourdough"))
    add(_c("q-matches-nested-value", "/posts?q=franklin",
           note="q searches nested values too, so meta.reviewer matches"))
    add(_c("q-numeric", "/posts?q=149"))
    add(_c("q-nomatch", "/posts?q=zzzzz"))
    add(_c("q-empty", "/posts?q="))
    add(_c("q-on-comments", "/comments?q=disagree"))
    add(_c("q-on-notes", "/notes?q=timetable"))
    add(_c("q-with-filter", "/posts?q=the&category=tech"))
    add(_c("q-multi-value", "/posts?q=slow&q=train"))

    # -- sorting -------------------------------------------------------------
    add(_c("sort-views", "/posts?_sort=views"))
    add(_c("sort-views-desc", "/posts?_sort=views&_order=desc"))
    add(_c("sort-title", "/posts?_sort=title"))
    add(_c("sort-title-desc", "/posts?_sort=title&_order=DESC"))
    add(_c("sort-two-keys", "/posts?_sort=category,views"))
    add(_c("sort-two-keys-mixed-order",
           "/posts?_sort=category,views&_order=asc,desc"))
    add(_c("sort-nested-key", "/posts?_sort=meta.rating"))
    add(_c("sort-nested-key-desc", "/posts?_sort=meta.reviewer&_order=desc"))
    add(_c("sort-boolean", "/posts?_sort=published"))
    add(_c("sort-comments-likes", "/comments?_sort=likes&_order=desc"))
    add(_c("sort-unknown-key", "/posts?_sort=nosuch"))
    add(_c("sort-bad-order", "/posts?_sort=views&_order=sideways"))
    add(_c("sort-with-filter", "/posts?category=tech&_sort=views&_order=desc"))
    add(_c("sort-notes-string-id", "/notes?_sort=id&_order=desc"))

    # -- pagination ----------------------------------------------------------
    add(_c("page-1", "/posts?_page=1", headers_extra=("X-Total-Count", "Link")))
    add(_c("page-2", "/posts?_page=2", headers_extra=("X-Total-Count", "Link")))
    add(_c("page-3", "/posts?_page=3", headers_extra=("X-Total-Count", "Link")))
    add(_c("page-last", "/posts?_page=3&_limit=10",
           headers_extra=("X-Total-Count", "Link")))
    add(_c("page-past-end", "/posts?_page=99",
           headers_extra=("X-Total-Count", "Link"),
           note="Link is present but empty past the last page"))
    add(_c("page-zero", "/posts?_page=0",
           headers_extra=("X-Total-Count", "Link")))
    add(_c("page-negative", "/posts?_page=-1",
           headers_extra=("X-Total-Count", "Link")))
    add(_c("page-nonnumeric", "/posts?_page=abc",
           headers_extra=("X-Total-Count", "Link"),
           note="_page is echoed literally into all three Link rels"))
    add(_c("page-limit-5", "/posts?_page=2&_limit=5",
           headers_extra=("X-Total-Count", "Link")))
    add(_c("page-limit-1", "/posts?_page=1&_limit=1",
           headers_extra=("X-Total-Count", "Link")))
    add(_c("page-limit-huge", "/posts?_page=1&_limit=1000",
           headers_extra=("X-Total-Count", "Link")))
    add(_c("page-limit-zero", "/posts?_page=1&_limit=0",
           headers_extra=("X-Total-Count", "Link")))
    add(_c("page-with-filter", "/posts?category=tech&_page=1&_limit=2",
           headers_extra=("X-Total-Count", "Link")))
    add(_c("page-with-sort", "/posts?_sort=views&_order=desc&_page=2&_limit=4",
           headers_extra=("X-Total-Count", "Link")))
    add(_c("page-comments", "/comments?_page=2&_limit=7",
           headers_extra=("X-Total-Count", "Link")))

    # -- slicing (no pagination metadata, but X-Total-Count) -----------------
    add(_c("slice-start-end", "/posts?_start=5&_end=10",
           headers_extra=("X-Total-Count",)))
    add(_c("slice-start-limit", "/posts?_start=5&_limit=3",
           headers_extra=("X-Total-Count",)))
    add(_c("slice-end-only", "/posts?_end=4",
           headers_extra=("X-Total-Count",)))
    add(_c("slice-start-only", "/posts?_start=20",
           headers_extra=("X-Total-Count",)))
    add(_c("slice-start-past-end", "/posts?_start=500",
           headers_extra=("X-Total-Count",)))
    add(_c("slice-end-before-start", "/posts?_start=10&_end=2",
           headers_extra=("X-Total-Count",)))
    add(_c("slice-negative-start", "/posts?_start=-3",
           headers_extra=("X-Total-Count",)))
    add(_c("slice-limit-only", "/posts?_limit=3",
           headers_extra=("X-Total-Count",)))
    add(_c("slice-with-sort", "/posts?_sort=id&_order=desc&_start=2&_end=6",
           headers_extra=("X-Total-Count",)))

    # -- relationships -------------------------------------------------------
    add(_c("embed-comments", "/posts?_embed=comments"))
    add(_c("embed-comments-item", "/posts/1?_embed=comments"))
    add(_c("embed-notes-string-ids", "/posts?_embed=notes"))
    add(_c("embed-item-no-children", "/users/1?_embed=posts",
           note="users are referenced by nothing, so the embedded list is empty"))
    add(_c("embed-unknown", "/posts?_embed=nosuch"))
    add(_c("embed-two-children", "/posts/1?_embed=comments&_embed=notes"))
    add(_c("expand-author", "/posts?_expand=author"))
    add(_c("expand-author-item", "/posts/1?_expand=author"))
    add(_c("expand-missing-fk", "/users?_expand=author",
           body_mode="stack",
           note="users carry no authorId, so _expand calls getById(undefined) "
                "and lodash-id throws: 500, not an untouched list"))
    add(_c("expand-unknown", "/posts?_expand=nosuch"))
    add(_c("embed-and-expand", "/posts?_embed=comments&_expand=author"))
    add(_c("embed-and-expand-item", "/posts/1?_embed=comments&_expand=author"))
    add(_c("expand-post-on-comment", "/comments?_expand=post"))
    add(_c("expand-two-parents", "/comments/1?_expand=post&_expand=author"))
    add(_c("expand-post-on-note", "/notes?_expand=post"))
    add(_c("embed-with-filter", "/posts?category=tech&_embed=comments"))
    add(_c("embed-with-page", "/posts?_embed=comments&_page=1&_limit=3",
           headers_extra=("X-Total-Count", "Link")))

    # -- nested routes -------------------------------------------------------
    add(_c("nested-post-comments", "/posts/1/comments"))
    add(_c("nested-post-notes", "/posts/1/notes"))
    add(_c("nested-empty-child", "/users/1/posts"))
    add(_c("nested-post-comments-missing-parent", "/posts/9999/comments"))
    add(_c("nested-author-posts", "/authors/1/posts"))
    add(_c("nested-with-filter", "/posts/1/comments?flagged=false"))
    add(_c("nested-with-sort", "/comments/1/nosuch"))
    add(_c("nested-author-posts-sorted",
           "/authors/2/posts?_sort=views&_order=desc"))
    add(_c("nested-with-page", "/authors/1/posts?_page=1&_limit=2",
           headers_extra=("X-Total-Count", "Link")))
    add(_c("nested-unknown-child", "/posts/1/nosuch"))

    # -- the whole database --------------------------------------------------
    add(_c("db-dump", "/db"))

    # -- static files and the index -----------------------------------------
    add(_c("static-index", "/", headers_skip=("ETag", "Last-Modified")))
    add(_c("static-index-explicit", "/index.html",
           headers_skip=("ETag", "Last-Modified")))
    add(_c("static-missing", "/nosuch.html"))
    add(_c("static-dotdot", "/../package.json"))

    # -- rewriter ------------------------------------------------------------
    add(_c("rules-listing", "/__rules"))
    add(_c("rewrite-api-root", "/api/"))
    add(_c("rewrite-api-splat-posts", "/api/posts"))
    add(_c("rewrite-api-splat-item", "/api/posts/2"))
    add(_c("rewrite-api-splat-query", "/api/posts?category=tech"))
    add(_c("rewrite-blog-show", "/blog/posts/3/show"))
    add(_c("rewrite-blog-category", "/blog/tech"))
    add(_c("rewrite-v1-note", "/v1/notes/n-a"))
    add(_c("rewrite-search", "/search/slow"))
    add(_c("rewrite-leaderboard", "/leaderboard",
           headers_extra=("X-Total-Count", "Link")))
    add(_c("rewrite-unmatched", "/apiX/posts"))

    # -- content negotiation and conditional requests -----------------------
    add(_c("accept-json", "/posts/1", headers=JSON))
    add(_c("accept-any", "/posts/1", headers=(("Accept", "*/*"),)))
    add(_c("accept-html", "/posts/1", headers=(("Accept", "text/html"),)))
    add(_c("gzip-list", "/posts", headers=GZIP, decompress="gzip"))
    add(_c("gzip-db", "/db", headers=GZIP, decompress="gzip"))
    add(_c("deflate-list", "/posts", headers=(("Accept-Encoding", "deflate"),),
           decompress="deflate"))
    add(_c("br-list", "/posts", headers=(("Accept-Encoding", "br"),),
           note="br is not offered; the response falls through to identity"))
    add(_c("identity-list", "/posts",
           headers=(("Accept-Encoding", "identity"),)))
    add(_c("no-accept-encoding-small", "/posts/1"))

    # -- CORS ----------------------------------------------------------------
    add(_c("cors-simple", "/posts/1",
           headers=(("Origin", "http://example.test"),),
           headers_extra=("Access-Control-Allow-Origin",
                          "Access-Control-Allow-Credentials",
                          "Vary")))
    add(Case(id="cors-preflight", method="OPTIONS", path="/posts",
             headers=(("Origin", "http://example.test"),
                      ("Access-Control-Request-Method", "POST"),
                      ("Access-Control-Request-Headers", "content-type")),
             headers_extra=("Access-Control-Allow-Origin",
                            "Access-Control-Allow-Methods",
                            "Access-Control-Allow-Headers",
                            "Access-Control-Allow-Credentials",
                            "Vary")))
    add(Case(id="cors-preflight-no-origin", method="OPTIONS", path="/posts"))

    # -- caching -------------------------------------------------------------
    # -- conditional requests ------------------------------------------------
    # The ETags below are the ones State A computes for these exact bodies. A
    # 304 here therefore says two things at once: the conditional handling
    # works, *and* the tag was derived the same way. Express's tag is
    # `W/"<byte length in hex>-<base64 sha1 of the body>"`; nothing in the
    # target dependency set computes that, so it has to be implemented.
    add(_c("cond-if-none-match-hit", "/posts/1",
           headers=(("If-None-Match", 'W/"11e-RC3ZoFAIklFHVkqa9flNURTk/rQ"'),),
           headers_extra=("ETag",),
           note="matching tag -> 304 with no body"))
    add(_c("cond-if-none-match-miss", "/posts/1",
           headers=(("If-None-Match", 'W/"not-the-tag"'),),
           headers_extra=("ETag",)))
    add(_c("cond-if-none-match-star", "/posts/1",
           headers=(("If-None-Match", "*"),), headers_extra=("ETag",)))
    add(_c("cond-if-none-match-list", "/posts/1",
           headers=(("If-None-Match",
                     'W/"other", W/"11e-RC3ZoFAIklFHVkqa9flNURTk/rQ"'),),
           headers_extra=("ETag",)))
    add(_c("cond-if-none-match-strong-form", "/posts/1",
           headers=(("If-None-Match", '"11e-RC3ZoFAIklFHVkqa9flNURTk/rQ"'),),
           headers_extra=("ETag",),
           note="strong form of a weak tag does not match"))
    add(_c("cond-if-none-match-list-body", "/posts",
           headers=(("If-None-Match", 'W/"1d18-13KYbRhTSlfYVd7xG3/Y9DU3BDY"'),),
           headers_extra=("ETag",)))
    add(_c("cond-if-none-match-404-body", "/posts/9999",
           headers=(("If-None-Match", 'W/"2-vyGp6PvFo4RvsFtPoIWeCReyIC8"'),),
           headers_extra=("ETag",),
           note="the 404 body is `{}`, which also has a tag"))
    add(Case(id="cond-head-if-none-match", method="HEAD", path="/posts/1",
             headers=(("If-None-Match", 'W/"11e-RC3ZoFAIklFHVkqa9flNURTk/rQ"'),),
             headers_extra=("ETag",)))
    add(_c("cond-if-modified-since-static", "/index.html",
           headers=(("If-Modified-Since", "Fri, 29 Sep 2023 00:00:00 GMT"),),
           headers_skip=("ETag", "Last-Modified")))
    add(_c("cond-if-modified-since-old", "/index.html",
           headers=(("If-Modified-Since", "Fri, 01 Jan 2010 00:00:00 GMT"),),
           headers_skip=("ETag", "Last-Modified")))
    add(_c("cond-if-none-match-static", "/index.html",
           headers=(("If-None-Match", 'W/"809-18ade3c2c00"'),),
           headers_skip=("ETag", "Last-Modified"),
           note="static tags are size-mtime, not content-derived"))

    add(_c("cache-control-json", "/posts/1", headers_extra=("Cache-Control",)))
    add(_c("cache-control-list", "/posts", headers_extra=("Cache-Control",)))
    add(_c("etag-json-item", "/posts/1", headers_extra=("ETag",)))
    add(_c("etag-json-list", "/posts", headers_extra=("ETag",)))
    add(Case(id="head-item", method="HEAD", path="/posts/1",
             headers_extra=("ETag",)))
    add(Case(id="head-list", method="HEAD", path="/posts"))
    add(Case(id="head-missing", method="HEAD", path="/posts/9999"))

    # -- method handling on read-only-shaped requests -----------------------
    add(Case(id="options-item", method="OPTIONS", path="/posts/1",
             headers_extra=("Allow",)))
    add(Case(id="trace-item", method="TRACE", path="/posts/1"))
    add(Case(id="unknown-method-item", method="PROPFIND", path="/posts/1"))

    # -- malformed and hostile URLs -----------------------------------------
    add(_c("url-double-slash", "//posts"))
    add(_c("url-encoded-slash", "/posts%2F1"))
    add(_c("url-percent-space", "/posts?title=Hello%20World"))
    add(_c("url-plus-space", "/posts?title=Hello+World"))
    add(_c("url-unicode", "/posts?q=%E6%97%A5%E6%9C%AC"))
    add(_c("url-bad-percent", "/posts%zz"))
    add(_c("url-very-long", "/posts?q=" + "a" * 400))
    add(_c("url-repeated-underscore-opts", "/posts?_limit=2&_limit=3",
           headers_extra=("X-Total-Count",)))
    add(_c("url-bracket-query", "/posts?a[b]=c"))
    add(_c("url-array-query", "/posts?id[]=1&id[]=2"))
    add(_c("url-query-no-value", "/posts?justakey"))
    add(_c("url-fragmentish", "/posts?q=a%23b"))

    return cases


# ---------------------------------------------------------------------------
# Mutating sessions
# ---------------------------------------------------------------------------

def _create_cases() -> list[Case]:
    """POST: auto ids, explicit ids, string ids, and the duplicate-id error."""
    cases: list[Case] = []
    add = cases.append

    add(Case(id="create-minimal", method="POST", path="/posts",
             json={"title": "Created A"},
             headers_extra=("Location", "Access-Control-Expose-Headers")))
    add(Case(id="create-full", method="POST", path="/posts",
             json={"title": "Created B", "views": 7, "category": "tech",
                   "published": True, "authorId": 1},
             headers_extra=("Location",)))
    add(Case(id="create-nested-object", method="POST", path="/posts",
             json={"title": "Created C",
                   "meta": {"slug": "created-c", "rating": 1.5,
                            "stats": {"reads": 3}}}))
    add(Case(id="create-with-array", method="POST", path="/posts",
             json={"title": "Created D", "tags": ["x", "y", "z"]}))
    add(Case(id="create-explicit-id", method="POST", path="/posts",
             json={"id": 500, "title": "Explicit"},
             headers_extra=("Location",)))
    add(Case(id="create-explicit-string-id", method="POST", path="/notes",
             json={"id": "n-created", "text": "hi"},
             headers_extra=("Location",)))
    add(Case(id="create-duplicate-id", method="POST", path="/posts",
             json={"id": 500, "title": "Clash"},
             body_mode="stack",
             note="lodash-id throws; the body carries a stack, so the frames "
                  "are cut and everything around them graded exactly"))
    add(Case(id="create-after-duplicate", method="POST", path="/posts",
             json={"title": "Created E"},
             headers_extra=("Location",),
             note="ids are max+1 at insert time, so the failed insert is invisible"))
    add(Case(id="create-empty-object", method="POST", path="/posts", json={}))
    add(Case(id="create-into-string-id-collection", method="POST",
             path="/notes", json={"text": "auto id among string ids"},
             headers_extra=("Location",),
             note="max() over non-numeric ids falls back to nanoid"))
    add(Case(id="create-on-singular", method="POST", path="/profile",
             json={"name": "posted"}))
    add(Case(id="create-unknown-collection", method="POST", path="/nosuch",
             json={"a": 1}))
    add(Case(id="verify-created-list", method="GET", path="/posts",
             headers_extra=()))
    add(Case(id="verify-created-item-500", method="GET", path="/posts/500"))
    add(Case(id="verify-created-note", method="GET", path="/notes/n-created"))
    add(Case(id="verify-total-after-creates", method="GET",
             path="/posts?_page=1&_limit=1",
             headers_extra=("X-Total-Count", "Link")))
    return cases


def _create_body_cases() -> list[Case]:
    """POST body parsing: content types, limits and malformed payloads."""
    cases: list[Case] = []
    add = cases.append

    add(Case(id="body-urlencoded", method="POST", path="/posts",
             data=b"title=Form+Post&age=33",
             headers=(("Content-Type", "application/x-www-form-urlencoded"),),
             headers_extra=("Location",),
             note="urlencoded values stay strings: age is \"33\""))
    add(Case(id="body-urlencoded-nested-bracket", method="POST", path="/posts",
             data=b"title=Bracket&meta%5Btag%5D=z",
             headers=(("Content-Type", "application/x-www-form-urlencoded"),),
             note="extended:false, so the bracket key is not expanded"))
    add(Case(id="body-urlencoded-empty", method="POST", path="/posts",
             data=b"",
             headers=(("Content-Type", "application/x-www-form-urlencoded"),)))
    add(Case(id="body-json-empty-string", method="POST", path="/posts",
             data=b"", headers=(("Content-Type", "application/json"),)))
    add(Case(id="body-json-malformed", method="POST", path="/posts",
             data=b"{not json", headers=(("Content-Type", "application/json"),),
             body_mode="stack",
             note="400 as Express's HTML error page; the page is graded, "
                  "the stack frames inside it are not"))
    add(Case(id="body-json-array", method="POST", path="/posts",
             data=b'[{"title":"arr"}]',
             headers=(("Content-Type", "application/json"),)))
    add(Case(id="body-json-scalar", method="POST", path="/posts",
             data=b'"just a string"',
             headers=(("Content-Type", "application/json"),),
             body_mode="stack",
             note="body-parser strict mode rejects a top-level scalar"))
    add(Case(id="body-json-null", method="POST", path="/posts",
             data=b"null", headers=(("Content-Type", "application/json"),),
             body_mode="stack",
             note="strict mode rejects `null` too, for the same reason"))
    add(Case(id="body-no-content-type", method="POST", path="/posts",
             data=b'{"title":"no ct"}'))
    add(Case(id="body-text-plain", method="POST", path="/posts",
             data=b"hello", headers=(("Content-Type", "text/plain"),)))
    add(Case(id="body-json-charset", method="POST", path="/posts",
             data=b'{"title":"charset"}',
             headers=(("Content-Type", "application/json; charset=utf-8"),),
             headers_extra=("Location",)))
    add(Case(id="body-json-vendor-type", method="POST", path="/posts",
             data=b'{"title":"vendor"}',
             headers=(("Content-Type", "application/vnd.api+json"),)))
    add(Case(id="body-unicode", method="POST", path="/posts",
             data='{"title":"日本語"}'.encode(),
             headers=(("Content-Type", "application/json"),),
             headers_extra=("Location",)))
    add(Case(id="body-deeply-nested", method="POST", path="/posts",
             json={"title": "deep", "a": {"b": {"c": {"d": {"e": 1}}}}}))
    add(Case(id="verify-body-cases", method="GET", path="/posts"))
    return cases


def _replace_cases() -> list[Case]:
    """PUT: full replacement semantics."""
    cases: list[Case] = []
    add = cases.append
    add(Case(id="put-item", method="PUT", path="/posts/1",
             json={"title": "Replaced"},
             note="PUT drops every field not in the body except the id"))
    add(Case(id="put-item-verify", method="GET", path="/posts/1"))
    add(Case(id="put-with-id-in-body", method="PUT", path="/posts/2",
             json={"id": 2, "title": "Replaced 2"}))
    add(Case(id="put-with-different-id-in-body", method="PUT", path="/posts/3",
             json={"id": 999, "title": "Id in body ignored?"}))
    add(Case(id="put-with-different-id-verify", method="GET", path="/posts/3"))
    add(Case(id="put-missing", method="PUT", path="/posts/9999",
             json={"title": "nope"}))
    add(Case(id="put-empty-body", method="PUT", path="/posts/4", json={}))
    add(Case(id="put-empty-body-verify", method="GET", path="/posts/4"))
    add(Case(id="put-singular", method="PUT", path="/profile",
             json={"name": "Replaced Profile"}))
    add(Case(id="put-singular-verify", method="GET", path="/profile"))
    add(Case(id="put-string-id", method="PUT", path="/notes/n-a",
             json={"text": "replaced"}))
    add(Case(id="put-string-id-verify", method="GET", path="/notes/n-a"))
    add(Case(id="put-urlencoded", method="PUT", path="/posts/5",
             data=b"title=Put+Form",
             headers=(("Content-Type", "application/x-www-form-urlencoded"),)))
    add(Case(id="put-collection", method="PUT", path="/posts",
             json={"title": "collection put"}))
    add(Case(id="put-list-verify", method="GET", path="/posts"))
    return cases


def _merge_cases() -> list[Case]:
    """PATCH: partial update semantics."""
    cases: list[Case] = []
    add = cases.append
    add(Case(id="patch-item", method="PATCH", path="/posts/1",
             json={"views": 4242}))
    add(Case(id="patch-item-verify", method="GET", path="/posts/1",
             note="fields absent from the patch survive"))
    add(Case(id="patch-add-field", method="PATCH", path="/posts/2",
             json={"brandnew": "yes"}))
    add(Case(id="patch-add-field-verify", method="GET", path="/posts/2"))
    add(Case(id="patch-nested-object", method="PATCH", path="/posts/1",
             json={"meta": {"slug": "patched"}},
             note="nested objects are replaced wholesale, not deep-merged"))
    add(Case(id="patch-nested-verify", method="GET", path="/posts/1"))
    add(Case(id="patch-null-field", method="PATCH", path="/posts/6",
             json={"category": None}))
    add(Case(id="patch-null-verify", method="GET", path="/posts/6"))
    add(Case(id="patch-id", method="PATCH", path="/posts/7", json={"id": 777}))
    add(Case(id="patch-id-verify-old", method="GET", path="/posts/7"))
    add(Case(id="patch-id-verify-new", method="GET", path="/posts/777"))
    add(Case(id="patch-missing", method="PATCH", path="/posts/9999",
             json={"views": 1}))
    add(Case(id="patch-empty", method="PATCH", path="/posts/8", json={}))
    add(Case(id="patch-empty-verify", method="GET", path="/posts/8"))
    add(Case(id="patch-singular", method="PATCH", path="/settings",
             json={"theme": "dark"}))
    add(Case(id="patch-singular-verify", method="GET", path="/settings"))
    add(Case(id="patch-collection", method="PATCH", path="/posts",
             json={"x": 1}))
    add(Case(id="patch-urlencoded", method="PATCH", path="/posts/9",
             data=b"title=Patched+Form",
             headers=(("Content-Type", "application/x-www-form-urlencoded"),)))
    add(Case(id="patch-urlencoded-verify", method="GET", path="/posts/9"))
    return cases


def _delete_cases() -> list[Case]:
    """DELETE, and the dependent cascade it triggers."""
    cases: list[Case] = []
    add = cases.append
    add(Case(id="delete-comment", method="DELETE", path="/comments/1"))
    add(Case(id="delete-comment-verify", method="GET", path="/comments/1"))
    add(Case(id="delete-post-with-comments", method="DELETE", path="/posts/1",
             note="dependent comments are removed with the parent"))
    add(Case(id="delete-cascade-verify-list", method="GET", path="/comments"))
    add(Case(id="delete-cascade-verify-nested", method="GET",
             path="/posts/1/comments"))
    add(Case(id="delete-post-verify", method="GET", path="/posts/1"))
    add(Case(id="delete-missing", method="DELETE", path="/posts/9999"))
    add(Case(id="delete-twice", method="DELETE", path="/posts/2"))
    add(Case(id="delete-twice-again", method="DELETE", path="/posts/2"))
    add(Case(id="delete-string-id", method="DELETE", path="/notes/n-b"))
    add(Case(id="delete-string-id-verify", method="GET", path="/notes"))
    add(Case(id="delete-singular", method="DELETE", path="/profile"))
    add(Case(id="delete-singular-verify", method="GET", path="/profile"))
    add(Case(id="delete-collection", method="DELETE", path="/posts"))
    add(Case(id="delete-author-cascade", method="DELETE", path="/authors/1",
             note="posts referencing the author are removed too"))
    add(Case(id="delete-author-cascade-verify", method="GET", path="/posts"))
    add(Case(id="delete-total-after", method="GET", path="/posts?_page=1",
             headers_extra=("X-Total-Count", "Link")))
    add(Case(id="delete-db-after", method="GET", path="/db"))
    return cases


def _nested_write_cases() -> list[Case]:
    """POST through a nested route injects the parent key into the body."""
    cases: list[Case] = []
    add = cases.append
    add(Case(id="nested-create-comment", method="POST", path="/posts/1/comments",
             json={"body": "nested create"},
             headers_extra=("Location",),
             note="postId is injected as the *string* \"1\""))
    add(Case(id="nested-create-verify", method="GET", path="/comments"))
    add(Case(id="nested-create-visible-under-parent", method="GET",
             path="/posts/1/comments"))
    add(Case(id="nested-create-filter-by-string-fk", method="GET",
             path="/comments?postId=1"))
    add(Case(id="nested-create-explicit-fk", method="POST",
             path="/posts/2/comments", json={"body": "explicit", "postId": 3},
             note="the injected key wins over the body's own"))
    add(Case(id="nested-create-explicit-fk-verify", method="GET",
             path="/comments?_sort=id&_order=desc&_limit=3",
             headers_extra=("X-Total-Count",)))
    add(Case(id="nested-create-missing-parent", method="POST",
             path="/posts/9999/comments", json={"body": "orphan"}))
    add(Case(id="nested-create-author-post", method="POST",
             path="/authors/1/posts", json={"title": "by author 1"}))
    add(Case(id="nested-create-author-post-verify", method="GET",
             path="/authors/1/posts"))
    add(Case(id="nested-put", method="PUT", path="/posts/1/comments",
             json={"body": "x"}))
    add(Case(id="nested-delete", method="DELETE", path="/posts/1/comments"))
    return cases


def _override_cases() -> list[Case]:
    """Method override, and the Vary it contributes."""
    cases: list[Case] = []
    add = cases.append
    add(Case(id="override-header-delete", method="POST", path="/posts/1",
             json={},
             headers=(("X-HTTP-Method-Override", "DELETE"),),
             headers_extra=("Vary",)))
    add(Case(id="override-header-delete-verify", method="GET", path="/posts/1"))
    add(Case(id="override-header-patch", method="POST", path="/posts/2",
             json={"views": 11},
             headers=(("X-HTTP-Method-Override", "PATCH"),),
             headers_extra=("Vary",)))
    add(Case(id="override-header-patch-verify", method="GET", path="/posts/2"))
    add(Case(id="override-header-put", method="POST", path="/posts/3",
             json={"title": "overridden put"},
             headers=(("X-HTTP-Method-Override", "PUT"),)))
    add(Case(id="override-header-put-verify", method="GET", path="/posts/3"))
    add(Case(id="override-header-lowercase", method="POST", path="/posts/4",
             json={"views": 1},
             headers=(("x-http-method-override", "patch"),)))
    add(Case(id="override-header-lowercase-verify", method="GET",
             path="/posts/4"))
    add(Case(id="override-header-get", method="POST", path="/posts/5",
             json={},
             headers=(("X-HTTP-Method-Override", "GET"),)))
    add(Case(id="override-header-bogus", method="POST", path="/posts",
             json={"title": "bogus override"},
             headers=(("X-HTTP-Method-Override", "NOTAMETHOD"),)))
    add(Case(id="override-query-ignored", method="POST",
             path="/posts/6?_method=DELETE", json={},
             note="only the header form is enabled"))
    add(Case(id="override-query-ignored-verify", method="GET", path="/posts/6"))
    add(Case(id="override-on-get", method="GET", path="/posts/7",
             headers=(("X-HTTP-Method-Override", "DELETE"),),
             note="override applies to POST only"))
    add(Case(id="override-on-get-verify", method="GET", path="/posts/7"))
    return cases


def _readonly_cases() -> list[Case]:
    """--read-only: every unsafe method is refused before it reaches the DB."""
    cases: list[Case] = []
    add = cases.append
    add(Case(id="ro-get-list", method="GET", path="/posts"))
    add(Case(id="ro-get-item", method="GET", path="/posts/1"))
    add(Case(id="ro-get-db", method="GET", path="/db"))
    add(Case(id="ro-head", method="HEAD", path="/posts/1"))
    add(Case(id="ro-post", method="POST", path="/posts", json={"title": "x"}))
    add(Case(id="ro-put", method="PUT", path="/posts/1", json={"title": "x"}))
    add(Case(id="ro-patch", method="PATCH", path="/posts/1", json={"views": 1}))
    add(Case(id="ro-delete", method="DELETE", path="/posts/1"))
    add(Case(id="ro-options", method="OPTIONS", path="/posts",
             headers=(("Origin", "http://example.test"),
                      ("Access-Control-Request-Method", "POST")),
             headers_extra=("Access-Control-Allow-Origin",)))
    add(Case(id="ro-post-nested", method="POST", path="/posts/1/comments",
             json={"body": "x"}))
    add(Case(id="ro-post-unknown", method="POST", path="/nosuch", json={}))
    add(Case(id="ro-override-delete", method="POST", path="/posts/1", json={},
             headers=(("X-HTTP-Method-Override", "DELETE"),),
             note="override must not be a way around read-only"))
    add(Case(id="ro-unchanged-after", method="GET", path="/posts/1"))
    add(Case(id="ro-static-index", method="GET", path="/",
             headers_skip=("ETag", "Last-Modified")))
    add(Case(id="ro-rules", method="GET", path="/__rules"))
    return cases


def _nocors_cases() -> list[Case]:
    """CORS actually disabled, via the alias that works."""
    cases = [
        Case(id="nocors-simple", method="GET", path="/posts/1",
             headers=(("Origin", "http://example.test"),),
             headers_extra=("Access-Control-Allow-Origin",
                            "Access-Control-Allow-Credentials", "Vary")),
        Case(id="nocors-preflight", method="OPTIONS", path="/posts",
             headers=(("Origin", "http://example.test"),
                      ("Access-Control-Request-Method", "POST")),
             headers_extra=("Access-Control-Allow-Origin",
                            "Access-Control-Allow-Methods")),
        Case(id="nocors-post", method="POST", path="/posts",
             json={"title": "no cors"},
             headers=(("Origin", "http://example.test"),),
             headers_extra=("Location", "Access-Control-Allow-Origin")),
        Case(id="nocors-list", method="GET", path="/posts",
             headers_extra=("Vary",)),
        Case(id="nocors-expose-headers", method="GET", path="/posts?_page=1",
             headers=(("Origin", "http://example.test"),),
             headers_extra=("Access-Control-Expose-Headers", "Link",
                            "X-Total-Count")),
    ]
    return cases


def _nogzip_cases() -> list[Case]:
    """Compression actually disabled, via the alias that works."""
    cases = [
        Case(id="nogzip-list", method="GET", path="/posts", headers=GZIP,
             headers_extra=("Content-Encoding", "Vary")),
        Case(id="nogzip-db", method="GET", path="/db", headers=GZIP,
             headers_extra=("Content-Encoding",)),
        Case(id="nogzip-item", method="GET", path="/posts/1", headers=GZIP,
             headers_extra=("Content-Encoding",)),
        Case(id="nogzip-index", method="GET", path="/", headers=GZIP,
             headers_extra=("Content-Encoding",),
             headers_skip=("ETag", "Last-Modified")),
        Case(id="nogzip-deflate", method="GET", path="/posts",
             headers=(("Accept-Encoding", "deflate"),),
             headers_extra=("Content-Encoding",)),
    ]
    return cases


def _inert_flag_cases() -> list[Case]:
    """The documented long forms are swallowed by yargs' boolean negation.

    ``--no-cors`` and ``--no-gzip`` are declared with ``.boolean()``, so yargs
    reads them as negations of ``cors`` and ``gzip`` -- names nothing consults --
    and the ``nc``/``ng`` aliases are what actually reach the option object. The
    long forms therefore have no effect at all. A rewrite that "fixes" this is
    changing observable behaviour, so both forms are graded.
    """
    cases = [
        Case(id="inert-cors-still-on", method="GET", path="/posts/1",
             headers=(("Origin", "http://example.test"),),
             headers_extra=("Access-Control-Allow-Origin",
                            "Access-Control-Allow-Credentials")),
        Case(id="inert-gzip-still-on", method="GET", path="/posts",
             headers=GZIP, decompress="gzip",
             headers_extra=("Content-Encoding",)),
        Case(id="inert-preflight", method="OPTIONS", path="/posts",
             headers=(("Origin", "http://example.test"),
                      ("Access-Control-Request-Method", "POST")),
             headers_extra=("Access-Control-Allow-Origin",
                            "Access-Control-Allow-Methods")),
    ]
    return cases


def _customid_cases() -> list[Case]:
    cases = [
        Case(id="cid-list", method="GET", path="/posts"),
        Case(id="cid-item", method="GET", path="/posts/1"),
        Case(id="cid-filter-underscore-id", method="GET", path="/posts?_id=2"),
        Case(id="cid-sort", method="GET", path="/posts?_sort=_id&_order=desc"),
        Case(id="cid-create", method="POST", path="/posts",
             json={"title": "custom id"}, headers_extra=("Location",)),
        Case(id="cid-create-verify", method="GET", path="/posts?_sort=_id&_order=desc&_limit=2",
             headers_extra=("X-Total-Count",)),
        Case(id="cid-patch", method="PATCH", path="/posts/3", json={"views": 5}),
        Case(id="cid-patch-verify", method="GET", path="/posts/3"),
        Case(id="cid-delete", method="DELETE", path="/posts/4",
             body_mode="stack",
             note="getRemovable() reads doc.id even under --id _id, so a "
                  "cascade check on an undefined id throws: 500, not 200"),
        Case(id="cid-delete-verify", method="GET", path="/posts/4"),
        Case(id="cid-embed", method="GET", path="/posts/2?_embed=comments"),
        Case(id="cid-nested", method="GET", path="/posts/2/comments"),
    ]
    return cases


def _customfk_cases() -> list[Case]:
    cases = [
        Case(id="cfk-embed", method="GET", path="/posts?_embed=comments"),
        Case(id="cfk-expand", method="GET", path="/comments?_expand=post"),
        Case(id="cfk-nested", method="GET", path="/posts/1/comments"),
        Case(id="cfk-nested-create", method="POST", path="/posts/1/comments",
             json={"body": "custom fk"}, headers_extra=("Location",)),
        Case(id="cfk-nested-create-verify", method="GET", path="/posts/1/comments"),
        Case(id="cfk-cascade-delete", method="DELETE", path="/posts/1"),
        Case(id="cfk-cascade-verify", method="GET", path="/comments"),
        Case(id="cfk-filter", method="GET", path="/comments?post_id=2"),
    ]
    return cases


def _delay_cases() -> list[Case]:
    cases = [
        Case(id="delay-item", method="GET", path="/posts/1"),
        Case(id="delay-list", method="GET", path="/posts"),
        Case(id="delay-create", method="POST", path="/posts",
             json={"title": "delayed"}, headers_extra=("Location",)),
        Case(id="delay-index", method="GET", path="/",
             headers_skip=("ETag", "Last-Modified")),
    ]
    return cases


def _staticalt_cases() -> list[Case]:
    cases = [
        Case(id="alt-index", method="GET", path="/",
             headers_skip=("ETag", "Last-Modified")),
        Case(id="alt-named-file", method="GET", path="/alt.txt",
             headers_skip=("ETag", "Last-Modified")),
        Case(id="alt-missing", method="GET", path="/nosuch.txt"),
        Case(id="alt-api-still-works", method="GET", path="/posts/1"),
        Case(id="alt-subdir", method="GET", path="/sub/deep.json",
             headers_skip=("ETag", "Last-Modified")),
    ]
    return cases


SESSIONS: tuple[Session, ...] = (
    Session(id="read", cases=tuple(_read_cases()), mutating=False,
            note="the default configuration, read-only requests"),
    Session(id="create", cases=tuple(_create_cases()), mutating=True),
    Session(id="create-body", cases=tuple(_create_body_cases()), mutating=True),
    Session(id="replace", cases=tuple(_replace_cases()), mutating=True),
    Session(id="merge", cases=tuple(_merge_cases()), mutating=True),
    Session(id="delete", cases=tuple(_delete_cases()), mutating=True),
    Session(id="nested-write", cases=tuple(_nested_write_cases()),
            mutating=True),
    Session(id="override", cases=tuple(_override_cases()), mutating=True),
    Session(id="read-only", cases=tuple(_readonly_cases()), mutating=True,
            argv=("--read-only",),
            note="mutating=True only because it must not share a server"),
    Session(id="no-cors", cases=tuple(_nocors_cases()), mutating=True,
            argv=("--nc",),
            note="--nc is the alias that actually disables CORS"),
    Session(id="no-gzip", cases=tuple(_nogzip_cases()), mutating=True,
            argv=("--ng",),
            note="--ng is the alias that actually disables compression"),
    Session(id="inert-flags", cases=tuple(_inert_flag_cases()), mutating=True,
            argv=("--no-cors", "--no-gzip"),
            note="the documented long forms have no effect upstream"),
    Session(id="custom-id", cases=tuple(_customid_cases()), mutating=True,
            argv=("--id", "_id"), seed="underscore-id"),
    Session(id="custom-fk", cases=tuple(_customfk_cases()), mutating=True,
            argv=("--foreignKeySuffix", "_id"), seed="underscore-fk"),
    Session(id="delay", cases=tuple(_delay_cases()), mutating=True,
            argv=("--delay", "150")),
    Session(id="static-alt", cases=tuple(_staticalt_cases()), mutating=True,
            argv=("--static", "altpublic"),
            note="--static is joined with cwd upstream, so it must be relative"),
)


def all_cases() -> list[tuple[str, Case]]:
    return [(s.id, c) for s in SESSIONS for c in s.cases]


def case_key(session_id: str, case_id: str) -> str:
    return f"{session_id}::{case_id}"


def fingerprint() -> str:
    """A digest of every request here, and of how its answer is compared.

    Case ids alone are not enough: editing a case's path or body while keeping
    its id would leave the recording looking complete while grading a request
    that was never recorded. The fingerprint is stored in
    ``data/recorded-responses.json`` and checked before grading, so that mistake
    fails loudly instead of quietly.
    """
    import hashlib

    h = hashlib.sha256()
    for session in SESSIONS:
        h.update(f"S|{session.id}|{session.argv}|{session.seed}|"
                 f"{session.mutating}|{session.env}\n".encode())
        for case in session.cases:
            h.update(
                f"C|{case.id}|{case.method}|{case.path}|{case.json!r}|"
                f"{case.data!r}|{case.headers}|{case.headers_extra}|"
                f"{case.headers_skip}|{case.decompress}|{case.body_mode}\n"
                .encode())
    return h.hexdigest()[:16]
