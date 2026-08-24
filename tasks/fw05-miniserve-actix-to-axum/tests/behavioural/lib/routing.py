"""Which module grades which recorded case.

The suite is split by **observable surface**, not by assertion family and not by
case count.  That choice is the whole reason this file exists, so it is worth
stating what the alternatives cost.

*Splitting by assertion family* -- one module for statuses, one for headers, one
for bodies -- reads well and grades badly.  Every module touches every feature, so
a submission that dropped archives entirely loses a few per cent of three modules
instead of one whole module, and the report says "headers differ in 31 places"
where the useful sentence is "the archive endpoint is gone".

*Splitting by case count* is worse, because this corpus is wildly uneven.
``default`` holds 133 cases and ``qrcode`` holds 1.  Weighting by volume prices a
whole retired surface at a rounding error: the QR code is either generated or it
is not, and "not" is a visible hole in the migration whether it took one
assertion to find or a hundred.

So: a module is a surface a user could describe in a sentence, its weight answers
"if this were the only thing that regressed, how much of the migration would be
missing?", and the corpus is routed into it case by case.  A case may be reached
by exactly one module -- double-graded cases would let one defect be charged
twice and make the weights unreadable.

Most sessions belong wholly to one surface, because they exist to exercise one
flag.  ``default`` is the exception: it is 133 cases of unflagged server across
listings, file serving, method dispatch and error pages, so it is routed by case
id.  Its ids are prefixed by intent (``d-sort-``, ``d-cond-``, ``d-range-``,
``d-method-``, ``d-404-``), which is what makes this tractable; ``self_check``
fails the image build if a case ever falls outside every rule.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from harness import corpus

# --------------------------------------------------------------------------- #
# The surfaces
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Surface:
    """One module: the sessions it owns, and its slice of ``default``."""

    id: str
    title: str
    #: Sessions graded here in full.  The first one listed is the session whose
    #: per-session facts (the final liveness read, the served-tree digest) this
    #: module asserts.
    sessions: tuple[str, ...] = ()
    #: ``default`` case ids claimed exactly.  Beats a prefix rule.
    exacts: frozenset[str] = field(default_factory=frozenset)
    #: ``default`` case id prefixes claimed.
    prefixes: tuple[str, ...] = ()
    #: What this surface is, and what a port most often gets wrong on it.  Becomes
    #: the docstring of the generated module, so it is written for whoever reads a
    #: failing report rather than for whoever maintains the table.
    about: str = ""


SURFACES: tuple[Surface, ...] = (
    Surface(
        id="listing",
        title="directory listings",
        sessions=("default", "hidden", "dirs-first", "sort-size-asc",
                  "sort-date-asc", "sort-name-asc-dirs-first", "index",
                  "index-missing", "readme", "disable-indexing",
                  "symlinks-default", "no-symlinks", "show-symlink-info",
                  "symlinked-root", "empty-root"),
        exacts=frozenset({
            "d-root", "d-dir-dira", "d-dir-dirb", "d-dir-dirc", "d-deep",
            "d-empty", "d-onechild", "d-media", "d-links",
            "d-raw", "d-raw-false", "d-raw-garbage", "d-unknown-param",
            "d-order-unknown", "d-range-on-dir",
            # Query-string validation on a listing: the answer is a listing (or a
            # listing-shaped error), so it is this surface's business rather than
            # the error pages'.
            "d-err-sort", "d-err-order", "d-err-download", "d-err-raw",
            # The *default* hidden policy.  The `hidden` session measures the
            # flag; these three measure what happens without it, which is a
            # listing decision that happens to be reported as a 404.
            "d-404-hidden-default", "d-404-hidden-dir-default",
            "d-404-hidden-nested-default",
        }),
        prefixes=("d-sort-", "d-sortdir-"),
        about="""\
The rendered index: which rows, in which order, with which columns, under which
breadcrumbs.  This is the largest surface because it is what miniserve mostly
is, and the sessions here vary the parts of it a flag controls -- hidden files,
the directories-first ordering, each sort key and direction, a served
``index.html``, a rendered README, indexing switched off entirely, and the
three symlink policies.

