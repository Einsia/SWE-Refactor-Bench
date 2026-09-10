"""What gets measured.

Every session here exists because it is the only way to reach some behaviour of
the baseline. miniserve decides everything from its command line, so a flag that
is never passed is a flag whose port is never checked -- and a port that quietly
drops ``--pretty-urls`` or renders the theme picker with the wrong option
selected is exactly the kind of near-miss this benchmark is meant to catch.

The corpus is organised by *what could go wrong* rather than by source file:

*   **Routing and static serving.** Paths that are already percent-encoded, that
    contain a literal newline or backslash, that traverse upward, that need a
    trailing-slash redirect. actix-files makes a specific set of choices here and
    a hand-rolled ``ServeDir`` makes different ones.
*   **The rendered listing.** Sort method x order x dirs-first is a matrix, not
    a flag: ``--default-sorting-method size`` with ``--default-sorting-order
    asc`` and ``--dirs-first`` is a different page from all three separately.
    The ``sorting/`` subtree of the sample tree is built so natural-sort, case-folding and
    size ordering all disagree with each other, which means a port that
    substitutes a plain lexicographic sort fails visibly.
*   **Conditional and range requests.** Graded by echoing back the server's own
    ``ETag`` and ``Last-Modified``, so a port that issues validators it then
    fails to honour is caught.
*   **Content negotiation.** Four compression schemes, plus ``identity`` and a
    ``q=0`` refusal.
*   **Archives.** tar, tar.gz and zip over subtrees that include a symlink, a
    dangling symlink, hidden files and a 1 MiB file.
*   **Auth.** Plain, sha256, sha512, empty password, file-based, multiple users,
    and the interaction between auth and the nonce static routes -- which in the
    baseline sit *outside* the authenticated scope and are therefore reachable
    unauthenticated. That is surprising, it is observable, and it is contract.
*   **Uploads and mkdir.** The multipart path, the sanitiser (``..``, hidden
    names, absolute names, symlink traversal), the allowed-directory restriction,
    overwrite behaviour, and the ``Referer``-driven redirect target.
*   **Errors.** Every ``RuntimeError`` variant that can be provoked over HTTP,
    each with its status and its rendered page.

Ports are assigned from ``PORT_BASE`` in declaration order, so inserting a
session shifts the ones after it. That is fine: nothing outside a single run
depends on the number. What must not change is the *set of case ids*, since the
golden file is keyed by them.
"""

from __future__ import annotations

import base64
from dataclasses import replace
from urllib.parse import quote

from . import cli
from .sessions import Case, Session, digest

# ---------------------------------------------------------------------------
# Sample-tree names, as request targets
# ---------------------------------------------------------------------------
# Encoded the way a browser would, then some deliberately *not*. The unencoded
# variants are separate cases because they are separate contracts: actix-web's
# ``Uri`` accepts a raw backslash and rejects a raw space, and a port built on
# ``http`` 1.x has to reproduce both answers.


def enc(name: str) -> str:
    """Percent-encode a sample-tree name for use as a single path segment."""
    return quote(name, safe="")


def basic(user: str, password: str) -> str:
    """An HTTP basic ``Authorization`` value.

    The sessions that carry credentials get them attached by the runner; this is
    for the cases that need a *specific*, usually wrong, one -- an unknown user,
    the right user with the wrong password, a valid header for a session that
    should still refuse it.
    """
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return f"Basic {token}"


#: The verification file names from the sample tree spec, by short handle.
NAMES = {
    "plain": "test.txt",
    "html": "test.html",
    "mkv": "test.mkv",
    "quotes": 'test " \' & < >.csv',
    "newline": "new\nline",
    "emoji": "😀.data",
    "printer": "⎙.mp4",
    "specials": "#[]{}()@!$&'`+,;= %20.test",
    "specials2": ":?#[]{}<>()@!$&'`|*+,;= %20.test",
    "backslash": "foo\\bar.test",
    "hidden1": ".hidden_file1",
    "hidden2": ".hidden_file2",
}

#: Media samples, for content-type inference. ``noext`` and ``plain.unknownext``
#: are the two ways a guess can fail, and they are answered differently.
MEDIA = ["pic.png", "doc.pdf", "data.json", "page.htm", "style.css", "app.js",
         "notes.md", "archive.zip", "clip.webm", "plain.unknownext", "noext",
         "UPPER.TXT"]

#: Directories with identical contents, so a listing difference is about the
#: *path*, not the entries.
DIRS = ["dira", "dirb", "dirc"]
HIDDEN_DIRS = [".hidden_dir1", ".hidden_dir2"]


def q(*parts: str) -> str:
    """Build an encoded request path from raw sample-tree segments."""
    return "/" + "/".join(enc(p) for p in parts)


# ---------------------------------------------------------------------------
# Case builders
# ---------------------------------------------------------------------------

def listing_cases(prefix: str, base: str = "", *,
                  body_mode: str = "html") -> list[Case]:
    """The listing of a directory, plus the query-parameter surface on it.

    ``sort`` and ``order`` are read from the query string on every request, so
    they are properties of the *request*, not of the session -- which is why the
    matrix lives here and the ``--default-sorting-*`` flags get sessions of their
    own to prove the default is what changes.
    """
    cases: list[Case] = []
    root = base + "/"
    cases.append(Case(id=f"{prefix}-root", path=root, body_mode=body_mode,
                      headers_extra=("content-type",)))
    for method in ("name", "size", "date"):
        for order in ("asc", "desc"):
            cases.append(Case(
                id=f"{prefix}-sort-{method}-{order}",
                path=f"{root}?sort={method}&order={order}",
                body_mode=body_mode,
                note="query sorting is per-request, not per-process"))
    # Sorting the subtree built to make the three methods disagree.
    for method in ("name", "size", "date"):
        for order in ("asc", "desc"):
            cases.append(Case(
                id=f"{prefix}-sortdir-{method}-{order}",
                path=f"{base}/sorting/?sort={method}&order={order}",
                body_mode=body_mode))
    cases.append(Case(id=f"{prefix}-sort-unknown",
                      path=f"{root}?sort=nope", body_mode=body_mode,
                      note="an unparseable sort method is a 400 in the baseline"))
    cases.append(Case(id=f"{prefix}-order-unknown",
                      path=f"{root}?order=sideways", body_mode=body_mode))
    cases.append(Case(id=f"{prefix}-raw", path=f"{root}?raw=true",
                      body_mode=body_mode,
                      note="the raw listing is a different renderer entirely"))
    cases.append(Case(id=f"{prefix}-raw-false", path=f"{root}?raw=false",
                      body_mode=body_mode))
    cases.append(Case(id=f"{prefix}-raw-garbage", path=f"{root}?raw=maybe",
                      body_mode=body_mode))
    cases.append(Case(id=f"{prefix}-unknown-param", path=f"{root}?zzz=1",
                      body_mode=body_mode,
                      note="an unknown parameter must be ignored, not rejected"))
    for name in DIRS:
        cases.append(Case(id=f"{prefix}-dir-{name}", path=f"{base}/{name}/",
                          body_mode=body_mode))
    cases.append(Case(id=f"{prefix}-dir-noslash", path=f"{base}/dira",
                      body_mode="shape", headers_extra=("location",),
                      note="redirect_to_slash_directory"))
    cases.append(Case(id=f"{prefix}-deep",
                      path=f"{base}/very/deeply/nested/", body_mode=body_mode))
    cases.append(Case(id=f"{prefix}-empty", path=f"{base}/emptydir/",
                      body_mode=body_mode,
                      note="a listing with no entries still has a breadcrumb"))
    cases.append(Case(id=f"{prefix}-onechild", path=f"{base}/onechild/",
                      body_mode=body_mode))
    cases.append(Case(id=f"{prefix}-media", path=f"{base}/media/",
                      body_mode=body_mode))
    cases.append(Case(id=f"{prefix}-links", path=f"{base}/links/",
                      body_mode=body_mode))
    return cases


def file_cases(prefix: str, base: str = "") -> list[Case]:
    """One GET per interesting file, plus the header surface on a text file."""
    cases: list[Case] = []
    for handle, name in NAMES.items():
        if handle.startswith("hidden"):
            continue
        cases.append(Case(
            id=f"{prefix}-file-{handle}",
            path=base + q(name),
            headers_extra=("content-type", "content-disposition",
                           "last-modified", "etag", "accept-ranges"),
            body_mode="exact" if handle not in ("emoji", "printer") else "binary",
        ))
    for name in MEDIA:
        cases.append(Case(
            id=f"{prefix}-media-{name}",
            path=f"{base}/media/{enc(name)}",
            headers_extra=("content-type", "content-disposition"),
            body_mode="binary" if name in ("pic.png", "doc.pdf", "archive.zip",
                                           "clip.webm") else "exact",
            note="content type is guessed from the extension; two of these "
                 "samples have none to guess from"))
    cases.append(Case(id=f"{prefix}-file-nested",
                      path=f"{base}/very/deeply/nested/test.rs",
                      headers_extra=("content-type",)))
    cases.append(Case(id=f"{prefix}-file-big", path=f"{base}/big.bin",
                      body_mode="binary",
                      headers_extra=("content-type", "accept-ranges", "etag")))
    return cases


