"""Live probes: properties that hold for any correct port, checked at grading time.

The behavioural suite compares the submission against a recording of State A. That
is the right test for behaviour, and it is exactly the wrong test for audit,
because a recording is a finite table -- and a finite table can be memorised. A
submission that special-cased the 728 requests in the corpus would score 1.00 on
behavioural compatibility while implementing nothing.

So this module asks questions whose answers were *not* recorded, and could not
have been:

* **Nonce charts.** The chart names are generated from ``secrets`` at grading
  time. No table shipped in a submission can contain them, and no amount of
  studying the corpus reveals them. If pushing ``chart-a7f3e91b`` and fetching it
  back works, something really is storing and serving charts.

* **Storage truth.** After a push returns 201, the object is looked for *on disk*,
  in the backend's own directory, and its bytes are compared with what was
  uploaded. An API that returns a plausible 201 without writing anything fails
  here and nowhere else.

* **Self-consistency.** The index advertises a download URL per chart version and
  a digest; both are followed and checked against the bytes actually served. This
  needs no oracle: the response must agree with itself.

* **Invariance.** The same logical request, dressed differently -- an extra
  harmless header, a different ``User-Agent`` -- must get the same answer. A port
  that recognises the harness and replays canned bytes diverges as soon as the
  request stops looking familiar.

The oracle binary is deliberately absent from the verifier image, so nothing here
can fall back to differential testing against State A. Every assertion below is a
property of a correct implementation, not a comparison.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import secrets
import tarfile
import urllib.parse

#: One nonce per grading run, mixed into every generated name. Regenerated per
#: process, so two runs of the suite never use the same corpus of chart names.
RUN_NONCE = secrets.token_hex(4)


def synth_chart(name: str, version: str) -> bytes:
    """Build a minimal but genuinely valid Helm chart archive.

    ChartMuseum parses this with Helm's own loader, so it has to be a real chart:
    a gzipped tar whose entries live under ``<name>/`` and which contains a
    ``Chart.yaml`` carrying a matching name and version. Anything less is rejected
    with a 400 and would make every probe below fail for the wrong reason.

    Deterministic given its arguments -- fixed mtime, fixed uid/gid, fixed gzip
    mtime -- so the same nonce chart pushed twice is byte-identical. Two probes
    depend on that: the re-push conflict check, and comparing the served bytes with
    the uploaded ones.
    """
    chart_yaml = f"apiVersion: v1\nname: {name}\nversion: {version}\n".encode()
    values_yaml = b"replicaCount: 1\n"

    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for rel, payload in ((f"{name}/Chart.yaml", chart_yaml),
                             (f"{name}/values.yaml", values_yaml)):
            info = tarfile.TarInfo(rel)
            info.size = len(payload)
            info.mtime = 1656633600  # 2022-07-01T00:00:00Z, the fixture epoch
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(payload))
    # mtime=0 in the gzip header too: the default writes 'now', which would make
    # the same chart two different byte strings a second apart.
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", mtime=0) as gz:
        gz.write(raw.getvalue())
    return out.getvalue()


def nonce_name(tag: str, i: int) -> str:
    """A chart name no submission can have precomputed.

    Lower-case and hyphenated because Helm requires it; the nonce is in the middle
    so a name is still readable in a failure message.
    """
    return f"probe-{tag}-{RUN_NONCE}-{i:02d}"


MULTIPART_BOUNDARY = "srbfw04probeboundary"


def multipart(field: str, filename: str, payload: bytes) -> tuple[bytes, str]:
    """A multipart/form-data body, assembled by hand.

    Hand-assembled for the same reason :mod:`harness.wire` does it: ``email.mime``
    folds long header lines, and a folded ``Content-Disposition`` is not what Go's
    ``mime/multipart`` reader expects.
    """
    b = MULTIPART_BOUNDARY
    head = (f"--{b}\r\n"
            f'Content-Disposition: form-data; name="{field}"; '
            f'filename="{filename}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n").encode()
    tail = f"\r\n--{b}--\r\n".encode()
    return head + payload + tail, f"multipart/form-data; boundary={b}"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _obs(oid: str, status: int, headers, body: bytes, **extra) -> dict:
    """One observation.

    Deliberately *not* run through :mod:`harness.normalize`: these probes compare
    a response with another response from the same run, or with bytes this module
    generated, so there is nothing cross-run to mask. Masking here would only
    weaken the comparison.
    """
    hmap: dict[str, list[str]] = {}
    for k, v in headers:
        hmap.setdefault(k.lower(), []).append(v)
    rec = {
        "id": oid,
        "status": status,
        "headers": hmap,
        "body_sha256": sha256(body),
        "body_len": len(body),
    }
    if len(body) <= 4096:
        rec["text"] = body.decode("utf-8", "replace")
        try:
            rec["json"] = json.loads(body)
        except ValueError:
            rec["json"] = None
    rec.update(extra)
    return rec


def storage_files(root: str) -> dict[str, str]:
    """Every file under the storage root, as ``relative path -> sha256``.

    This is the ground truth a push claim is checked against. Read from the
    filesystem rather than from any API, because the whole point is to find out
    whether the API told the truth.
    """
    out: dict[str, str] = {}
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root)
            try:
                with open(full, "rb") as fh:
                    out[rel] = sha256(fh.read())
            except OSError:
                out[rel] = "<<unreadable>>"
    return out


# --- batteries --------------------------------------------------------------
#
# Each returns a dict of observations. They take an already-launched server so a
# single launch can carry several batteries: a launch costs about a second, and
# the audit suite needs thousands of assertions, so the ratio of assertions
# to launches is what keeps the grading run to minutes rather than hours.

N_NONCE = 12


def battery_lifecycle(server) -> dict:
    """Push, list, download, verify, re-push, delete, confirm gone.

    Six independent nonce charts, each taken through the full cycle, with the
    storage directory read directly at three points. This is the battery that
    cannot be faked: the names do not exist until the moment they are pushed.
    """
    out: dict = {"nonce": RUN_NONCE, "charts": {}}
    for i in range(N_NONCE):
        name = nonce_name("life", i)
        version = f"1.{i}.0"
        payload = synth_chart(name, version)
        rec: dict = {"name": name, "version": version,
                     "uploaded_sha256": sha256(payload),
                     "uploaded_len": len(payload)}

        # The existence check BEFORE the push. ``HEAD /api/:repo/charts/:name`` is
        # one of only two HEAD routes ChartMuseum declares, and it is what a helm
        # client uses to find out whether a chart is already published. Asking it
        # for a nonce name at three points in the cycle -- absent, present, absent
        # again -- is the version of the HEAD contract that no lookup table can
        # answer, because the name did not exist when the submission was written.
        st, hd, bd = server.request("HEAD", f"/api/charts/{name}")
        rec["head_before_push"] = _obs("head_before_push", st, hd, bd)
        st, hd, bd = server.request("HEAD", f"/api/charts/{name}/{version}")
        rec["head_version_before_push"] = _obs("head_version_before_push",
                                               st, hd, bd)

        body, ctype = multipart("chart", f"{name}-{version}.tgz", payload)
        st, hd, bd = server.request("POST", "/api/charts", body=body,
                                    headers=[("Content-Type", ctype)])
        rec["push"] = _obs("push", st, hd, bd)

        # Ground truth immediately after the push claim.
        rec["storage_after_push"] = storage_files(server.storage)

        st, hd, bd = server.request("GET", f"/api/charts/{name}")
        rec["list_one"] = _obs("list_one", st, hd, bd)

        st, hd, bd = server.request("GET", f"/api/charts/{name}/{version}")
        rec["describe"] = _obs("describe", st, hd, bd)

        st, hd, bd = server.request("HEAD", f"/api/charts/{name}")
        rec["head_after_push"] = _obs("head_after_push", st, hd, bd)
        st, hd, bd = server.request("HEAD", f"/api/charts/{name}/{version}")
        rec["head_version_after_push"] = _obs("head_version_after_push",
                                              st, hd, bd)

        st, hd, bd = server.request("GET", f"/charts/{name}-{version}.tgz")
        rec["download"] = _obs("download", st, hd, bd)

        st, hd, bd = server.request("GET", "/index.yaml")
        rec["index"] = _obs("index", st, hd, bd, text_full=bd.decode(
            "utf-8", "replace"))

        # A second push of identical bytes: without --allow-overwrite this is a
        # 409, and that is a real contract -- it is what stops a CI job from
        # silently replacing a released chart.
        body2, ctype2 = multipart("chart", f"{name}-{version}.tgz", payload)
        st, hd, bd = server.request("POST", "/api/charts", body=body2,
                                    headers=[("Content-Type", ctype2)])
        rec["repush"] = _obs("repush", st, hd, bd)

        st, hd, bd = server.request("DELETE", f"/api/charts/{name}/{version}")
        rec["delete"] = _obs("delete", st, hd, bd)

        rec["storage_after_delete"] = storage_files(server.storage)

        st, hd, bd = server.request("GET", f"/charts/{name}-{version}.tgz")
        rec["download_after_delete"] = _obs("download_after_delete", st, hd, bd)

        st, hd, bd = server.request("GET", f"/api/charts/{name}/{version}")
        rec["describe_after_delete"] = _obs("describe_after_delete", st, hd, bd)

        st, hd, bd = server.request("HEAD", f"/api/charts/{name}/{version}")
        rec["head_version_after_delete"] = _obs("head_version_after_delete",
                                                st, hd, bd)

        out["charts"][name] = rec
    return out


#: Requests replayed byte-identically several times over. Chosen to span the
#: handlers that read state, because a cache or a map-iteration bug shows up as a
#: response that changes when nothing did.
DETERMINISM_PATHS = [
    ("GET", "/index.yaml"),
    ("GET", "/api/charts"),
    ("GET", "/api/charts/mychart"),
    ("GET", "/api/charts/mychart/0.1.0"),
    ("GET", "/charts/mychart-0.1.0.tgz"),
    ("GET", "/health"),
    ("GET", "/api/charts/does-not-exist"),
    ("GET", "/no/such/route"),
]
DETERMINISM_REPEATS = 4


def battery_determinism(server, *, prefix: str = "") -> dict:
    """The same request, several times, recorded separately each time.

    Repeated at the far end of the battery list rather than back-to-back would be
    stronger still, but four consecutive identical requests already catches the
    defect this exists for: an index assembled by ranging over a Go map, whose
    entry order is randomised per iteration. That bug passes every behavioural test
    on a lucky run.

    ``prefix`` is for the ``--context-path`` launch: that flag moves every route
    including ``/health``, so running the same battery under a prefix is where a
    port that applied the prefix in one place and not another shows up.
    """
    out: dict = {}
    for method, path in DETERMINISM_PATHS:
        full = prefix + path
        runs = []
        for n in range(DETERMINISM_REPEATS):
            st, hd, bd = server.request(method, full)
            runs.append(_obs(f"{method} {full} #{n}", st, hd, bd))
        out[f"{method} {full}"] = runs
    return out


#: A request, and dressings of it that must not change the answer. Every variant
#: is semantically identical to the base by HTTP's own rules, so a port that
#: answers them differently is keying on something it should not be -- which is
#: what a grader-recognising submission does.
INVARIANCE_VARIANTS = [
    ("baseline", []),
    ("extra-header", [("X-Srb-Probe", "1")]),
    ("user-agent", [("User-Agent", "not-the-grader/1.0")]),
    ("accept-any", [("Accept", "*/*")]),
    ("accept-yaml", [("Accept", "application/x-yaml")]),
    ("no-cache", [("Cache-Control", "no-cache")]),
    ("odd-order", [("X-B", "2"), ("X-A", "1")]),
]
INVARIANCE_PATHS = [
    ("GET", "/index.yaml"),
    ("GET", "/api/charts"),
    ("GET", "/charts/mychart-0.1.0.tgz"),
    ("GET", "/health"),
]


def battery_invariance(server) -> dict:
    """The same requests, dressed differently."""
    out: dict = {}
    for method, path in INVARIANCE_PATHS:
        per: dict = {}
        for label, headers in INVARIANCE_VARIANTS:
            st, hd, bd = server.request(method, path, headers=headers)
            per[label] = _obs(f"{label}", st, hd, bd)
        out[f"{method} {path}"] = per
    return out


#: Generated paths, not drawn from the corpus. The point is coverage of the
#: hand-written ``match()`` logic's shape -- depth, separators, method -- rather
#: than of any particular URL, so these are built systematically and their answers
#: are checked for internal consistency rather than against a recording.
def _grid_paths() -> list[str]:
    base = [
        "/api/charts", "/api/charts/", "/api/charts//",
        "/api/charts/mychart", "/api/charts/mychart/", "/api/charts/mychart//",
        "/api/charts/mychart/0.1.0", "/api/charts/mychart/0.1.0/",
        "/api/charts/mychart/0.1.0/extra",
        "/index.yaml", "/index.yaml/", "//index.yaml",
        "/charts/mychart-0.1.0.tgz", "/charts/mychart-0.1.0.tgz/",
        "/charts/", "/charts", "/charts//mychart-0.1.0.tgz",
        "/health", "/health/", "//health",
        "/", "//", "///",
        "/api", "/api/", "/apix/charts",
        "/api/charts/mychart/0.1.0/templates",
        "/api/prov/mychart/0.1.0",
        "/nonexistent", "/nonexistent/deep/path",
        "/api/charts/UPPER", "/api/charts/mychart/0.1.0-SNAPSHOT",
        "/charts/nonexistent-9.9.9.tgz",
        "/api/charts/mychart/9.9.9",
    ]
    return base


#: Read-only methods only, and that is a correctness requirement rather than a
#: preference. Measured, not theorised. Keeping the grid read-only makes it
#: deterministic, safe to share a launch, and safe to assert repeatability on.
GRID_METHODS = ["GET", "HEAD", "OPTIONS"]

#: Write methods are probed separately, and only ever at resources that do not
#: exist: a nonce chart name nothing has pushed. That keeps the observation
#: ("how does the router answer a write to an unknown resource") while removing
#: the side effect.
GRID_WRITE_METHODS = ["POST", "PUT", "DELETE", "PATCH"]


def battery_route_grid(server, *, prefix: str = "") -> dict:
    """Every generated path crossed with every read method, status recorded.

    Nothing here is compared with State A -- the assertions built on it are
    consistency properties, stated in the test module. Bodies are recorded only
    when small, because a 4 KiB archive times a hundred would dominate the record
    for no benefit.

    Under ``prefix`` the whole grid moves, which is the interesting case: with
    ``--context-path=/cm`` the unprefixed paths must all become unknown, and that
    is asserted rather than assumed.
    """
    out: dict = {"prefix": prefix}
    for path in _grid_paths():
        full = prefix + path
        for method in GRID_METHODS:
            st, hd, bd = server.request(method, full)
            out[f"{method} {full}"] = _obs(f"{method} {full}", st, hd, bd)
    return out


def battery_write_discipline(server) -> dict:
    """Write methods aimed at resources that do not exist.

    Every target embeds the run nonce, so none of them can be present and none of
    these requests can destroy seeded state. What is being observed is that the
    router *routes* an unknown write consistently rather than, say, 405-ing one
    shape and 404-ing an identical one, or falling through to a handler that
    panics.
    """
    ghost = nonce_name("ghost", 0)
    targets = [
        f"/api/charts/{ghost}",
        f"/api/charts/{ghost}/1.0.0",
        f"/api/charts/{ghost}/1.0.0/extra",
        f"/charts/{ghost}-1.0.0.tgz",
        f"/api/{ghost}/charts",
        f"/{ghost}",
        f"/{ghost}/deep/path",
    ]
    out: dict = {"ghost": ghost}
    for path in targets:
        for method in GRID_WRITE_METHODS:
            st, hd, bd = server.request(method, path)
            out[f"{method} {path}"] = _obs(f"{method} {path}", st, hd, bd)
    out["storage"] = storage_files(server.storage)
    return out


#: Routes that change state, and therefore must be refused without credentials
#: when authentication is configured. Written out rather than derived, because
#: 'which routes mutate' is a design fact about this API and deriving it from the
#: router would make the test agree with whatever the submission did.
MUTATING_ROUTES = [
    ("POST", "/api/charts"),
    ("POST", "/api/prov"),
    ("DELETE", "/api/charts/mychart/0.1.0"),
    ("DELETE", "/api/charts/mychart/0.2.0"),
    ("DELETE", "/api/charts/otherchart/0.1.0"),
    ("DELETE", "/api/charts/nonexistent/1.0.0"),
]
READ_ROUTES = [
    ("GET", "/index.yaml"),
    ("GET", "/api/charts"),
    ("GET", "/api/charts/mychart"),
    ("GET", "/api/charts/mychart/0.1.0"),
    ("GET", "/charts/mychart-0.1.0.tgz"),
]


def battery_auth(server, *, anonymous_get: bool) -> dict:
    """Unauthenticated requests against every mutating and reading route.

    ``anonymous_get`` records what the launch was configured for, so the test
    module can state the right expectation without re-deriving it: with
    ``--auth-anonymous-get`` reads are open and writes are not, and without it
    everything is closed.
    """
    out: dict = {"anonymous_get": anonymous_get, "mutating": {}, "reading": {}}
    for method, path in MUTATING_ROUTES:
        body = None
        headers = []
        if method == "POST":
            payload = synth_chart(nonce_name("auth", 0), "1.0.0")
            body, ctype = multipart("chart", "x.tgz", payload)
            headers = [("Content-Type", ctype)]
        st, hd, bd = server.request(method, path, body=body, headers=headers)
        out["mutating"][f"{method} {path}"] = _obs(f"{method} {path}", st, hd, bd)
    for method, path in READ_ROUTES:
        st, hd, bd = server.request(method, path)
        out["reading"][f"{method} {path}"] = _obs(f"{method} {path}", st, hd, bd)
    # Storage is read afterwards so a write that was supposed to be refused but
    # happened anyway is caught even if the response looked like a 401.
    out["storage"] = storage_files(server.storage)
    return out


#: The limit the upload-limit launch is configured with, and the sizes probed
#: around it. The over-limit sizes are jittered by the run nonce so the exact body
#: length is not a fixed number a submission could have a rule for.
UPLOAD_LIMIT = 4096
_JITTER = int(RUN_NONCE[:2], 16)


def upload_sizes() -> list[tuple[str, int, bool]]:
    """``(label, body size, should be accepted)`` around :data:`UPLOAD_LIMIT`."""
    #: Sizes are the *payload* target, not the request size: the multipart envelope
    #: adds about 180 bytes on top. Nothing is probed within a few hundred bytes of
    #: the limit, deliberately -- whether the limiter counts the body or the whole
    #: request is an implementation detail that a reasonable port could land either
    #: side of, and a benchmark should not turn that into a failure. What is probed
    #: is comfortably under and unambiguously over.
    return [
        ("minimal", 0, True),                            # the bare chart, ~700 B
        ("under", UPLOAD_LIMIT // 2, True),
        ("over-jittered", UPLOAD_LIMIT * 2 + _JITTER, False),
        ("over-4x", UPLOAD_LIMIT * 4 + _JITTER, False),
        ("over-16x", UPLOAD_LIMIT * 16 + _JITTER, False),
    ]


def battery_upload_limit(server) -> dict:
    """Uploads either side of ``--max-upload-size``.

    ``gin-contrib/size`` is a retired dependency, so the port has to re-implement
    the limit itself, and what it must reproduce is specific and easy to get wrong:
    over the limit the response is **413 with a bare ``text/plain`` body reading
    ``request too large``** -- the only non-JSON error this API produces. Every
    other failure is ``{"error":"..."}``, so a port that routes this through its own
    JSON error helper produces a plausible-looking 413 that is still wrong.

    The over-limit sizes are jittered by the run nonce, so "which bodies are too
    big" is not a question a fixed rule answers.
    """
    out: dict = {"limit": UPLOAD_LIMIT, "jitter": _JITTER, "cases": {}}
    # Numbered by position, not by ``hash(label)``: Python's string hash is salted
    # per process, so two labels could collide on the same ``% 90`` and the
    # "nothing was written" check -- which matches storage filenames by substring --
    # would then blame a refused upload for an accepted one's file. Measured: a
    # collision put ``probe-lim-<nonce>-64-1.0.0.tgz`` in storage under two labels
    # at once and failed State A.
    for i, (label, size, accepted) in enumerate(upload_sizes()):
        name = nonce_name("lim", i)
        payload = synth_chart(name, "1.0.0")
        # Padding only. A chart is a gzipped tarball and truncating one makes it
        # invalid, which would test the chart parser rather than the size limiter;
        # ``size`` is therefore a floor, and the "minimal" case (size 0) is the
        # unpadded chart.
        if size > len(payload):
            payload = payload + b"\0" * (size - len(payload))
        body, ctype = multipart("chart", f"{name}-1.0.0.tgz", payload)
        st, hd, bd = server.request("POST", "/api/charts", body=body,
                                    headers=[("Content-Type", ctype)])
        out["cases"][label] = _obs(label, st, hd, bd, chart=name,
                                   body_bytes=len(body),
                                   expected_accept=accepted)
    # A read is not a write: the limiter must not touch GET.
    st, hd, bd = server.request("GET", "/index.yaml")
    out["read"] = _obs("read", st, hd, bd)
    out["storage"] = storage_files(server.storage)
    return out


#: How many nonce charts the metrics battery pushes. The gauges ChartMuseum
#: exports count charts and versions in storage, so this number has to be known to
#: check the arithmetic.
N_METRICS_PUSH = 3


def battery_metrics(server) -> dict:
    """Two scrapes with known traffic in between.

    ``zsais/go-gin-prometheus`` is retired too, so the exposition is re-implemented
    by the port. Three properties are recorded here, none of which need an oracle:

    * the gauges are arithmetic on storage -- 2 seeded charts and 4 seeded versions
      plus what this battery pushes
    * the counters move by the traffic actually generated
    * no nonce chart name appears anywhere in the body. ChartMuseum maps request
      paths back to route templates before they become labels
      (``mapURLWithParamsBackToRouteTemplate``); a port that drops the mapping
      labels each series with the raw path, so every chart name ever requested
      becomes its own time series. Since the names here are generated at grading
      time, this catches it directly.
    """
    st, hd, bd = server.request("GET", "/metrics")
    out: dict = {"nonce": RUN_NONCE, "pushed": [],
                 "before": _obs("before", st, hd, bd,
                                text_full=bd.decode("utf-8", "replace"))}
    requests_made = 0
    for i in range(N_METRICS_PUSH):
        name = nonce_name("met", i)
        payload = synth_chart(name, "7.7.7")
        body, ctype = multipart("chart", f"{name}-7.7.7.tgz", payload)
        st, hd, bd = server.request("POST", "/api/charts", body=body,
                                    headers=[("Content-Type", ctype)])
        out["pushed"].append({"name": name, "version": "7.7.7",
                              "status": st})
        requests_made += 1
        # Reads that exercise every parameterised route shape, so the url label
        # mapping is given something to collapse.
        for path in (f"/api/charts/{name}", f"/api/charts/{name}/7.7.7",
                     f"/charts/{name}-7.7.7.tgz", f"/api/charts/{name}/9.9.9"):
            server.request("GET", path)
            requests_made += 1
    for _ in range(4):
        server.request("GET", "/index.yaml")
        requests_made += 1
    out["requests_made"] = requests_made
    st, hd, bd = server.request("GET", "/metrics")
    out["after"] = _obs("after", st, hd, bd,
                        text_full=bd.decode("utf-8", "replace"))
    out["storage"] = storage_files(server.storage)
    return out


def battery_index_self_consistency(server, *, prefix: str = "") -> dict:
    """Follow the index's own promises: every advertised URL and digest.

    The index states, for each chart version, where to download it and what its
    sha256 is. Both are then checked against the bytes the server actually serves.
    No oracle is involved -- the response is being held to its own claims, which is
    a property no canned table survives, because the table would have to agree with
    itself across two different endpoints.
    """
    st, hd, bd = server.request("GET", prefix + "/index.yaml")
    out: dict = {"prefix": prefix,
                 "index": _obs("index", st, hd, bd,
                               text_full=bd.decode("utf-8", "replace")),
                 "downloads": {}}
    from harness import normalize
    doc = normalize.parse_index(bd)
    entries = (doc or {}).get("entries", {}) if isinstance(doc, dict) else {}
    for name, versions in sorted(entries.items()):
        for v in versions or []:
            if not isinstance(v, dict):
                continue
            version = v.get("version")
            digest = v.get("digest")
            urls = v.get("urls") or []
            if not urls:
                continue
            # ChartMuseum advertises a RELATIVE url by default -- literally
            # ``charts/mychart-0.1.0.tgz`` -- so it has to be resolved against the
            # index's own location, per RFC 3986, exactly as Helm does. Resolving
            # it against the root instead drops the context path and turns every
            # download into a 404: measured on State A under
            # ``--context-path=/cm``, where all four charts came back 404 with a
            # naive resolution and 200 with this one. ``--chart-url`` makes these
            # absolute, which urljoin also handles.
            base = f"http://127.0.0.1{prefix}/index.yaml"
            resolved = urllib.parse.urljoin(base, urls[0])
            split = urllib.parse.urlsplit(resolved)
            path = split.path or "/"
            if split.query:
                path = f"{path}?{split.query}"
            st, hd, bd2 = server.request("GET", path)
            out["downloads"][f"{name}/{version}"] = _obs(
                f"{name}/{version}", st, hd, bd2,
                advertised_digest=digest, advertised_url=urls[0],
                requested_path=path)
    return out


def battery_tenant_isolation(server) -> dict:
    """Under ``--depth``, a chart pushed to one tenant must not appear in another.

    Multi-tenancy is the most intricate thing the hand-written ``match()`` does:
    the same URL shape means different things at depth 0, 1 and 2, and the tenant
    prefix has to be peeled off the path before the chart name is read. A port that
    got this subtly wrong still serves charts -- it serves them to the wrong
    tenant, which is a data-disclosure bug rather than a formatting one.

    Nonce names again, so this cannot be answered from a table.
    """
    out: dict = {"pushes": {}, "views": {}}
    tenants = ["org1/repo1", "org2/repo2"]
    for ti, tenant in enumerate(tenants):
        name = nonce_name("tenant", ti)
        version = "1.0.0"
        payload = synth_chart(name, version)
        body, ctype = multipart("chart", f"{name}-{version}.tgz", payload)
        st, hd, bd = server.request("POST", f"/api/{tenant}/charts", body=body,
                                    headers=[("Content-Type", ctype)])
        out["pushes"][tenant] = _obs(f"push {tenant}", st, hd, bd,
                                     chart=name, version=version,
                                     uploaded_sha256=sha256(payload))
    # Cross product: each tenant's index and per-chart view, asked about each
    # tenant's chart. Only the diagonal may be found.
    for tenant in tenants:
        for ti, other in enumerate(tenants):
            name = nonce_name("tenant", ti)
            st, hd, bd = server.request("GET", f"/api/{tenant}/charts/{name}")
            out["views"][f"{tenant} sees {name}"] = _obs(
                f"{tenant}/{name}", st, hd, bd, tenant=tenant, chart=name,
                owner=other, is_own=(tenant == other))
    for tenant in tenants:
        st, hd, bd = server.request("GET", f"/{tenant}/index.yaml")
        out[f"index {tenant}"] = _obs(f"index {tenant}", st, hd, bd,
                                      text_full=bd.decode("utf-8", "replace"))
    return out


#: The launches the audit suite makes, and what runs on each. Flag sets are
#: taken from configurations the corpus already proves State A supports, so a
#: failure here is never "this flag combination was never valid".
#:
#: Seven launches for several thousand assertions. Each entry is
#: (id, flags, seeds, batteries).
_SEEDS_FLAT = ("=testdata/charts/mychart/mychart-0.0.1.tgz,"
               "testdata/charts/mychart/mychart-0.1.0.tgz,"
               "testdata/charts/mychart/mychart-0.2.0.tgz,"
               "testdata/charts/otherchart/otherchart-0.1.0.tgz")
_SEEDS_DEPTH2 = ("org1/repo1=testdata/charts/mychart/mychart-0.1.0.tgz",
                 "org2/repo2=testdata/charts/otherchart/otherchart-0.1.0.tgz")

#: Read-only batteries share the ``plain`` launch; every battery that writes gets
#: its own, so no battery can observe another's side effects. ``index_self`` in
#: particular must see the seeded state exactly as seeded.
PROBE_LAUNCHES = [
    ("plain", (), (_SEEDS_FLAT,),
     ("determinism", "invariance", "route_grid", "index_self")),
    ("ghost-writes", (), (_SEEDS_FLAT,), ("write_discipline",)),
    ("write", (), (_SEEDS_FLAT,), ("lifecycle",)),
    ("overwrite", ("--allow-overwrite",), (_SEEDS_FLAT,), ("lifecycle",)),
    ("auth-closed", ("--basic-auth-user=myuser", "--basic-auth-pass=mypass"),
     (_SEEDS_FLAT,), ("auth",)),
    ("auth-anon-get", ("--basic-auth-user=myuser", "--basic-auth-pass=mypass",
                       "--auth-anonymous-get"), (_SEEDS_FLAT,), ("auth",)),
    ("depth2", ("--depth=2",), _SEEDS_DEPTH2, ("tenant",)),
    ("contextpath", ("--context-path=/cm",), (_SEEDS_FLAT,),
     ("determinism_cm", "route_grid_cm", "index_self")),
    # Each of the two retired Gin-ecosystem middlewares gets its own launch,
    # because both need a flag the others must not have: an upload limit small
    # enough to trip would break every other battery's pushes, and --enable-metrics
    # adds a route and a middleware to every request path.
    ("upload-limit", (f"--max-upload-size={UPLOAD_LIMIT}",), (_SEEDS_FLAT,),
     ("upload_limit",)),
    ("metrics", ("--enable-metrics",), (_SEEDS_FLAT,), ("metrics",)),
]


def run_launch(binary: str, spec, *, rundir: str) -> dict:
    """Perform one probe launch and every battery assigned to it."""
    from harness.launch import serve

    pid, flags, seeds, batteries = spec
    os.makedirs(rundir, exist_ok=True)
    out: dict = {"id": pid, "flags": list(flags), "batteries": {}}
    server = serve(binary, flags=flags, seeds=tuple(seeds), rundir=rundir)
    try:
        for b in batteries:
            if b == "lifecycle":
                out["batteries"][b] = battery_lifecycle(server)
            elif b == "determinism":
                out["batteries"][b] = battery_determinism(server)
            elif b == "determinism_cm":
                out["batteries"]["determinism"] = battery_determinism(
                    server, prefix="/cm")
            elif b == "invariance":
                out["batteries"][b] = battery_invariance(server)
            elif b == "route_grid":
                out["batteries"][b] = battery_route_grid(server)
            elif b == "route_grid_cm":
                out["batteries"]["route_grid"] = battery_route_grid(
                    server, prefix="/cm")
            elif b == "write_discipline":
                out["batteries"][b] = battery_write_discipline(server)
            elif b == "upload_limit":
                out["batteries"][b] = battery_upload_limit(server)
            elif b == "metrics":
                out["batteries"][b] = battery_metrics(server)
            elif b == "index_self":
                # The context-path launch reaches its index through the prefix;
                # the download URLs it advertises are then followed exactly as
                # advertised, which is the property under test.
                prefix = "/cm" if "--context-path=/cm" in flags else ""
                out["batteries"][b] = battery_index_self_consistency(
                    server, prefix=prefix)
            elif b == "auth":
                out["batteries"][b] = battery_auth(
                    server, anonymous_get="--auth-anonymous-get" in flags)
            elif b == "tenant":
                out["batteries"][b] = battery_tenant_isolation(server)
            else:  # pragma: no cover - guarded by the table above
                raise ValueError(f"unknown battery {b!r}")
    finally:
        server.stop()
        out["log"] = server.log[-8000:]
    return out


def run_all(binary: str, workdir: str) -> dict:
    """Every probe launch.  A launch that fails costs only its own batteries."""
    from harness.launch import LaunchError

    out: dict = {"nonce": RUN_NONCE, "launches": {}}
    for spec in PROBE_LAUNCHES:
        pid = spec[0]
        rundir = os.path.join(workdir, "probe", pid)
        try:
            out["launches"][pid] = run_launch(binary, spec, rundir=rundir)
        except LaunchError as exc:
            out["launches"][pid] = {"id": pid, "__failed__": str(exc),
                                    "batteries": {}}
        except OSError as exc:
            out["launches"][pid] = {
                "id": pid, "__failed__": f"{type(exc).__name__}: {exc}",
                "batteries": {}}
    return out