A port that renders a listing at all gets the file names right.  What it gets
wrong quietly is everything beside them: the humanised size column (``1.4 KiB``
where miniserve writes ``1.40 KiB``), the timestamp format, the sort-link query
strings, the ``.symlink`` row class, and the ordering when two entries collide.
""",
    ),
    Surface(
        id="static",
        title="serving a file",
        sessions=("single-file", "single-file-index"),
        exacts=frozenset({"d-favicon", "d-css", "d-favicon-head", "d-css-head"}),
        prefixes=("d-file-", "d-media-", "d-cond-", "d-range-"),
        about="""\
Handing back the bytes of one file, and the header contract that comes with it:
the guessed content type, ``Accept-Ranges``, the ETag and ``Last-Modified``,
the disposition, and what a conditional or ranged request does to all of them.

This is where the two frameworks differ most mechanically.  actix-files answers
a range with 206 and a ``Content-Range``, refuses an unsatisfiable one with
416, honours ``If-None-Match`` with 304 and ``If-Match`` with 412, and derives
an ETag from the file's metadata.  tower-http's ``ServeDir`` emits no ETag at
all by default, which makes every conditional request unconditional -- a
difference no status code shows and every caching client sees.
""",
    ),
    Surface(
        id="routing",
        title="paths, prefixes, redirects and method dispatch",
        sessions=("spa", "pretty-urls", "route-prefix", "route-prefix-slashes",
                  "random-route"),
        exacts=frozenset({"d-dir-noslash", "d-favicon-post"}),
        prefixes=("d-method-",),
        about="""\
Where a request lands: the SPA fallback, extensionless pretty URLs, an explicit
``--route-prefix`` with and without surrounding slashes, the per-boot
``--random-route``, the trailing-slash redirect, and which methods are answered
at all.

The prefix sessions are the ones that catch a port out, because the prefix has
to appear in three unrelated places at once -- the routes that match, the links
in every rendered page, and the ``Location`` of a redirect.  A port that mounts
its router under the prefix but builds hrefs without it serves a page whose
every link 404s, and passes any test that only looks at status codes.
""",
    ),
    Surface(
        id="errors",
        title="what a request that cannot be served gets",
        prefixes=("d-404-", "d-err-"),
        about="""\
The failure paths: a missing file, a path that escapes the served root, a
hidden file when hidden files are off, a malformed query, a directory operation
on a file.  All of them from the unflagged server, routed here by case id.

miniserve renders these as pages, not as bare status lines -- a 404 carries the
same shell, theme and footer as a listing does.  Two things go wrong in a port.
It answers the right status with the wrong body, usually a framework default
like axum's empty ``404 Not Found``; or it answers a *different* status, most
often turning a deliberate 400 into a 404 because the query parser failed
before the route matched.  Both are visible to a user and neither is visible in
a diff of the happy path.
""",
    ),
    Surface(
        id="archives",
        title="tar, tar.gz and zip on demand",
        sessions=("archives", "archives-tar-only", "archives-hidden",
                  "archives-no-symlinks"),
        about="""\
Downloading a directory as a stream: the three formats, the subset of them a
flag permits, and how hidden files and symlinks are treated inside the archive.

Graded on the member list and the member contents rather than on bytes, because
two correct archivers disagree about tar block padding, zip extra fields and
compression level.  What that leaves is exactly the part that goes wrong: an
archive rooted at the wrong prefix, a symlink inlined where the baseline
skipped it, hidden files included when ``--hidden`` was off, or a stream that
simply does not unpack.
""",
    ),
    Surface(
        id="encodings",
        title="content encodings on the wire",
        sessions=("compress", "compress-off"),
        about="""\
What ``Accept-Encoding`` negotiates, and what the response then says about
itself.  Both directions are graded: with compression on, a client offering
gzip, deflate, brotli or zstd gets one of them and it decodes; with compression
off, the same requests get identity bodies.

The failure that matters is a port that advertises an encoding it did not
apply, or applies one and forgets to say so.  Either produces a body no client
can read, while the status line and the length both look reasonable.
""",
    ),
    Surface(
        id="upload",
        title="multipart upload and directory creation",
        sessions=("upload", "upload-restricted", "upload-multi-dir",
                  "upload-overwrite", "upload-mkdir", "upload-hidden",
                  "upload-media-type", "upload-raw-media-type",
                  "upload-no-symlinks", "upload-with-auth",
                  "upload-with-prefix"),
        # Upload is off unless enabled, and that negative is part of this surface.
        exacts=frozenset({"d-404-upload-without-flag", "d-err-mkdir-no-flag"}),
        about="""\