def conditional_cases(prefix: str, base: str = "") -> list[Case]:
    """Validators, echoed back from the server's own answer.

    The source case is a plain GET; each conditional case reuses its ``ETag`` or
    ``Last-Modified``. A port that emits no validator sends no condition and
    lands on a different status, which is the point.
    """
    src = f"{prefix}-cond-src"
    path = f"{base}/medium.txt"
    return [
        Case(id=src, path=path,
             headers_extra=("etag", "last-modified", "accept-ranges")),
        Case(id=f"{prefix}-cond-inm-match", path=path,
             etag_from=(src, "If-None-Match"),
             headers_extra=("etag", "last-modified"),
             note="304, and the baseline still sends the entity headers"),
        Case(id=f"{prefix}-cond-inm-star", path=path,
             headers=(("If-None-Match", "*"),),
             headers_extra=("etag",)),
        Case(id=f"{prefix}-cond-inm-miss", path=path,
             headers=(("If-None-Match", '"deadbeef:0:0:0"'),),
             headers_extra=("etag",)),
        Case(id=f"{prefix}-cond-im-match", path=path,
             etag_from=(src, "If-Match"), headers_extra=("etag",)),
        Case(id=f"{prefix}-cond-im-miss", path=path,
             headers=(("If-Match", '"deadbeef:0:0:0"'),),
             note="412 Precondition Failed"),
        Case(id=f"{prefix}-cond-ims-equal", path=path,
             last_modified_from=(src, "If-Modified-Since"),
             headers_extra=("last-modified",)),
        Case(id=f"{prefix}-cond-ims-old", path=path,
             headers=(("If-Modified-Since",
                       "Sat, 01 Jan 2000 00:00:00 GMT"),)),
        Case(id=f"{prefix}-cond-ims-future", path=path,
             headers=(("If-Modified-Since",
                       "Fri, 01 Jan 2100 00:00:00 GMT"),)),
        Case(id=f"{prefix}-cond-ius-old", path=path,
             headers=(("If-Unmodified-Since",
                       "Sat, 01 Jan 2000 00:00:00 GMT"),)),
        Case(id=f"{prefix}-cond-ims-garbage", path=path,
             headers=(("If-Modified-Since", "not a date"),),
             note="an unparseable date is ignored, not an error"),
    ]


def range_cases(prefix: str, base: str = "") -> list[Case]:
    """Byte ranges, including the forms that are errors."""
    path = f"{base}/medium.txt"
    src = f"{prefix}-range-src"
    ranges = [
        ("head", "bytes=0-9"),
        ("mid", "bytes=10-19"),
        ("open", "bytes=100-"),
        ("suffix", "bytes=-20"),
        ("single", "bytes=0-0"),
        ("whole", "bytes=0-"),
        ("past-end", "bytes=999999-1000000"),
        ("inverted", "bytes=20-10"),
        ("multi", "bytes=0-4,10-14"),
        ("unit-unknown", "items=0-10"),
        ("garbage", "bytes=abc"),
        # NOT tested: an empty ``Range: bytes=``. actix-files 0.6.5 panics on it
        # -- `index out of bounds: the len is 0 but the index is 0` in
        # named.rs:534 -- which takes down the worker that was handling the
        # request. That is an upstream bug, not behaviour worth reproducing:
        # grading it would mean requiring the port to panic too, and any sane
        # answer (416, or 200 with the whole file) would be marked wrong. It is
        # left here as a note rather than deleted so it is clear the omission is
        # deliberate.
    ]
    cases = [Case(id=src, path=path,
                  headers_extra=("etag", "accept-ranges", "content-type"))]
    for handle, value in ranges:
        cases.append(Case(
            id=f"{prefix}-range-{handle}", path=path,
            headers=(("Range", value),),
            headers_extra=("content-range", "content-type", "accept-ranges"),
            body_mode="exact"))
    cases.append(Case(id=f"{prefix}-range-if-range-match", path=path,
                      headers=(("Range", "bytes=0-4"),),
                      etag_from=(src, "If-Range"),
                      headers_extra=("content-range",),
                      note="matching If-Range keeps the 206"))
    # RFC 9110 says a stale If-Range must downgrade the response to a full 200.
    # actix-files 0.6.5 does not: it ignores If-Range entirely and still answers
    # 206 with `bytes 0-4/4200`. Verified against the oracle, not inferred. The
    # case is graded as recorded, so a port that implements If-Range CORRECTLY
    # fails it -- which is the intended reading of "bug-for-bug", and the reason
    # this note spells out that the standard and the baseline disagree.
    cases.append(Case(id=f"{prefix}-range-if-range-miss", path=path,
                      headers=(("Range", "bytes=0-4"),
                               ("If-Range", '"deadbeef:0:0:0"')),
                      headers_extra=("content-range",),
                      note="a stale If-Range is ignored; still 206, per the "
                           "baseline rather than per RFC 9110"))
    cases.append(Case(id=f"{prefix}-range-on-dir", path=f"{base}/dira/",
                      headers=(("Range", "bytes=0-9"),), body_mode="html",
                      note="a listing is generated, so Range does not apply"))
    return cases


def method_cases(prefix: str, base: str = "") -> list[Case]:
    """The method surface. actix-files answers only GET and HEAD."""
    cases = []
    for method in ("HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS",
                   "TRACE"):
        cases.append(Case(id=f"{prefix}-method-{method.lower()}-file",
                          method=method, path=f"{base}/test.txt",
                          headers_extra=("allow", "content-type", "etag")))
        cases.append(Case(id=f"{prefix}-method-{method.lower()}-dir",
                          method=method, path=f"{base}/",
                          headers_extra=("allow", "content-type"),
                          body_mode="shape"))
    return cases


def notfound_cases(prefix: str, base: str = "") -> list[Case]:
    """Every way to not find something, and every way to try to escape."""
    targets = [
        ("plain", "/nope"),
        ("nested", "/nope/deeper/still"),
        ("trailing", "/nope/"),
        ("dotdot", "/../"),
        ("dotdot-file", "/../Cargo.toml"),
        ("dotdot-encoded", "/%2e%2e/Cargo.toml"),
        ("dotdot-double", "/..%252fCargo.toml"),
        ("dotdot-deep", "/dira/../../etc/passwd"),
        ("absolute", "//etc/passwd"),
        ("backslash-escape", "/..\\Cargo.toml"),
        ("null", "/test%00.txt"),
        ("dot", "/."),
        ("dotslash", "/./test.txt"),
        ("double-slash", "/dira//test.txt"),
        ("triple-slash", "///test.txt"),
        ("dir-as-file", "/dira/test.txt/"),
        ("case", "/TEST.TXT"),
        ("hidden-default", "/.hidden_file1"),
        ("hidden-dir-default", "/.hidden_dir1/"),
        ("hidden-nested-default", "/dira/.hidden_file1"),
        ("overencoded", "/%74est.txt"),
        ("query-only", "/?"),
        ("frag-like", "/test.txt%23frag"),
        ("long", "/" + "a" * 300),
        ("upload-without-flag", "/upload?path=."),
    ]
    cases = [Case(id=f"{prefix}-404-{handle}", path=base + path,
                  body_mode="html", headers_extra=("content-type",))
             for handle, path in targets]
    # Request lines no client library will send. ``http.client`` rejects a
    # literal space or a control character in the target before it reaches the
    # socket, so these go out over a bare socket instead. What the server does
    # with them -- 400, 404, or hang up without answering, which is what
    # actix-web does -- is contract either way, and a port that answers 200
    # where the baseline refused is a path-handling bug worth catching.
    for handle, path, prefixed in (
            ("space-raw", "/test .txt", True),
            ("tab-raw", "/test\t.txt", True),
            # These two are malformed as request *lines*, not as paths, so the
            # session's route prefix does not apply to them.
            ("naked-name", "test.txt", False),
            ("scheme-absolute", "http://127.0.0.1/test.txt", False)):
        cases.append(Case(id=f"{prefix}-404-{handle}",
                          path=(base + path) if prefixed else path,
                          body_mode="shape", raw_request=True,
                          headers_extra=("content-type",)))
    return cases


def static_route_cases(prefix: str) -> list[Case]:
    """The favicon and stylesheet, at their per-boot nonce routes.

    ``{favicon}`` and ``{css}`` are substituted by the runner from the hrefs the
    page itself emitted, because the routes are nanoid-generated per process.
    That indirection is also the test: a port that renders links it does not
    serve gets a 404 here.
    """
    return [
        Case(id=f"{prefix}-favicon", path="{favicon}",
             headers_extra=("content-type",),
             note="data/logo.svg, served inline"),
        Case(id=f"{prefix}-css", path="{css}",
             headers_extra=("content-type",),
             note="the compiled stylesheet: base + light theme + dark theme"),
        Case(id=f"{prefix}-favicon-head", method="HEAD", path="{favicon}",
             headers_extra=("content-type",)),
        Case(id=f"{prefix}-css-head", method="HEAD", path="{css}",
             headers_extra=("content-type",)),
        Case(id=f"{prefix}-favicon-post", method="POST", path="{favicon}",
             body_mode="html",
             note="the static routes are GET-only; a POST falls through"),
    ]


