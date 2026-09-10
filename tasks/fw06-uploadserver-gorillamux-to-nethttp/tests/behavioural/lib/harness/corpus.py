"""The case corpus: what gets asked of the server, and under which flags.

This file defines REQUESTS ONLY.  Expected responses are captured from the State A
oracle and stored as golden records -- nothing here hand-writes a status code or a
header, because a hand-written expectation is a second implementation of the thing
under test and it is the copy that will be wrong.

Cases are grouped by `suite`, which selects the graded module, and each carries a
`why` string.  The `why` is not decoration: when a case fails, the report prints it,
so the failure says what contract broke rather than only which byte differed.

Cases whose `mutates` flag is set get a freshly seeded docroot before they run.
Upload and truncation cases change the tree -- `-max_upload_size` in particular
leaves a partially written file behind on a 413 -- so a later GET would otherwise
observe whatever an earlier case happened to leave.
"""
from __future__ import annotations

import hashlib

# --- profiles ----------------------------------------------------------------
# Flag sets the server is launched with.  Each exists because it moves a
# DIFFERENT axis of observable behaviour; a profile that only reshuffles bytes
# already covered elsewhere is cost without signal.
PROFILES = {
    # CORS defaults ON upstream, so this is the plain configuration.
    "default": [],
    # Removes exactly one header (Access-Control-Allow-Origin) and, notably, does
    # NOT remove Access-Control-Allow-Methods -- handleOptions sets that
    # unconditionally.  An agent who moves CORS into a single middleware and gates
    # all of it on the flag breaks OPTIONS, and only this profile sees it.
    "nocors": ["-enable_cors=false"],
    # Auth is middleware that runs AFTER route matching, which is observable:
    # /filesabc answers 401 (matched, then refused) while /nope answers 404
    # (never matched).  A port that authenticates before dispatch inverts this.
    "auth": ["-enable_auth", "-read_only_tokens", "ro1",
             "-read_write_tokens", "rw1"],
    # MaxBytesReader fires DURING the copy into an already-created file, so 413
    # leaves a truncated file on disk and a retry answers 409.  Every case here
    # mutates.
    "maxsize": ["-max_upload_size", "16"],
}

# Deliberately NOT a profile: -file_naming_strategy.  The strategy is only
# consulted when the multipart filename is empty, and Go's mime/multipart
# classifies a part with filename="" as a non-file field, so FormFile fails first
# and the request 500s before the strategy is reached.  Unreachable via `-F
# file=@x` and via a hand-built filename="" body alike, which also makes
# ResolveFileNamingStrategy's nil return (a latent panic) unreachable.  A profile
# for it would add launches and grade nothing.