The only surface that writes to disk: ``multipart/form-data`` upload, directory
creation, and the eleven configurations that constrain them -- an allowed
directory list, an overwrite policy, a media-type restriction, uploads into
hidden or symlinked paths, and upload behind auth or a route prefix.

Two things are graded, and the second is the reason this weighs as much as it
does.  The response: a 303 back to the listing, or the specific error for a
rejected name, type or destination.  And the *filesystem*: the served tree is
digested at the end of each session without mtimes, so a port that returns the
baseline's exact 303 and writes the file to the wrong place, under the wrong
name or with the wrong bytes fails here and nowhere else.
""",
    ),
    Surface(
        id="auth",
        title="HTTP basic auth",
        sessions=("auth-plain", "auth-sha256", "auth-sha512", "auth-multi",
                  "auth-file", "auth-with-prefix"),
        about="""\
``--auth`` in all the spellings miniserve accepts: a plaintext password, a
``sha256:`` or ``sha512:`` digest, several accounts at once, and accounts read
from a file.  Graded on what an unauthenticated, wrongly authenticated and
correctly authenticated request each receive.

The interesting failures are not "auth is missing" -- that is loud.  They are
the 401 that arrives without a ``WWW-Authenticate`` header, so no browser ever
prompts; the digest comparison that succeeds against the wrong account; and the
middleware mounted so that one route (the favicon, the stylesheet, the upload
endpoint) answers without credentials.
""",
    ),
    Surface(
        id="tls",
        title="HTTPS",
        sessions=("tls-pkcs8", "tls-pkcs1", "tls-ec", "tls-with-everything"),
        about="""\
``--tls-cert`` and ``--tls-key`` with the three key encodings a user is likely
to have on disk -- PKCS#8, PKCS#1 and an EC key -- plus one session that turns
TLS on alongside everything else, because the interaction is where it breaks.

This surface is mostly a build question wearing a runtime hat.  TLS support is
behind a Cargo feature; a port that rewires ``Cargo.toml`` while migrating is
one edit away from a binary whose ``--tls-cert`` flag no longer exists, and
this is where that shows up as four sessions that cannot be started rather than
as a compile error.
""",
    ),
    Surface(
        id="presentation",
        title="title, themes, QR code and footers",
        sessions=("presentation-combined", "title", "theme-squirrel",
                  "theme-dark-monokai", "hide-theme-selector",
                  "hide-version-footer", "wget-footer", "qrcode"),
        about="""\
The parts of the page that are not the file list: the ``<title>`` and its
default, the colour scheme and the picker that switches it, the QR code, the
wget footer and the version footer.

Each is a whole feature that a port can drop while rendering a page that looks
correct, which is why they are one surface rather than a footnote to the
listing. The QR code is the sharpest of them: it is a generated SVG path, so
matching the module count means the encoded text, the error-correction level
and the version all agree -- a port that regenerates it from a different URL
passes every other assertion and fails this one.
""",
    ),
    Surface(
        id="config",
        title="configuration reaching the response",
        sessions=("headers", "headers-override", "env-config", "env-overridden",
                  "verbose"),
        about="""\
Configuration that is only observable in what comes back: ``--header`` adding a
response header, ``--header`` overriding one miniserve sets itself, the
``MINISERVE_*`` environment aliases, and the precedence between an alias and an
explicit flag.

Small, and kept as its own surface because it is the one place where the
*plumbing* is what is being graded.  Every flag here has to survive the whole
path from clap through the config struct into the response, and a port that
reads it correctly and never applies it looks identical to one that never read
it -- except in these fifteen cases.
""",
    ),
)

#: Modules that grade recorded cases, in declaration order.
SURFACE_BY_ID = {s.id: s for s in SURFACES}

#: The session ``default``'s cases are routed; every other session belongs whole.
#: It still appears in one surface's ``sessions`` -- ``listing`` -- because its
#: per-session facts (was the server still up at the end, did the served tree
#: change) have to be asserted somewhere, and a listing is what it mostly serves.
SPLIT_SESSION = "default"


def owner(session_id: str, case_id: str) -> str | None:
    """The module id that grades this case, or None if nothing claims it."""
    if session_id == SPLIT_SESSION:
        for surface in SURFACES:
            if case_id in surface.exacts:
                return surface.id
        for surface in SURFACES:
            if any(case_id.startswith(p) for p in surface.prefixes):
                return surface.id
        return None
    for surface in SURFACES:
        if session_id in surface.sessions:
            return surface.id
    return None


def home(session_id: str) -> str | None:
    """The module that asserts this session's per-session facts."""
    for surface in SURFACES:
        if session_id in surface.sessions:
            return surface.id
    return None