def archive_cases(prefix: str, base: str = "", *,
                  kinds: tuple[str, ...] = ("tar", "tar_gz", "zip")
                  ) -> list[Case]:
    """Archive downloads, over subtrees chosen for what they contain.

    ``links/`` has a symlink to a file, a symlink to a directory and a dangling
    symlink -- three cases the tar and zip writers each answer their own way, and
    the dangling one is where a port that resolves eagerly falls over. ``/`` is
    the whole 133-entry tree including a 1 MiB file, which is also the only case
    where the archive stream is large enough to be chunked.
    """
    targets = [("root", f"{base}/"), ("dira", f"{base}/dira/"),
               ("links", f"{base}/links/"), ("empty", f"{base}/emptydir/"),
               ("deep", f"{base}/very/"), ("sorting", f"{base}/sorting/")]
    cases: list[Case] = []
    for kind in kinds:
        for handle, path in targets:
            sep = "&" if "?" in path else "?"
            cases.append(Case(
                id=f"{prefix}-archive-{kind}-{handle}",
                path=f"{path}{sep}download={kind}",
                body_mode="archive",
                headers_extra=("content-type", "content-disposition"),
                note="member list and per-member digest, not bytes"))
    cases.append(Case(id=f"{prefix}-archive-unknown",
                      path=f"{base}/?download=rar", body_mode="html",
                      note="an unknown archive method is a 400"))
    cases.append(Case(id=f"{prefix}-archive-on-file",
                      path=f"{base}/test.txt?download=tar", body_mode="binary",
                      note="the query is ignored on a file"))
    cases.append(Case(id=f"{prefix}-archive-head", method="HEAD",
                      path=f"{base}/dira/?download=tar", body_mode="shape",
                      headers_extra=("content-type", "content-disposition")))
    return cases


def error_cases(prefix: str) -> list[Case]:
    """The RuntimeError variants reachable over HTTP, with their pages."""
    return [
        Case(id=f"{prefix}-err-sort", path="/?sort=bogus", body_mode="html",
             headers_extra=("content-type",)),
        Case(id=f"{prefix}-err-order", path="/?order=bogus", body_mode="html"),
        Case(id=f"{prefix}-err-download", path="/?download=bogus",
             body_mode="html"),
        Case(id=f"{prefix}-err-raw", path="/?raw=bogus", body_mode="html"),
        Case(id=f"{prefix}-err-route", path="/definitely-not-here",
             body_mode="html", headers_extra=("content-type",)),
        Case(id=f"{prefix}-err-invalid-utf8", path="/%FF%FE", body_mode="html"),
        Case(id=f"{prefix}-err-mkdir-no-flag", method="POST",
             path="/upload?path=.",
             multipart=(("mkdir", None, "", b"newdir"),), body_mode="html"),
    ]


# ---------------------------------------------------------------------------
# The sessions
# ---------------------------------------------------------------------------

_RAW = ()   # readability: a session with no flags