def _mp(filename: str, content: bytes, field: str = "file") -> tuple[dict, bytes]:
    """A minimal multipart/form-data body with a fixed boundary."""
    b = "----RepoMorphBenchBoundary"
    body = (
        f"--{b}\r\n"
        f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + content + f"\r\n--{b}--\r\n".encode()
    return {"Content-Type": f"multipart/form-data; boundary={b}"}, body


def case(cid, suite, method, target, why, *, profile="default",
         headers=None, body=None, mutates=False):
    return {
        "id": cid, "suite": suite, "profile": profile, "method": method,
        "target": target, "headers": headers or {}, "body": body,
        "mutates": mutates, "why": why,
    }


def _static_cases():
    """Content-Type, Content-Length and byte-exact bodies across the fixture set."""
    s = "body"
    return [
        case("get-text", s, "GET", "/files/a.txt",
             "the workhorse: a plain text file, extension-driven Content-Type"),
        case("get-nested", s, "GET", "/files/sub/b.txt",
             "a path with a real separator -- {path...} must span it"),
        case("get-json", s, "GET", "/files/data.json",
             "application/json comes from the extension, not from sniffing"),
        case("get-html", s, "GET", "/files/page.html",
             "text/html; charset=utf-8 -- the charset suffix is part of the value"),
        case("get-png", s, "GET", "/files/tiny.png",
             "a binary body compared byte for byte, and image/png by extension"),
        case("get-noext", s, "GET", "/files/README",
             "no extension, so the type is SNIFFED from the first 512 bytes"),
        case("get-big", s, "GET", "/files/big.bin",
             "4096 bytes of application/octet-stream; also the range subject"),
        case("get-space-pct", s, "GET", "/files/sp%20ace.txt",
             "%20 must decode to a space before the filesystem lookup"),
        case("get-plus-literal", s, "GET", "/files/odd+name.txt",
             "a literal + in a path is a plus, not a space (that is query syntax)"),
        case("get-plus-encoded", s, "GET", "/files/odd%2Bname.txt",
             "%2B decodes to the same file as the literal +"),
        case("head-text", "headers", "HEAD", "/files/a.txt",
             "HEAD carries the full header set and no body at all"),
        case("head-big", "headers", "HEAD", "/files/big.bin",
             "Content-Length on HEAD must describe the body that was not sent"),
    ]


def _range_cases():
    s = "headers"
    return [
        case("range-prefix", s, "GET", "/files/big.bin",
             "a single range -- 206, Content-Range, and the sliced body",
             headers={"Range": "bytes=0-99"}),
        case("range-suffix", s, "GET", "/files/a.txt",
             "an open-ended suffix range counts back from the end",
             headers={"Range": "bytes=-3"}),
        case("range-multi", s, "GET", "/files/big.bin",
             "two ranges become multipart/byteranges; the boundary is random and "
             "is masked, everything around it is graded",
             headers={"Range": "bytes=100-199,300-399"}),
        case("range-unsatisfiable", s, "GET", "/files/a.txt",
             "a range past EOF is 416, not a truncated 206",
             headers={"Range": "bytes=999-1999"}),
        case("range-ignored-bad", s, "GET", "/files/a.txt",
             "a syntactically invalid Range is ignored, yielding a plain 200",
             headers={"Range": "bytes=abc"}),
    ]


def _conditional_cases():
    s = "headers"
    stamped = "Thu, 01 Jan 2026 00:00:00 GMT"
    older = "Wed, 01 Jan 2025 00:00:00 GMT"
    return [
        case("ims-equal", s, "GET", "/files/a.txt",
             "If-Modified-Since exactly at the mtime is a 304 -- this is why the "
             "fixture mtimes are stamped to a constant instead of inherited",
             headers={"If-Modified-Since": stamped}),
        case("ims-older", s, "GET", "/files/a.txt",
             "an older If-Modified-Since still yields the body",
             headers={"If-Modified-Since": older}),
        case("ims-head", s, "HEAD", "/files/a.txt",
             "304 on HEAD: no body, and a specific reduced header set",
             headers={"If-Modified-Since": stamped}),
    ]

def _routing_cases():
    """The cases that actually discriminate one router from another."""
    s = "routing"
    put_hdr, put_body = _mp("x.txt", b"z\n")
    return [
        # THE case.  gorilla/mux routes on the decoded r.URL.Path, so %2F is a
        # real separator and this resolves to sub/b.txt.  ServeMux matches on the
        # ESCAPED path, so a port that switches to r.PathValue("path") answers 404
        # here while passing every other static case.  Preserving upstream's
        # regexp over r.URL.Path is what keeps it green.
        case("encoded-slash", s, "GET", "/files/sub%2Fb.txt",
             "%2F is a separator to the decoded-path router: this resolves to "
             "sub/b.txt, and it is the single most discriminating case in the corpus"),
        case("clean-dotdot", s, "GET", "/files/../a.txt",
             "an unclean path is redirected, not resolved -- bodiless 301"),
        case("clean-dotdot-encoded", s, "GET", "/files/%2e%2e/a.txt",
             "percent-encoded dot-dot cleans the same way as the literal form"),
        case("clean-double-slash", s, "GET", "/files//a.txt",
             "a doubled separator is unclean and 301s"),
        case("clean-dot", s, "GET", "/files/./a.txt",
             "a single-dot segment is unclean and 301s"),
        case("clean-sub-dotdot", s, "GET", "/files/sub/../a.txt",
             "cleaning resolves through a real directory too"),
        case("clean-keeps-query", s, "GET", "/files/../a.txt?x=1&y=2",
             "the query survives into the Location header verbatim"),
        # THE OTHER discriminating case, and the one that makes this migration a
        # design problem rather than a transcription.  State A registers
        # PathPrefix("/files"), a STRING-prefix matcher: /filesabc matches it, the
        # handler runs, the ^/files/(.+)$ regexp then fails, and the answer is the
        # file-missing 404 -- notably WITHOUT the CORS header, because that is set
        # later in the success path.  ServeMux has no string-prefix pattern: "/files"
        # matches only itself and "/files/" is a subtree, so NEITHER matches
        # /filesabc, which lands on the catch-all instead and would answer the
        # generic "not found".  Reproducing this needs deliberate work in the
        # fallback handler, and no other case in the corpus reveals whether it was
        # done.
        case("prefix-not-segment", s, "GET", "/filesabc",
             "a STRING-prefix match in State A (PathPrefix), which ServeMux cannot "
             "express: /filesabc must still answer the file-missing 404, without "
             "CORS -- reproducing this is the design work the task is really about"),
        case("prefix-not-segment-deep", s, "GET", "/filesabc/x/y",
             "the same prefix quirk with further segments"),
        case("prefix-nearly", s, "GET", "/file",
             "a shorter path that is NOT a prefix of /files: the generic 404"),
        case("prefix-files-suffix", s, "PUT", "/filesabc",
             "the prefix quirk on the write route, which answers the 405 whose "
             "body names the route template",
             headers=put_hdr, body=put_body, mutates=True),
        case("unmatched", s, "GET", "/nope",
             "the catch-all 404 -- a different body from the file-missing 404"),
        case("missing-file", s, "GET", "/files/nosuch.txt",
             "matched the route, missed the file"),
        case("directory", s, "GET", "/files/sub",
             "a directory is a 404 whose body names it, and it DOES carry CORS -- "
             "the pairing with /filesabc is what pins CORS to the right branch"),
        case("root", s, "GET", "/",
             "the bare root falls to the catch-all"),
        case("files-bare", s, "GET", "/files",
             "the route template with an empty capture"),
        case("files-slash", s, "GET", "/files/",
             "trailing slash with an empty capture"),
        case("method-not-allowed-upload", s, "GET", "/upload",
             "wrong method on a matched route is 405 with an Allow header"),
        case("method-not-allowed-files", s, "DELETE", "/files/a.txt",
             "405 on the files route; the Allow list is the route's, and upstream "
             "omits HEAD and OPTIONS from it -- an inconsistency to preserve"),
        case("upload-trailing-slash", s, "POST", "/upload/",
             "POST /upload/ does NOT match POST /upload: it is the catch-all 404"),
    ]


def _options_cases():
    s = "routing"
    return [
        case("options-upload-204", s, "OPTIONS", "/upload",
             "204 with Access-Control-Allow-Methods, in every profile"),
        case("options-file", s, "OPTIONS", "/files/a.txt",
             "OPTIONS on the files route advertises GET, PUT, HEAD -- a wider set "
             "than the 405 Allow header on the same route reports"),
        case("options-missing", s, "OPTIONS", "/files/nosuch.txt",
             "OPTIONS does not stat the file, so a missing path still answers 204"),
    ]

def _upload_cases():
    s = "upload"
    h_ok, b_ok = _mp("up.txt", b"uploaded body\n")
    h_nested, b_nested = _mp("deep.txt", b"deep\n")
    h_empty, b_empty = _mp("empty.txt", b"")
    return [
        case("post-upload", s, "POST", "/upload",
             "the happy path: 201 and a JSON body naming the stored path",
             headers=h_ok, body=b_ok, mutates=True),
        case("post-upload-empty", s, "POST", "/upload",
             "a zero-byte upload is still a 201",
             headers=h_empty, body=b_empty, mutates=True),
        case("post-upload-nofile", s, "POST", "/upload",
             "no file part at all -- FormFile fails and the server answers 500",
             headers={"Content-Type": "application/x-www-form-urlencoded"},
             body=b"x=1", mutates=True),
        case("post-upload-wrong-field", s, "POST", "/upload",
             "the part is present but under the wrong field name: still a 500",
             headers=_mp("up.txt", b"x", field="notfile")[0],
             body=_mp("up.txt", b"x", field="notfile")[1], mutates=True),
        # PUT goes through the SAME processUpload path as POST, so it also expects
        # multipart/form-data with a "file" part -- a raw request body yields
        # "cannot obtain the uploaded content" with a 500.  Not obvious from the
        # method name, and worth a case of its own below.
        case("put-new", s, "PUT", "/files/put-new.txt",
             "PUT creates a file and answers 201 with the stored path",
             headers=_mp("ignored.txt", b"put body\n")[0],
             body=_mp("ignored.txt", b"put body\n")[1], mutates=True),
        case("put-raw-body", s, "PUT", "/files/put-raw.txt",
             "a RAW body is a 500, not a 201: PUT shares POST's multipart path, "
             "so the form part is mandatory on both",
             headers={"Content-Type": "application/octet-stream"},
             body=b"raw\n", mutates=True),
        case("put-existing", s, "PUT", "/files/a.txt",
             "PUT over an existing file is refused with 409, not silently accepted",
             headers=_mp("a.txt", b"overwrite\n")[0],
             body=_mp("a.txt", b"overwrite\n")[1], mutates=True),
        case("put-overwrite-flag", s, "PUT", "/files/a.txt?overwrite=true",
             "?overwrite=true turns that 409 into a 201 -- a whole branch that is "
             "invisible without the query parameter",
             headers=_mp("a.txt", b"overwritten\n")[0],
             body=_mp("a.txt", b"overwritten\n")[1], mutates=True),
        case("put-overwrite-boolish", s, "PUT", "/files/a.txt?overwrite=1",
             "the flag is parsed boolishly, so 1 works as well as true",
             headers=_mp("a.txt", b"boolish\n")[0],
             body=_mp("a.txt", b"boolish\n")[1], mutates=True),
        case("put-nested-new", s, "PUT", "/files/sub/put-deep.txt",
             "PUT into an existing subdirectory",
             headers=_mp("x", b"deep put\n")[0],
             body=_mp("x", b"deep put\n")[1], mutates=True),
        case("put-encoded-slash", s, "PUT", "/files/sub%2Fput-enc.txt",
             "the decoded-path router treats %2F as a separator on writes too",
             headers=_mp("x", b"enc put\n")[0],
             body=_mp("x", b"enc put\n")[1], mutates=True),
        case("put-bare-files", s, "PUT", "/files",
             "an empty capture takes the `path == \"\"` branch: a 405 whose body "
             "names the route template, not a 404",
             headers=_mp("x", b"y\n")[0], body=_mp("x", b"y\n")[1], mutates=True),
        case("post-nested-upload", s, "POST", "/upload",
             "a multipart filename containing a separator",
             headers=h_nested, body=b_nested, mutates=True),
    ]


def _auth_cases():
    s = "auth"
    p = "auth"
    h_ok, b_ok = _mp("auth-up.txt", b"authed\n")
    return [
        case("auth-no-token", s, "GET", "/files/a.txt",
             "no credential at all is 401", profile=p),
        case("auth-query-token", s, "GET", "/files/a.txt?token=ro1",
             "a read-only token in the query string is accepted", profile=p),
        case("auth-bearer", s, "GET", "/files/a.txt",
             "the same token as a Bearer header is accepted",
             profile=p, headers={"Authorization": "Bearer ro1"}),
        case("auth-basic-rejected", s, "GET", "/files/a.txt",
             "Basic is not a supported scheme here, so it is a 401",
             profile=p, headers={"Authorization": "Basic cm8xOg=="}),
        case("auth-bad-token", s, "GET", "/files/a.txt?token=nope",
             "a well-formed but unknown token is a 401", profile=p),
        case("auth-head-401", s, "HEAD", "/files/a.txt",
             "the 401 on HEAD omits Content-Type and body -- a reduced header set "
             "that a hand-written error path tends to get wrong", profile=p),
        case("auth-ro-cannot-write", s, "PUT", "/files/auth-new.txt",
             "a read-only token is refused on a write with 401",
             profile=p, headers=_mp("x", b"nope\n")[0],
             body=_mp("x", b"nope\n")[1], mutates=True),
        case("auth-rw-can-write", s, "PUT", "/files/auth-rw.txt?token=rw1",
             "a read-write token succeeds on the same request",
             profile=p, headers=_mp("x", b"yes\n")[0],
             body=_mp("x", b"yes\n")[1], mutates=True),
        case("auth-ro-cannot-post", s, "POST", "/upload?token=ro1",
             "read-only is refused on upload", profile=p,
             headers=h_ok, body=b_ok, mutates=True),
        case("auth-rw-can-post", s, "POST", "/upload?token=rw1",
             "read-write succeeds on upload", profile=p,
             headers=h_ok, body=b_ok, mutates=True),
        # The ordering evidence.  Both are unauthenticated; they differ only in
        # whether the ROUTE matched, so the pair pins auth to run after dispatch.
        case("auth-prefix-401", s, "GET", "/filesabc",
             "matched the files route then refused: 401, not 404 -- auth runs "
             "AFTER route matching and this is how you can tell", profile=p),
        case("auth-unmatched-404", s, "GET", "/nope",
             "never matched a route, so auth never ran: 404, not 401", profile=p),
        case("auth-405-not-401", s, "DELETE", "/files/a.txt",
             "method rejection also precedes auth: 405, not 401", profile=p),
        case("auth-clean-precedes", s, "GET", "/files/../a.txt?token=ro1",
             "path cleaning precedes auth: a 301 with the token preserved in "
             "Location, which an authenticate-first port turns into a 401", profile=p),
        case("auth-options-open", s, "OPTIONS", "/upload",
             "OPTIONS is answered 204 even unauthenticated -- this is the property "
             "the readiness probe depends on", profile=p),
    ]

def _nocors_cases():
    s = "headers"
    p = "nocors"
    return [
        case("nocors-get", s, "GET", "/files/a.txt",
             "with CORS disabled the Allow-Origin header is gone", profile=p),
        case("nocors-options", s, "OPTIONS", "/upload",
             "but Allow-Methods is still set -- handleOptions writes it "
             "unconditionally, so a single flag-gated CORS middleware breaks this",
             profile=p),
        case("nocors-404", s, "GET", "/files/sub",
             "the directory 404 loses only Allow-Origin", profile=p),
    ]


def _maxsize_cases():
    s = "flows"
    p = "maxsize"
    h16, b16 = _mp("exact16.txt", b"0123456789abcdef")          # exactly 16 bytes
    h17, b17 = _mp("over17.txt", b"0123456789abcdefg")          # 17 bytes
    return [
        case("maxsize-exact", s, "POST", "/upload",
             "16 bytes is accepted: the boundary is inclusive",
             profile=p, headers=h16, body=b16, mutates=True),
        case("maxsize-over", s, "POST", "/upload",
             "17 bytes is refused with 413 -- and MaxBytesReader fires DURING the "
             "copy, so the response also carries Connection: close",
             profile=p, headers=h17, body=b17, mutates=True),
        case("maxsize-put-over", s, "PUT", "/files/big-put.txt",
             "the same limit applies to PUT",
             profile=p, headers=_mp("big-put.txt", b"0123456789abcdefg")[0],
             body=_mp("big-put.txt", b"0123456789abcdefg")[1], mutates=True),
    ]


def _flow_cases():
    """Multi-step flows: a sequence whose later steps observe earlier writes."""
    s = "flows"
    h, b = _mp("flow.txt", b"flow body\n")
    ha, ba = _mp("x.txt", b"flow a\n")
    h2, b2 = _mp("x.txt", b"flow a2\n")
    h3, b3 = _mp("x.txt", b"flow a3\n")
    ht, bt = _mp("trunc.txt", b"0123456789abcdefg")   # 17 bytes, over the limit
    hr, br = _mp("trunc.txt", b"retry\n")             # 6 bytes, within it
    return [
        case("flow-put-then-get", s, "PUT", "/files/flow-a.txt",
             "step 1 of 5: create. NOTE the stored path comes from the URL, not "
             "from the part's filename -- the two deliberately disagree here",
             headers=ha, body=ba, mutates=True),
        case("flow-put-then-get.get", s, "GET", "/files/flow-a.txt",
             "step 2 of 5: read back exactly the bytes of the form part, with a "
             "wall-clock Last-Modified (masked) rather than the stamped constant"),
        case("flow-put-then-get.notfilename", s, "GET", "/files/x.txt",
             "step 3 of 5: and the part's filename was NOT used as the path"),
        case("flow-put-then-get.again", s, "PUT", "/files/flow-a.txt",
             "step 4 of 5: the second PUT is a 409",
             headers=h2, body=b2),
        case("flow-put-then-get.overwrite", s, "PUT",
             "/files/flow-a.txt?overwrite=true",
             "step 5 of 5: with the flag the same request is a 201",
             headers=h3, body=b3),
        case("flow-post-then-get", s, "POST", "/upload",
             "step 1 of 2: upload, whose 201 body names the stored path",
             headers=h, body=b, mutates=True),
        case("flow-post-then-get.get", s, "GET", "/files/flow.txt",
             "step 2 of 2: the uploaded bytes are retrievable at that path"),
        # The 413 is NOT a clean rejection.  MaxBytesReader fires mid-copy, after
        # the destination file is already open, so the first 16 bytes survive on
        # disk.  These three steps pin that down twice over: the GET returns the
        # truncated prefix, and the follow-up PUT sees a file that exists.  A port
        # that checks Content-Length up front and refuses before opening anything
        # is cleaner than upstream -- and fails both of the later two steps.
        case("flow-truncation", s, "POST", "/upload",
             "step 1 of 3: an over-limit upload is refused with 413",
             profile="maxsize", headers=ht, body=bt, mutates=True),
        case("flow-truncation.retry", s, "GET", "/files/trunc.txt",
             "step 2 of 3: the refused upload still left the first 16 bytes on "
             "disk, so this is a 200 whose body is the truncated prefix",
             profile="maxsize"),
        case("flow-truncation.reput", s, "PUT", "/files/trunc.txt",
             "step 3 of 3: the same fact a second way -- a within-limit write to "
             "that path is a 409, because the partial file is really there",
             profile="maxsize", headers=hr, body=br),
    ]


def all_cases() -> list[dict]:
    cases = (
        _static_cases() + _range_cases() + _conditional_cases()
        + _routing_cases() + _options_cases() + _upload_cases()
        + _auth_cases() + _nocors_cases() + _maxsize_cases() + _flow_cases()
    )
    seen = set()
    for c in cases:
        if c["id"] in seen:
            raise ValueError(f"duplicate case id: {c['id']}")
        seen.add(c["id"])
        if c["profile"] not in PROFILES:
            raise ValueError(f"{c['id']}: unknown profile {c['profile']}")
    return cases


# Flow cases are ordered sequences: an id containing "." is a continuation of the
# case named by its prefix and MUST run immediately after it, without re-seeding.
def is_continuation(cid: str) -> bool:
    return "." in cid


def fingerprint() -> str:
    """Digest of every REQUEST this corpus makes, and of the flags behind them.

    Covers the request and nothing else -- id, profile, method, target, headers,
    body, and each profile's flag list.  `why` is prose and excluded, so a comment
    can be improved without invalidating a recording.

    What this is for: data/golden-statea.json holds the answers State A gave to
    these exact requests, and it cannot notice being asked about different ones.
    Edit a target from `/files/sub%2Fb.txt` to `/files/sub/b.txt` and every
    comparison still runs, still passes for the recorded response, and no longer
    grades the behaviour the case exists to grade.  The recording carries this
    digest and the stage image refuses to build when the two disagree.

    Deliberately not a digest of case COUNT or of the id set: two edits that
    cancel out keep both, which is exactly the situation where a check is worth
    having.
    """
    h = hashlib.sha256()
    for pid in sorted(PROFILES):
        h.update(f"profile {pid} {PROFILES[pid]!r}\n".encode())
    for c in sorted(all_cases(), key=lambda c: c["id"]):
        h.update("\x1f".join((
            c["id"], c["profile"], c["suite"], c["method"], c["target"],
            repr(sorted(c["headers"].items())),
            "" if c["body"] is None else hashlib.sha256(c["body"]).hexdigest(),
            "mutates" if c["mutates"] else "",
        )).encode() + b"\x1e")
    return h.hexdigest()


if __name__ == "__main__":
    import collections
    import json
    import sys

    cs = all_cases()
    if "--json" in sys.argv:
        json.dump({"version": 1, "profiles": PROFILES, "cases": cs},
                  sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        print(f"{len(cs)} cases")
        for k, v in sorted(collections.Counter(c["suite"] for c in cs).items()):
            print(f"  {k:10s} {v}")
        for k, v in sorted(collections.Counter(c["profile"] for c in cs).items()):
            print(f"  profile {k:10s} {v}")