def cases_for(module_id: str) -> list[tuple[object, object]]:
    """``(session, case)`` pairs this module grades, in corpus order."""
    return [(session, case) for session, case in corpus.all_cases()
            if owner(session.id, case.id) == module_id]


def sessions_for(module_id: str) -> list[object]:
    """Sessions whose per-session facts this module asserts."""
    surface = SURFACE_BY_ID.get(module_id)
    if surface is None:
        return []
    wanted = set(surface.sessions)
    return [s for s in corpus.SESSIONS if s.id in wanted]


# --------------------------------------------------------------------------- #
# Self-check
# --------------------------------------------------------------------------- #
# Run at image build time.  A routing table is the kind of file that goes stale
# silently: add a session to the corpus and forget to claim it, and 25 cases stop
# being graded while every module still passes.  The failure mode is invisible
# from the outside, which is exactly why it is asserted at build time rather than
# trusted.


def audit() -> dict[str, object]:
    """What is routed where, plus anything that is not routed at all."""
    unclaimed: list[str] = []
    per_module: dict[str, int] = {s.id: 0 for s in SURFACES}
    for session, case in corpus.all_cases():
        module_id = owner(session.id, case.id)
        if module_id is None:
            unclaimed.append(f"{session.id}::{case.id}")
        else:
            per_module[module_id] += 1

    homeless = [s.id for s in corpus.SESSIONS if home(s.id) is None]

    # A session claimed by two surfaces would make `owner` order-dependent.
    doubled: list[str] = []
    for session in corpus.SESSIONS:
        claims = [s.id for s in SURFACES if session.id in s.sessions]
        if len(claims) > 1:
            doubled.append(f"{session.id} claimed by {', '.join(claims)}")

    # Likewise a `default` case matched by two surfaces' prefixes.  Exact-beats-
    # prefix is deliberate and not reported; two prefixes matching is not.
    ambiguous: list[str] = []
    for session, case in corpus.all_cases():
        if session.id != SPLIT_SESSION or case.id in _ALL_EXACTS:
            continue
        by_prefix = [s.id for s in SURFACES
                     if any(case.id.startswith(p) for p in s.prefixes)]
        if len(by_prefix) > 1:
            ambiguous.append(f"{case.id} matched by {', '.join(by_prefix)}")

    empty = sorted(m for m, n in per_module.items() if n == 0)
    return {
        "cases": sum(per_module.values()) + len(unclaimed),
        "routed": sum(per_module.values()),
        "per_module": per_module,
        "unclaimed": unclaimed,
        "homeless_sessions": homeless,
        "doubled_sessions": doubled,
        "ambiguous_cases": ambiguous,
        "empty_modules": empty,
    }


_ALL_EXACTS = frozenset().union(*(s.exacts for s in SURFACES))


def self_check() -> int:
    report = audit()
    problems: list[str] = []
    if report["unclaimed"]:
        cases = report["unclaimed"]
        problems.append(
            f"{len(cases)} recorded case(s) are graded by no module, so they are "
            f"not graded at all: {', '.join(cases[:12])}"
            + (" ..." if len(cases) > 12 else ""))
    if report["homeless_sessions"]:
        problems.append(
            "no module asserts the per-session facts of: "
            + ", ".join(report["homeless_sessions"]))
    if report["doubled_sessions"]:
        problems.append("session claimed twice: "
                        + "; ".join(report["doubled_sessions"]))
    if report["ambiguous_cases"]:
        problems.append("case matched by two prefix rules: "
                        + "; ".join(report["ambiguous_cases"]))
    if report["empty_modules"]:
        problems.append(
            "module(s) with no cases -- a module that grades nothing still "
            "carries weight, so this silently inflates the score: "
            + ", ".join(report["empty_modules"]))

    print(f"routing: {report['routed']}/{report['cases']} cases across "
          f"{len(SURFACES)} surfaces")
    for module_id, count in sorted(report["per_module"].items(),
                                   key=lambda kv: (-kv[1], kv[0])):
        owned = len(SURFACE_BY_ID[module_id].sessions)
        print(f"  {module_id:14s} {count:4d} cases  {owned:2d} session(s)")
    if problems:
        print("\nFAIL:")
        for problem in problems:
            print("  - " + problem)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(self_check())