SESSION_SPECS: list[Session] = [
    # -- the default configuration -----------------------------------------
    Session(
        id="default",
        argv=_RAW,
        cases=tuple(
            listing_cases("d")
            + file_cases("d")
            + conditional_cases("d")
            + range_cases("d")
            + method_cases("d")
            + notfound_cases("d")
            + static_route_cases("d")
            + error_cases("d")
        ),
        note="no flags at all: the configuration a reader of the README gets",
    ),

    # -- hidden files ------------------------------------------------------
    Session(
        id="hidden",
        argv=("--hidden",),
        cases=tuple(
            listing_cases("h")
            + [Case(id=f"h-hidden-{k}", path=q(NAMES[k]),
                    headers_extra=("content-type", "etag"))
               for k in ("hidden1", "hidden2")]
            + [Case(id=f"h-hidden-dir-{i}", path=f"/{d}/", body_mode="html")
               for i, d in enumerate(HIDDEN_DIRS)]
            + [Case(id="h-hidden-nested", path="/dira/.hidden_file1"),
               Case(id="h-hidden-in-dir",
                    path="/.hidden_dir1/test.txt"),
               Case(id="h-archive-tar", path="/?download=tar",
                    body_mode="archive",
                    note="--hidden also changes what an archive contains")]
        ),
        note="the same tree with the dotfile filter off",
    ),

    # -- listing layout ----------------------------------------------------
    Session(
        id="dirs-first",
        argv=("--dirs-first",),
        cases=tuple(listing_cases("df")),
    ),
    Session(
        id="sort-size-asc",
        argv=("--default-sorting-method", "size",
              "--default-sorting-order", "asc"),
        cases=tuple(listing_cases("ssa")),
        note="the default the process starts with, not the query default",
    ),
    Session(
        id="sort-date-asc",
        argv=("--default-sorting-method", "date",
              "--default-sorting-order", "asc"),
        cases=tuple(listing_cases("sda")),
    ),
    Session(
        id="sort-name-asc-dirs-first",
        argv=("--default-sorting-method", "name",
              "--default-sorting-order", "asc", "--dirs-first", "--hidden"),
        cases=tuple(listing_cases("snad")),
        note="three flags whose effects compose in one page",
    ),

    # -- index, spa, pretty urls -------------------------------------------
    Session(
        id="index",
        argv=("--index", "withindex/index.html"),
        cases=tuple([
            Case(id="ix-root", path="/", body_mode="exact",
                 headers_extra=("content-type", "etag", "last-modified"),
                 note="the index replaces the listing at the root only"),
            Case(id="ix-root-head", method="HEAD", path="/",
                 headers_extra=("content-type", "etag")),
            Case(id="ix-dir-without-index", path="/dira/", body_mode="html",
                 note="a directory with no index file still lists"),
            Case(id="ix-withindex", path="/withindex/", body_mode="exact",
                 headers_extra=("content-type",),
                 note="actix-files applies index_file to every directory"),
            Case(id="ix-withindex-file", path="/withindex/index.html"),
            Case(id="ix-other", path="/withindex/other.txt"),
            Case(id="ix-404", path="/nope", body_mode="html"),
            Case(id="ix-file", path="/test.txt"),
            Case(id="ix-query", path="/?sort=size", body_mode="shape"),
        ] + static_route_cases("ix")),
        discover_path="/dira/",
    ),
    Session(
        id="index-missing",
        argv=("--index", "no-such-index.html"),
        cases=(
            Case(id="ixm-root", path="/", body_mode="html",
                 note="a missing index file warns at startup and falls back"),
            Case(id="ixm-dir", path="/dira/", body_mode="html"),
        ),
    ),
    Session(
        id="spa",
        argv=("--index", "withindex/index.html", "--spa"),
        cases=(
            Case(id="spa-root", path="/", headers_extra=("content-type",)),
            Case(id="spa-unknown", path="/some/client/route",
                 headers_extra=("content-type", "etag"),
                 note="the SPA fallback serves the index for any unknown path"),
            Case(id="spa-unknown-deep", path="/a/b/c/d/e"),
            Case(id="spa-real-file", path="/test.txt",
                 note="a real file still wins over the fallback"),
            Case(id="spa-real-dir", path="/dira/", body_mode="html"),
            Case(id="spa-dotdot", path="/../Cargo.toml", body_mode="shape"),
            Case(id="spa-head", method="HEAD", path="/client/route"),
            Case(id="spa-post", method="POST", path="/client/route",
                 body_mode="shape"),
        ),
    ),
    Session(
        id="pretty-urls",
        argv=("--pretty-urls",),
        cases=(
            Case(id="pu-about", path="/pretty/about",
                 headers_extra=("content-type", "etag"),
                 note="/pretty/about serves pretty/about.html"),
            Case(id="pu-about-slash", path="/pretty/about/",
                 note="the trailing slash is stripped before .html is appended"),
            Case(id="pu-about-html", path="/pretty/about.html",
                 note="the explicit form still works"),
            Case(id="pu-nested", path="/pretty/nested"),
            Case(id="pu-missing", path="/pretty/nothing", body_mode="html"),
            Case(id="pu-root", path="/", body_mode="html"),
            Case(id="pu-dir", path="/pretty/", body_mode="html"),
            Case(id="pu-file", path="/test.txt"),
            Case(id="pu-txt", path="/medium",
                 note="only .html is appended, so this one misses"),
            Case(id="pu-head", method="HEAD", path="/pretty/about",
                 headers_extra=("content-type",)),
        ),
    ),

    # -- readme ------------------------------------------------------------
    Session(
        id="readme",
        argv=("--readme",),
        cases=(
            Case(id="rd-root", path="/", body_mode="html",
                 note="README.md is rendered into the listing by comrak"),
            Case(id="rd-readmedir", path="/readmedir/", body_mode="html"),
            Case(id="rd-no-readme", path="/dira/", body_mode="html",
                 note="a directory without one renders no block at all"),
            Case(id="rd-raw", path="/?raw=true", body_mode="html",
                 note="the raw listing has no readme block either way"),
            Case(id="rd-file", path="/README.md",
                 headers_extra=("content-type",),
                 note="the file itself is still served as markdown source"),
            Case(id="rd-empty", path="/emptydir/", body_mode="html"),
        ),
    ),

    # -- indexing off ------------------------------------------------------
    Session(
        id="disable-indexing",
        argv=("--disable-indexing",),
        cases=(
            Case(id="di-root", path="/", body_mode="html",
                 note="the listing is refused, not empty"),
            Case(id="di-dir", path="/dira/", body_mode="html"),
            Case(id="di-file", path="/test.txt",
                 headers_extra=("content-type", "etag"),
                 note="files are still served: only the listing is off"),
            Case(id="di-nested-file", path="/dira/test.txt"),
            Case(id="di-archive", path="/?download=tar", body_mode="html"),
            Case(id="di-404", path="/nope", body_mode="html"),
        ),
        alive_path="/test.txt",
    ),

    # -- route prefixes ----------------------------------------------------
    Session(
        id="route-prefix",
        argv=("--route-prefix", "myprefix"),
        cases=tuple([
            Case(id="rp-root", path="/myprefix/", body_mode="html",
                 note="everything moves under the prefix"),
            Case(id="rp-root-noslash", path="/myprefix", body_mode="shape",
                 headers_extra=("location",)),
            Case(id="rp-file", path="/myprefix/test.txt"),
            Case(id="rp-dir", path="/myprefix/dira/", body_mode="html"),
            Case(id="rp-bare-root", path="/", body_mode="html",
                 note="the unprefixed root is a 404 now"),
            Case(id="rp-bare-file", path="/test.txt", body_mode="html"),
            Case(id="rp-wrong-prefix", path="/other/test.txt",
                 body_mode="html"),
            Case(id="rp-double", path="/myprefix/myprefix/", body_mode="html"),
            Case(id="rp-archive", path="/myprefix/dira/?download=zip",
                 body_mode="archive"),
        ] + static_route_cases("rp")),
        discover_path="/myprefix/",
        alive_path="/myprefix/",
        note="--route-prefix also moves the nonce static routes under it",
    ),
    Session(
        id="route-prefix-slashes",
        argv=("--route-prefix", "/slashed/"),
        cases=(
            Case(id="rps-root", path="/slashed/", body_mode="html",
                 note="surrounding slashes are trimmed by the parser"),
            Case(id="rps-file", path="/slashed/test.txt"),
            Case(id="rps-bare", path="/", body_mode="html"),
        ),
        alive_path="/slashed/",
    ),
    Session(
        id="random-route",
        argv=("--random-route",),
        cases=tuple([
            Case(id="rr-root", path="{route}/", body_mode="html",
                 note="the prefix is generated per boot and read off stdout"),
            Case(id="rr-file", path="{route}/test.txt"),
            Case(id="rr-bare", path="/", body_mode="html"),
            Case(id="rr-guess", path="/000000/", body_mode="html"),
        ] + static_route_cases("rr")),
        discover_path="{route}/",
        alive_path="{route}/",
        note="with --random-route the static routes deliberately sit at the "
             "bare root, so as not to leak the prefix",
    ),

    # -- symlinks ----------------------------------------------------------
    Session(
        id="symlinks-default",
        argv=_RAW,
        cases=(
            Case(id="sl-links", path="/links/", body_mode="html"),
            Case(id="sl-to-file", path="/links/to-file",
                 headers_extra=("content-type", "etag")),
            Case(id="sl-to-dir", path="/links/to-dir/", body_mode="html"),
            Case(id="sl-to-dir-noslash", path="/links/to-dir",
                 body_mode="shape", headers_extra=("location",)),
            Case(id="sl-through", path="/links/to-dir/test.txt"),
            Case(id="sl-dangling", path="/links/dangling", body_mode="html"),
            Case(id="sl-archive", path="/links/?download=tar",
                 body_mode="archive"),
        ),
    ),
    Session(
        id="no-symlinks",
        argv=("--no-symlinks",),
        cases=(
            Case(id="ns-links", path="/links/", body_mode="html",
                 note="symlinked entries vanish from the listing"),
            Case(id="ns-to-file", path="/links/to-file", body_mode="html"),
            Case(id="ns-to-dir", path="/links/to-dir/", body_mode="html"),
            Case(id="ns-through", path="/links/to-dir/test.txt",
                 body_mode="html"),
            Case(id="ns-dangling", path="/links/dangling", body_mode="html"),
            Case(id="ns-real-file", path="/test.txt"),
            Case(id="ns-archive", path="/links/?download=tar",
                 body_mode="archive"),
        ),
    ),
    Session(
        id="show-symlink-info",
        argv=("--show-symlink-info",),
        cases=(
            Case(id="ssi-links", path="/links/", body_mode="html",
                 note="the listing marks symlinked entries"),
            Case(id="ssi-root", path="/", body_mode="html"),
        ),
    ),

    # -- archives ----------------------------------------------------------
    Session(
        id="archives",
        argv=("--enable-tar", "--enable-tar-gz", "--enable-zip"),
        cases=tuple(archive_cases("ar") + [
            Case(id="ar-root-listing", path="/", body_mode="html",
                 note="all three download links appear in the footer"),
        ]),
    ),
    Session(
        id="archives-tar-only",
        argv=("--enable-tar",),
        cases=(
            Case(id="at-listing", path="/", body_mode="html",
                 note="only the enabled method is offered"),
            Case(id="at-tar", path="/dira/?download=tar", body_mode="archive",
                 headers_extra=("content-type", "content-disposition")),
            Case(id="at-zip", path="/dira/?download=zip", body_mode="html",
                 note="a disabled method is a 403, not a 404"),
            Case(id="at-targz", path="/dira/?download=tar_gz",
                 body_mode="html"),
        ),
    ),
    Session(
        id="archives-hidden",
        argv=("--enable-tar", "--enable-zip", "--hidden"),
        cases=(
            Case(id="ah-tar", path="/dira/?download=tar", body_mode="archive",
                 note="dotfiles are inside the archive when --hidden is on"),
            Case(id="ah-zip", path="/dira/?download=zip", body_mode="archive"),
            Case(id="ah-tar-root", path="/?download=tar", body_mode="archive"),
        ),
    ),
    Session(
        id="archives-no-symlinks",
        argv=("--enable-tar", "--no-symlinks"),
        cases=(
            Case(id="ans-tar", path="/links/?download=tar",
                 body_mode="archive",
                 note="symlinks excluded from the archive too"),
        ),
    ),

    # -- compression -------------------------------------------------------
    Session(
        id="compress",
        argv=("--compress-response", "--enable-tar"),
        cases=(
            Case(id="cp-gzip", path="/", headers=(("Accept-Encoding", "gzip"),),
                 decompress="gzip", body_mode="html",
                 headers_extra=("content-encoding", "vary")),
            Case(id="cp-br", path="/", headers=(("Accept-Encoding", "br"),),
                 decompress="br", body_mode="html",
                 headers_extra=("content-encoding", "vary")),
            Case(id="cp-zstd", path="/",
                 headers=(("Accept-Encoding", "zstd"),),
                 decompress="zstd", body_mode="html",
                 headers_extra=("content-encoding", "vary")),
            Case(id="cp-deflate", path="/",
                 headers=(("Accept-Encoding", "deflate"),),
                 decompress="deflate", body_mode="html",
                 headers_extra=("content-encoding", "vary"),
                 note="zlib-wrapped, not raw; the decompressor accepts either"),
            Case(id="cp-identity", path="/",
                 headers=(("Accept-Encoding", "identity"),),
                 body_mode="html", headers_extra=("content-encoding",)),
            Case(id="cp-none", path="/", body_mode="html",
                 headers_extra=("content-encoding", "vary"),
                 note="no Accept-Encoding at all"),
            Case(id="cp-star", path="/", headers=(("Accept-Encoding", "*"),),
                 body_mode="html", headers_extra=("content-encoding", "vary"),
                 note="`*` accepts anything, and the answer is identity"),
            Case(id="cp-qvalues", path="/",
                 headers=(("Accept-Encoding", "gzip;q=0.1, br;q=0.9"),),
                 decompress="br", body_mode="html",
                 headers_extra=("content-encoding",),
                 note="the highest q wins"),
            Case(id="cp-gzip-refused", path="/",
                 headers=(("Accept-Encoding", "gzip;q=0"),),
                 body_mode="html", headers_extra=("content-encoding", "vary"),
                 note="q=0 is a refusal, so the listing arrives uncompressed"),
            Case(id="cp-unknown", path="/",
                 headers=(("Accept-Encoding", "exotic"),),
                 body_mode="html", headers_extra=("content-encoding", "vary")),
            Case(id="cp-file-gzip", path="/medium.txt",
                 headers=(("Accept-Encoding", "gzip"),), decompress="gzip",
                 headers_extra=("content-encoding", "content-type", "etag")),
            Case(id="cp-binary-gzip", path="/media/pic.png",
                 headers=(("Accept-Encoding", "gzip"),), body_mode="binary",
                 headers_extra=("content-encoding", "content-type"),
                 note="an already-compressed type is not recompressed"),
            # Gunzipped first, then graded as the archive it is. Comparing the
            # gzip bytes instead would grade the compression *level*, which no
            # client can observe and which every compressor picks for itself --
            # actix used flate2's `Compression::fast()`, and a port that says
            # `GzipEncoder::new` gets flate2's default. The member list plus
            # `content-encoding: gzip` is the whole of what a client sees.
            Case(id="cp-archive-gzip", path="/dira/?download=tar",
                 headers=(("Accept-Encoding", "gzip"),), decompress="gzip",
                 body_mode="archive",
                 headers_extra=("content-encoding", "content-type")),
            Case(id="cp-404-gzip", path="/nope",
                 headers=(("Accept-Encoding", "gzip"),), decompress="gzip",
                 body_mode="html",
                 note="the error page goes through the same encoder"),
        ),
    ),
    Session(
        id="compress-off",
        argv=_RAW,
        cases=(
            Case(id="cpo-gzip", path="/", headers=(("Accept-Encoding", "gzip"),),
                 body_mode="html", headers_extra=("content-encoding", "vary"),
                 note="without the flag, Accept-Encoding changes nothing"),
            Case(id="cpo-file", path="/medium.txt",
                 headers=(("Accept-Encoding", "gzip, br, zstd"),),
                 headers_extra=("content-encoding",)),
        ),
    ),

    # -- authentication ----------------------------------------------------
    # ``auth=`` on the session attaches credentials to every case that does not
    # carry its own header, so the unauthenticated and wrong-credential cases
    # state theirs explicitly. ``basic()`` builds the header rather than the
    # session doing it, so a case can be wrong on purpose.
    Session(
        id="auth-plain",
        argv=("--auth", "joe:pw123"),
        auth=("joe", "pw123"),
        cases=tuple([
            Case(id="ap-ok-root", path="/", body_mode="html"),
            Case(id="ap-ok-file", path="/test.txt",
                 headers_extra=("etag", "content-type")),
            Case(id="ap-ok-dir", path="/dira/", body_mode="html"),
            Case(id="ap-none", path="/", anonymous=True,
                 body_mode="html",
                 headers_extra=("www-authenticate", "content-type")),
            Case(id="ap-none-file", path="/test.txt",
                 anonymous=True, body_mode="html",
                 headers_extra=("www-authenticate",)),
            Case(id="ap-wrong-pass", path="/",
                 headers=(("Authorization", basic("joe", "nope")),),
                 body_mode="html", headers_extra=("www-authenticate",)),
            Case(id="ap-wrong-user", path="/",
                 headers=(("Authorization", basic("nobody", "pw123")),),
                 body_mode="html"),
            Case(id="ap-empty-pass", path="/",
                 headers=(("Authorization", basic("joe", "")),),
                 body_mode="html"),
            Case(id="ap-malformed", path="/",
                 headers=(("Authorization", "Basic not-base64!"),),
                 body_mode="html"),
            Case(id="ap-not-basic", path="/",
                 headers=(("Authorization", "Bearer sometoken"),),
                 body_mode="html"),
            Case(id="ap-no-colon", path="/",
                 headers=(("Authorization", "Basic am9lcHcxMjM="),),
                 body_mode="html", note="base64 of 'joepw123', no colon"),
            Case(id="ap-lowercase-scheme", path="/",
                 headers=(("Authorization", "basic " +
                           basic("joe", "pw123").split(" ", 1)[1]),),
                 body_mode="html",
                 note="the scheme is case-insensitive per RFC 7235"),
            Case(id="ap-404", path="/nope", body_mode="html",
                 note="authenticated, but still not found"),
            Case(id="ap-archive", path="/dira/?download=tar",
                 body_mode="shape"),
        ] + static_route_cases("ap")),
        note="the nonce static routes sit outside the authenticated scope in "
             "the baseline, so they answer without credentials -- surprising, "
             "observable, and therefore contract",
    ),
    Session(
        id="auth-sha256",
        argv=("--auth",
              "bob:sha256:23d47445adfb8991789b459b6ba1b974"
              "d727d310aa9d80b7c2875b9430c0ba25"),
        auth=("bob", "pw123"),
        cases=(
            Case(id="a256-ok", path="/", body_mode="html"),
            Case(id="a256-ok-file", path="/test.txt"),
            Case(id="a256-wrong", path="/",
                 headers=(("Authorization", basic("bob", "nope")),),
                 body_mode="html"),
            Case(id="a256-plaintext-as-hash", path="/",
                 headers=(("Authorization", basic(
                     "bob", "23d47445adfb8991789b459b6ba1b974"
                            "d727d310aa9d80b7c2875b9430c0ba25")),),
                 body_mode="html",
                 note="sending the hash as the password must not authenticate"),
            Case(id="a256-none", path="/", anonymous=True,
                 body_mode="html", headers_extra=("www-authenticate",)),
        ),
    ),
    Session(
        id="auth-sha512",
        argv=("--auth",
              "sue:sha512:aa5a1976568caa1b89f67459a1a6289884c7af87d3d36b32"
              "f44d61358555a0c2e5006e9fcdffb356a3423615a34bf9f2a42950d00ae1"
              "ad9d72d37a240745bd18"),
        auth=("sue", "pw123"),
        cases=(
            Case(id="a512-ok", path="/", body_mode="html"),
            Case(id="a512-wrong", path="/",
                 headers=(("Authorization", basic("sue", "nope")),),
                 body_mode="html"),
            Case(id="a512-none", path="/", anonymous=True,
                 body_mode="html", headers_extra=("www-authenticate",)),
        ),
    ),
    Session(
        id="auth-multi",
        argv=("--auth", "joe:pw123", "--auth", "bill:"),
        auth=("joe", "pw123"),
        cases=(
            Case(id="am-joe", path="/", body_mode="html"),
            Case(id="am-bill-empty", path="/",
                 headers=(("Authorization", basic("bill", "")),),
                 body_mode="html",
                 note="an empty password is accepted deliberately upstream"),
            Case(id="am-bill-nonempty", path="/",
                 headers=(("Authorization", basic("bill", "x")),),
                 body_mode="html"),
            Case(id="am-unknown", path="/",
                 headers=(("Authorization", basic("sue", "pw123")),),
                 body_mode="html"),
        ),
    ),
    Session(
        id="auth-file",
        argv=("--auth-file", "{authfile}"),
        auth=("joe", "pw123"),
        cases=(
            Case(id="af-joe-plain", path="/", body_mode="html"),
            Case(id="af-bob-sha256", path="/",
                 headers=(("Authorization", basic("bob", "pw123")),),
                 body_mode="html"),
            Case(id="af-sue-sha512", path="/",
                 headers=(("Authorization", basic("sue", "pw123")),),
                 body_mode="html"),
            Case(id="af-bill-empty", path="/",
                 headers=(("Authorization", basic("bill", "")),),
                 body_mode="html"),
            Case(id="af-wrong", path="/",
                 headers=(("Authorization", basic("joe", "wrong")),),
                 body_mode="html"),
            Case(id="af-none", path="/", anonymous=True,
                 body_mode="html", headers_extra=("www-authenticate",)),
        ),
        note="all four line forms the file parser accepts, in one file",
    ),
    Session(
        id="auth-with-prefix",
        argv=("--auth", "joe:pw123", "--route-prefix", "guarded"),
        auth=("joe", "pw123"),
        cases=tuple([
            Case(id="awp-ok", path="/guarded/", body_mode="html"),
            Case(id="awp-none", path="/guarded/",
                 anonymous=True, body_mode="html",
                 headers_extra=("www-authenticate",)),
            Case(id="awp-bare", path="/", body_mode="html",
                 note="outside the scope, so a 404 rather than a 401"),
        ] + static_route_cases("awp")),
        discover_path="/guarded/",
        alive_path="/guarded/",
    ),

    # -- custom headers ----------------------------------------------------
    #
    # The `Rmb` spelling below is frozen, not stale. These strings are request
    # inputs: they go into `fingerprint()`, and the server echoes them into the
    # responses `data/responses.json.gz` recorded from State A. Renaming them --
    # which a project-wide rename once did -- invalidates that capture and stops
    # the stage building, for a token that names nothing and means nothing. Any
    # new spelling requires a fresh capture; see `harness/capture.py`.
    Session(
        id="headers",
        argv=("--header", "X-Rmb-One:first",
              "--header", "X-Rmb-Two: second",
              "--header", "Cache-Control:no-store",
              "--header", "X-Rmb-Empty:"),
        cases=(
            Case(id="hd-root", path="/", body_mode="html",
                 headers_extra=("x-rmb-one", "x-rmb-two", "cache-control",
                                "x-rmb-empty")),
            Case(id="hd-file", path="/test.txt",
                 headers_extra=("x-rmb-one", "x-rmb-two", "cache-control",
                                "content-type", "etag")),
            Case(id="hd-404", path="/nope", body_mode="html",
                 headers_extra=("x-rmb-one", "cache-control"),
                 note="the custom headers survive the error path"),
            Case(id="hd-favicon", path="{favicon}",
                 headers_extra=("x-rmb-one", "content-type")),
            Case(id="hd-head", method="HEAD", path="/test.txt",
                 headers_extra=("x-rmb-one", "x-rmb-two")),
            Case(id="hd-redirect", path="/dira", body_mode="shape",
                 headers_extra=("x-rmb-one", "location")),
        ),
        note="DefaultHeaders wraps everything, including errors and redirects",
    ),
    Session(
        id="headers-override",
        argv=("--header", "Content-Type:application/x-rmb",
              "--header", "Server:rmb-test"),
        cases=(
            Case(id="hdo-file", path="/test.txt",
                 headers_extra=("content-type", "server"),
                 note="DefaultHeaders does not override a header already set"),
            Case(id="hdo-root", path="/", body_mode="html",
                 headers_extra=("content-type", "server")),
        ),
    ),

    # -- presentation ------------------------------------------------------
    Session(
        id="title",
        argv=("--title", "RMB <fw05> & \"friends\""),
        cases=(
            Case(id="ti-root", path="/", body_mode="html",
                 note="the title is HTML-escaped in <title> and in the header"),
            Case(id="ti-dir", path="/dira/", body_mode="html"),
            Case(id="ti-raw", path="/?raw=true", body_mode="html"),
        ),
    ),
    Session(
        id="theme-squirrel",
        argv=("--color-scheme", "squirrel"),
        cases=tuple([
            Case(id="th-root", path="/", body_mode="html",
                 note="the picker marks the chosen light theme"),
        ] + static_route_cases("th")),
    ),
    Session(
        id="theme-dark-monokai",
        argv=("--color-scheme", "archlinux", "--color-scheme-dark", "monokai"),
        cases=tuple([
            Case(id="thd-root", path="/", body_mode="html"),
        ] + static_route_cases("thd")),
        note="the stylesheet is base + light + a dark media query",
    ),
    Session(
        id="hide-theme-selector",
        argv=("--hide-theme-selector",),
        cases=(
            Case(id="hts-root", path="/", body_mode="html"),
            Case(id="hts-dir", path="/dira/", body_mode="html"),
        ),
    ),
    Session(
        id="hide-version-footer",
        argv=("--hide-version-footer",),
        cases=(
            Case(id="hvf-root", path="/", body_mode="html"),
            Case(id="hvf-404", path="/nope", body_mode="html"),
        ),
    ),
    Session(
        id="wget-footer",
        argv=("--show-wget-footer",),
        cases=(
            Case(id="wg-root", path="/", body_mode="html",
                 note="the footer prints a runnable wget command with the "
                      "bound address and cut-dirs count in it"),
            Case(id="wg-dir", path="/dira/", body_mode="html"),
            Case(id="wg-deep", path="/very/deeply/nested/", body_mode="html",
                 note="--cut-dirs tracks the depth"),
            Case(id="wg-raw", path="/?raw=true", body_mode="html"),
        ),
    ),
    Session(
        id="qrcode",
        argv=("--qrcode",),
        cases=(
            Case(id="qr-root", path="/", body_mode="html",
                 note="the SVG path is an encoding of the absolute URL, which "
                      "is why the port is pinned"),
            Case(id="qr-dir", path="/dira/", body_mode="html"),
            Case(id="qr-deep", path="/very/deeply/nested/", body_mode="html"),
        ),
    ),
    Session(
        id="presentation-combined",
        argv=("--qrcode", "--show-wget-footer", "--hide-version-footer",
              "--title", "combined", "--color-scheme", "zenburn",
              "--enable-tar", "--enable-zip", "--readme", "--hidden",
              "--dirs-first", "--show-symlink-info"),
        cases=tuple([
            Case(id="pc-root", path="/", body_mode="html",
                 note="nine flags in one page: the whole chrome at once"),
            Case(id="pc-dir", path="/dira/", body_mode="html"),
            Case(id="pc-links", path="/links/", body_mode="html"),
            Case(id="pc-raw", path="/?raw=true", body_mode="html"),
            Case(id="pc-readmedir", path="/readmedir/", body_mode="html"),
        ] + static_route_cases("pc")),
    ),

    # -- single file -------------------------------------------------------
    Session(
        id="single-file",
        tree="file",
        argv=_RAW,
        cases=(
            Case(id="sf-root", path="/",
                 headers_extra=("content-type", "etag", "last-modified"),
                 note="serving a file routes through file_handler, not Files"),
            Case(id="sf-empty-path", path="",
                 note="the resource is registered for both '' and '/'"),
            Case(id="sf-head", method="HEAD", path="/",
                 headers_extra=("content-type", "etag")),
            Case(id="sf-post", method="POST", path="/", body_mode="shape",
                 note="file_handler is registered with web::to, so any method"),
            Case(id="sf-put", method="PUT", path="/", body_mode="shape"),
            Case(id="sf-other", path="/anything", body_mode="html"),
            Case(id="sf-range", path="/", headers=(("Range", "bytes=0-3"),),
                 headers_extra=("content-range", "accept-ranges")),
            Case(id="sf-query", path="/?download=tar", body_mode="shape"),
        ),
        alive_path="/",
    ),
    Session(
        id="single-file-index",
        tree="file",
        argv=("--index", "test.txt"),
        cases=(
            Case(id="sfi-root", path="/", headers_extra=("content-type",)),
        ),
        alive_path="/",
    ),

    # -- symlinked serve root ----------------------------------------------
    Session(
        id="symlinked-root",
        tree="symlink",
        argv=_RAW,
        cases=(
            Case(id="slr-root", path="/", body_mode="html",
                 note="the serve path itself is a symlink"),
            Case(id="slr-file", path="/test.txt"),
            Case(id="slr-dir", path="/dira/", body_mode="html"),
        ),
    ),

    # -- empty root --------------------------------------------------------
    Session(
        id="empty-root",
        tree="empty",
        argv=("--enable-tar", "--readme", "--qrcode"),
        cases=(
            Case(id="er-root", path="/", body_mode="html",
                 note="an empty listing still has chrome, breadcrumb and QR"),
            Case(id="er-archive", path="/?download=tar", body_mode="archive",
                 note="an archive of nothing is still a valid archive"),
            Case(id="er-404", path="/anything", body_mode="html"),
        ),
    ),

    # -- uploads -----------------------------------------------------------
    # Every upload session is ``mutating``, so it gets a freshly materialised
    # tree. Case order is contract: the listing read after a POST is a different
    # page from the one read before it, and that difference is the evidence the
    # upload landed.
    Session(
        id="upload",
        argv=("--upload-files",),
        mutating=True,
        cases=(
            Case(id="up-listing-before", path="/", body_mode="html",
                 note="the upload form appears in the listing"),
            Case(id="up-post-root", method="POST", path="/upload?path=.",
                 multipart=(("file_to_upload", "uploaded.txt",
                             "text/plain", b"hello from the harness\n"),),
                 body_mode="shape", headers_extra=("location",),
                 note="303 back to the Referer, or / if there is none"),
            Case(id="up-get-uploaded", path="/uploaded.txt",
                 headers_extra=("content-type", "etag")),
            Case(id="up-listing-after", path="/", body_mode="html",
                 note="the new entry is in the listing, with its own mtime"),
            Case(id="up-post-referer", method="POST",
                 path="/upload?path=uploads",
                 headers=(("Referer", "/uploads/"),),
                 multipart=(("file_to_upload", "second.txt",
                             "text/plain", b"second\n"),),
                 body_mode="shape", headers_extra=("location",),
                 note="the redirect follows the Referer header"),
            Case(id="up-listing-uploads", path="/uploads/", body_mode="html"),
            Case(id="up-duplicate", method="POST", path="/upload?path=uploads",
                 multipart=(("file_to_upload", "existing.txt",
                             "text/plain", b"clobber\n"),),
                 body_mode="html",
                 note="409 without --overwrite-files"),
            Case(id="up-existing-unchanged", path="/uploads/existing.txt",
                 note="and the original bytes are still there"),
            Case(id="up-dotdot", method="POST", path="/upload?path=../",
                 multipart=(("file_to_upload", "escape.txt",
                             "text/plain", b"escape\n"),),
                 body_mode="html", note="the path parameter is sanitised"),
            Case(id="up-dotdot-name", method="POST", path="/upload?path=.",
                 multipart=(("file_to_upload", "../escape2.txt",
                             "text/plain", b"escape\n"),),
                 body_mode="html", note="so is the filename"),
            Case(id="up-absolute-name", method="POST", path="/upload?path=.",
                 multipart=(("file_to_upload", "/tmp/absolute.txt",
                             "text/plain", b"absolute\n"),),
                 body_mode="shape",
                 note="an absolute filename is reduced to its last component"),
            Case(id="up-hidden-name", method="POST", path="/upload?path=.",
                 multipart=(("file_to_upload", ".hidden_upload",
                            "text/plain", b"hidden\n"),),
                 body_mode="html",
                 note="rejected: hidden paths are off without --hidden"),
            Case(id="up-no-filename", method="POST", path="/upload?path=.",
                 multipart=(("file_to_upload", None, "text/plain", b"x\n"),),
                 body_mode="html",
                 note="a part with no filename cannot be saved"),
            Case(id="up-empty-file", method="POST", path="/upload?path=.",
                 multipart=(("file_to_upload", "empty.txt", "text/plain",
                             b""),),
                 body_mode="shape"),
            Case(id="up-multiple-parts", method="POST", path="/upload?path=.",
                 multipart=(("file_to_upload", "multi1.txt", "text/plain",
                             b"one\n"),
                            ("file_to_upload", "multi2.txt", "text/plain",
                             b"two\n")),
                 body_mode="shape",
                 note="every part in one request is saved"),
            Case(id="up-get-multi1", path="/multi1.txt"),
            Case(id="up-get-multi2", path="/multi2.txt"),
            Case(id="up-nonexistent-dir", method="POST",
                 path="/upload?path=no/such/dir",
                 multipart=(("file_to_upload", "x.txt", "text/plain", b"x\n"),),
                 body_mode="html"),
            Case(id="up-path-is-file", method="POST",
                 path="/upload?path=test.txt",
                 multipart=(("file_to_upload", "x.txt", "text/plain", b"x\n"),),
                 body_mode="html",
                 note="uploading into a file, not a directory"),
            Case(id="up-not-multipart", method="POST", path="/upload?path=.",
                 data=b"just some bytes",
                 headers=(("Content-Type", "text/plain"),),
                 body_mode="html"),
            Case(id="up-bad-boundary", method="POST", path="/upload?path=.",
                 data=b"--wrongboundary\r\n\r\n",
                 headers=(("Content-Type",
                           "multipart/form-data; boundary=other"),),
                 body_mode="html"),
            Case(id="up-no-path-param", method="POST", path="/upload",
                 multipart=(("file_to_upload", "x.txt", "text/plain", b"x\n"),),
                 body_mode="html",
                 note="the path parameter is required"),
            Case(id="up-get-upload-route", path="/upload?path=.",
                 body_mode="html",
                 note="the route is POST-only"),
            Case(id="up-mkdir-without-flag", method="POST",
                 path="/upload?path=.",
                 multipart=(("mkdir", None, "", b"nope"),), body_mode="html"),
            Case(id="up-final-listing", path="/", body_mode="html",
                 note="the accumulated state of the tree after all of the above"),
        ),
    ),
    Session(
        id="upload-restricted",
        argv=("--upload-files", "uploads"),
        mutating=True,
        cases=(
            Case(id="ur-listing-root", path="/", body_mode="html",
                 note="no upload form outside the allowed directory"),
            Case(id="ur-listing-allowed", path="/uploads/", body_mode="html"),
            Case(id="ur-post-allowed", method="POST",
                 path="/upload?path=uploads",
                 multipart=(("file_to_upload", "ok.txt", "text/plain",
                             b"ok\n"),), body_mode="shape"),
            Case(id="ur-post-allowed-sub", method="POST",
                 path="/upload?path=uploads/sub",
                 multipart=(("file_to_upload", "ok2.txt", "text/plain",
                             b"ok\n"),), body_mode="shape",
                 note="a subdirectory of an allowed directory is allowed"),
            Case(id="ur-post-forbidden", method="POST", path="/upload?path=.",
                 multipart=(("file_to_upload", "no.txt", "text/plain",
                             b"no\n"),), body_mode="html",
                 note="403 outside the allowed set"),
            Case(id="ur-post-forbidden-dir", method="POST",
                 path="/upload?path=dira",
                 multipart=(("file_to_upload", "no.txt", "text/plain",
                             b"no\n"),), body_mode="html"),
            Case(id="ur-get-ok", path="/uploads/ok.txt"),
            Case(id="ur-final", path="/uploads/", body_mode="html"),
        ),
    ),
    Session(
        id="upload-multi-dir",
        argv=("--upload-files", "uploads", "--upload-files", "dira"),
        mutating=True,
        cases=(
            Case(id="umd-uploads", method="POST", path="/upload?path=uploads",
                 multipart=(("file_to_upload", "a.txt", "text/plain", b"a\n"),),
                 body_mode="shape"),
            Case(id="umd-dira", method="POST", path="/upload?path=dira",
                 multipart=(("file_to_upload", "b.txt", "text/plain", b"b\n"),),
                 body_mode="shape"),
            Case(id="umd-dirb", method="POST", path="/upload?path=dirb",
                 multipart=(("file_to_upload", "c.txt", "text/plain", b"c\n"),),
                 body_mode="html"),
            Case(id="umd-listing-dira", path="/dira/", body_mode="html"),
        ),
    ),
    Session(
        id="upload-overwrite",
        argv=("--upload-files", "--overwrite-files"),
        mutating=True,
        cases=(
            Case(id="uo-before", path="/uploads/existing.txt"),
            Case(id="uo-post", method="POST", path="/upload?path=uploads",
                 multipart=(("file_to_upload", "existing.txt", "text/plain",
                             b"overwritten by the harness\n"),),
                 body_mode="shape"),
            Case(id="uo-after", path="/uploads/existing.txt",
                 headers_extra=("content-type",),
                 note="the bytes changed, and so did the length"),
            Case(id="uo-listing", path="/uploads/", body_mode="html"),
        ),
    ),
    Session(
        id="upload-mkdir",
        argv=("--upload-files", "--mkdir"),
        mutating=True,
        cases=(
            Case(id="mk-listing-before", path="/", body_mode="html",
                 note="the mkdir form appears alongside the upload form"),
            Case(id="mk-create", method="POST", path="/upload?path=.",
                 multipart=(("mkdir", None, "", b"created"),),
                 body_mode="shape", headers_extra=("location",)),
            Case(id="mk-listing-after", path="/", body_mode="html"),
            Case(id="mk-get-created", path="/created/", body_mode="html"),
            Case(id="mk-nested", method="POST", path="/upload?path=.",
                 multipart=(("mkdir", None, "", b"one/two/three"),),
                 body_mode="shape",
                 note="create_dir_all, so intermediate levels appear"),
            Case(id="mk-get-nested", path="/one/two/three/", body_mode="html"),
            Case(id="mk-backslash", method="POST", path="/upload?path=.",
                 multipart=(("mkdir", None, "", b"back\\slash"),),
                 body_mode="shape",
                 note="backslashes are rewritten to forward slashes"),
            Case(id="mk-get-backslash", path="/back/slash/", body_mode="html"),
            Case(id="mk-dotdot", method="POST", path="/upload?path=.",
                 multipart=(("mkdir", None, "", b"../escaped"),),
                 body_mode="html"),
            Case(id="mk-hidden", method="POST", path="/upload?path=.",
                 multipart=(("mkdir", None, "", b".hidden_new"),),
                 body_mode="html"),
            Case(id="mk-absolute", method="POST", path="/upload?path=.",
                 multipart=(("mkdir", None, "", b"/tmp/absolute"),),
                 body_mode="shape"),
            Case(id="mk-existing", method="POST", path="/upload?path=.",
                 multipart=(("mkdir", None, "", b"dira"),),
                 body_mode="shape",
                 note="create_dir_all is idempotent, so this succeeds"),
            Case(id="mk-empty-name", method="POST", path="/upload?path=.",
                 multipart=(("mkdir", None, "", b""),), body_mode="html"),
            Case(id="mk-in-subdir", method="POST", path="/upload?path=uploads",
                 multipart=(("mkdir", None, "", b"sub-created"),),
                 body_mode="shape"),
            Case(id="mk-listing-uploads", path="/uploads/", body_mode="html"),
            Case(id="mk-final", path="/", body_mode="html"),
        ),
    ),
    Session(
        id="upload-hidden",
        argv=("--upload-files", "--mkdir", "--hidden"),
        mutating=True,
        cases=(
            Case(id="uh-hidden-file", method="POST", path="/upload?path=.",
                 multipart=(("file_to_upload", ".hidden_upload", "text/plain",
                             b"hidden\n"),), body_mode="shape",
                 note="--hidden also permits uploading hidden names"),
            Case(id="uh-hidden-dir", method="POST", path="/upload?path=.",
                 multipart=(("mkdir", None, "", b".hidden_created"),),
                 body_mode="shape"),
            Case(id="uh-listing", path="/", body_mode="html"),
            Case(id="uh-get", path="/.hidden_upload"),
        ),
    ),
    Session(
        id="upload-media-type",
        argv=("--upload-files", "--media-type", "image",
              "--media-type", "audio"),
        cases=(
            Case(id="umt-listing", path="/", body_mode="html",
                 note="the form's accept attribute is built from the flags"),
        ),
    ),
    Session(
        id="upload-raw-media-type",
        argv=("--upload-files", "--raw-media-type", ".png,.jpg,image/*"),
        cases=(
            Case(id="urm-listing", path="/", body_mode="html",
                 note="the raw form is passed through verbatim"),
        ),
    ),
    Session(
        id="upload-no-symlinks",
        argv=("--upload-files", "--mkdir", "--no-symlinks"),
        mutating=True,
        cases=(
            Case(id="uns-through-symlink", method="POST",
                 path="/upload?path=links/to-dir",
                 multipart=(("file_to_upload", "x.txt", "text/plain", b"x\n"),),
                 body_mode="html",
                 note="uploading through a symlink is refused"),
            Case(id="uns-mkdir-through-symlink", method="POST",
                 path="/upload?path=links/to-dir",
                 multipart=(("mkdir", None, "", b"nope"),), body_mode="html"),
            Case(id="uns-plain", method="POST", path="/upload?path=uploads",
                 multipart=(("file_to_upload", "y.txt", "text/plain", b"y\n"),),
                 body_mode="shape"),
        ),
    ),
    Session(
        id="upload-with-auth",
        argv=("--upload-files", "--mkdir", "--auth", "joe:pw123"),
        auth=("joe", "pw123"),
        mutating=True,
        cases=(
            Case(id="uwa-post-ok", method="POST", path="/upload?path=.",
                 multipart=(("file_to_upload", "authed.txt", "text/plain",
                             b"authed\n"),), body_mode="shape"),
            Case(id="uwa-post-anon", method="POST", path="/upload?path=.",
                 anonymous=True,
                 multipart=(("file_to_upload", "anon.txt", "text/plain",
                             b"anon\n"),), body_mode="html",
                 headers_extra=("www-authenticate",),
                 note="an unauthenticated upload must not land"),
            Case(id="uwa-listing", path="/", body_mode="html"),
        ),
    ),
    Session(
        id="upload-with-prefix",
        argv=("--upload-files", "--route-prefix", "up"),
        mutating=True,
        cases=(
            Case(id="uwp-post", method="POST", path="/up/upload?path=.",
                 multipart=(("file_to_upload", "prefixed.txt", "text/plain",
                             b"prefixed\n"),), body_mode="shape",
                 headers_extra=("location",)),
            Case(id="uwp-bare-post", method="POST", path="/upload?path=.",
                 multipart=(("file_to_upload", "bare.txt", "text/plain",
                             b"bare\n"),), body_mode="html",
                 note="the unprefixed route is gone"),
            Case(id="uwp-listing", path="/up/", body_mode="html"),
        ),
        alive_path="/up/",
    ),

    # -- TLS ---------------------------------------------------------------
    # Three (cert, key) container formats, because rustls-pemfile parses each one
    # separately and a port that wires up only PKCS#8 passes one of these.
    Session(
        id="tls-pkcs8",
        argv=("--tls-cert", "{tls}/cert_rsa.pem",
              "--tls-key", "{tls}/key_pkcs8.pem",
              "--qrcode", "--show-wget-footer"),
        scheme="https",
        cases=(
            Case(id="tp8-root", path="/", body_mode="html",
                 note="over TLS the page's own links say https, and so does "
                      "the QR payload and the wget footer"),
            Case(id="tp8-file", path="/test.txt",
                 headers_extra=("content-type", "etag")),
            Case(id="tp8-dir", path="/dira/", body_mode="html"),
            Case(id="tp8-redirect", path="/dira", body_mode="shape",
                 headers_extra=("location",)),
            Case(id="tp8-404", path="/nope", body_mode="html"),
        ),
    ),
    Session(
        id="tls-pkcs1",
        argv=("--tls-cert", "{tls}/cert_rsa.pem",
              "--tls-key", "{tls}/key_pkcs1.pem"),
        scheme="https",
        cases=(
            Case(id="tp1-root", path="/", body_mode="html"),
            Case(id="tp1-file", path="/test.txt"),
        ),
    ),
    Session(
        id="tls-ec",
        argv=("--tls-cert", "{tls}/cert_ec.pem", "--tls-key", "{tls}/key_ec.pem"),
        scheme="https",
        cases=(
            Case(id="tec-root", path="/", body_mode="html"),
            Case(id="tec-file", path="/test.txt"),
        ),
    ),
    Session(
        id="tls-with-everything",
        argv=("--tls-cert", "{tls}/cert_rsa.pem",
              "--tls-key", "{tls}/key_pkcs8.pem",
              "--auth", "joe:pw123", "--enable-tar", "--compress-response",
              "--upload-files"),
        scheme="https",
        auth=("joe", "pw123"),
        mutating=True,
        cases=(
            Case(id="twe-root", path="/", body_mode="html"),
            Case(id="twe-anon", path="/", anonymous=True, body_mode="html",
                 headers_extra=("www-authenticate",)),
            Case(id="twe-gzip", path="/",
                 headers=(("Accept-Encoding", "gzip"),), decompress="gzip",
                 body_mode="html", headers_extra=("content-encoding",)),
            Case(id="twe-archive", path="/dira/?download=tar",
                 body_mode="archive"),
            Case(id="twe-upload", method="POST", path="/upload?path=.",
                 multipart=(("file_to_upload", "tls-upload.txt", "text/plain",
                             b"over tls\n"),), body_mode="shape"),
            Case(id="twe-listing", path="/", body_mode="html"),
        ),
        note="TLS composes with auth, compression, archives and uploads at once",
    ),

    # -- configuration through the environment ------------------------------
    # Every flag is also an environment variable. A port that reads the CLI and
    # forgets the env is a port that silently ignores half of miniserve's
    # documented configuration surface.
    Session(
        id="env-config",
        argv=_RAW,
        env=(("MINISERVE_HIDDEN", "true"),
             ("MINISERVE_DIRS_FIRST", "true"),
             ("MINISERVE_TITLE", "from the environment"),
             ("MINISERVE_ENABLE_TAR", "true"),
             ("MINISERVE_QRCODE", "true"),
             ("MINISERVE_COLOR_SCHEME", "monokai")),
        cases=(
            Case(id="ev-root", path="/", body_mode="html",
                 note="six settings, none of them on the command line"),
            Case(id="ev-hidden-file", path="/.hidden_file1"),
            Case(id="ev-archive", path="/dira/?download=tar",
                 body_mode="archive"),
        ),
    ),
    Session(
        id="env-overridden",
        argv=("--title", "from the command line"),
        env=(("MINISERVE_TITLE", "from the environment"),),
        cases=(
            Case(id="evo-root", path="/", body_mode="html",
                 note="the command line wins over the environment"),
        ),
    ),

    # -- verbose logging ---------------------------------------------------
    Session(
        id="verbose",
        argv=("--verbose",),
        cases=(
            Case(id="vb-root", path="/", body_mode="html",
                 note="--verbose changes the log, not the response"),
            Case(id="vb-file", path="/test.txt"),
            Case(id="vb-404", path="/nope", body_mode="html"),
        ),
    ),
]


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def _assign_ports(specs: list[Session]) -> tuple[Session, ...]:
    """Hand out pinned ports in declaration order, and validate as we go.

    The validation is here rather than in a test because a corpus that is
    internally inconsistent -- a duplicate case id, a conditional case pointing
    at a case that does not exist, a session whose ``{route}`` placeholder can
    never be filled -- would produce a golden file that quietly measures
    something other than what it claims to.
    """
    from .sessions import PORT_BASE

    out: list[Session] = []
    seen_sessions: set[str] = set()
    seen_cases: set[str] = set()
    for offset, session in enumerate(specs):
        if session.id in seen_sessions:
            raise ValueError(f"duplicate session id {session.id!r}")
        seen_sessions.add(session.id)

        ids = {c.id for c in session.cases}
        for case in session.cases:
            if case.id in seen_cases:
                raise ValueError(f"duplicate case id {case.id!r}")
            seen_cases.add(case.id)
            for spec in (case.etag_from, case.last_modified_from):
                if spec and spec[0] not in ids:
                    raise ValueError(
                        f"{case.id}: derives a header from {spec[0]!r}, which "
                        "is not a case in the same session")
            if "{route}" in case.path and "--random-route" not in session.argv:
                raise ValueError(
                    f"{case.id}: uses {{route}} outside a --random-route "
                    "session")
        if any("{favicon}" in c.path or "{css}" in c.path
               for c in session.cases) and not session.discover_path:
            raise ValueError(f"{session.id}: needs a discover_path")
        out.append(replace(session, port=PORT_BASE + offset))
    return tuple(out)


SESSIONS: tuple[Session, ...] = _assign_ports(SESSION_SPECS)


def case_key(session_id: str, case_id: str) -> str:
    return f"{session_id}::{case_id}"


def all_cases() -> list[tuple[Session, Case]]:
    return [(s, c) for s in SESSIONS for c in s.cases]


def listing_order(session: Session, case: Case) -> tuple[str, bool]:
    """The sort method and dirs-first setting a request is answered under.

    ``sort`` and ``order`` are per-request, the ``--default-sorting-*`` flags
    are per-process, and the query wins where it names a method the baseline
    parses.
    """
    method = "name"
    if "--default-sorting-method" in session.argv:
        method = session.argv[session.argv.index("--default-sorting-method") + 1]
    for part in case.path.partition("?")[2].split("&"):
        key, _, value = part.partition("=")
        if key == "sort" and value in ("name", "size", "date"):
            method = value
    return method, "--dirs-first" in session.argv


def fingerprint() -> str:
    """Digest of the corpus definition.

    Written into the golden file and checked before grading. A golden file
    captured against a different corpus is not a weaker check, it is a different
    check, and comparing against one would produce failures that say nothing
    about the submission.
    """
    payload = []
    for session in SESSIONS:
        payload.append({
            "id": session.id,
            "argv": list(session.argv),
            "port": session.port,
            "tree": session.tree,
            "env": [list(e) for e in session.env],
            "mutating": session.mutating,
            "scheme": session.scheme,
            "auth": list(session.auth) if session.auth else None,
            "alive_path": session.alive_path,
            "discover_path": session.discover_path,
            "cases": [{
                "id": c.id, "method": c.method, "path": c.path,
                "data": c.data.decode("utf-8", "replace") if c.data else None,
                "headers": [list(h) for h in c.headers],
                "multipart": [[p[0], p[1], p[2],
                               p[3].decode("utf-8", "replace")]
                              for p in c.multipart],
                "headers_extra": list(c.headers_extra),
                "headers_skip": list(c.headers_skip),
                "decompress": c.decompress,
                "body_mode": c.body_mode,
                "etag_from": list(c.etag_from) if c.etag_from else None,
                "last_modified_from": (list(c.last_modified_from)
                                       if c.last_modified_from else None),
                "anonymous": c.anonymous,
                "raw_request": c.raw_request,
            } for c in session.cases],
        })
    # The one-shot CLI invocations are part of the same contract and live in the
    # same golden file, so a change to either side has to invalidate it.
    return digest({"sessions": payload, "cli": cli.fingerprint()})


def summary() -> dict:
    """Counts, for the image build and the instruction to quote."""
    cases = all_cases()
    modes: dict[str, int] = {}
    for _, case in cases:
        modes[case.body_mode] = modes.get(case.body_mode, 0) + 1
    return {
        "sessions": len(SESSIONS),
        "cases": len(cases),
        "body_modes": modes,
        "mutating_sessions": sum(1 for s in SESSIONS if s.mutating),
        "tls_sessions": sum(1 for s in SESSIONS if s.scheme == "https"),
        "cli_invocations": len(cli.INVOCATIONS),
        "fingerprint": fingerprint(),
    }
