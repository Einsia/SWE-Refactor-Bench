"""Turning a live response into something two runs can be compared on.

Four classes of variation have to be removed before State A and State B can be
compared, and each is removed for a stated reason.

1.  **Per-process facts.** ``Date`` moves and ``Connection`` is a transport
    detail. Both are dropped, never compared. ``Content-Length`` is *kept*:
    ChartMuseum sets it explicitly on some responses and lets Go compute it on
    others, and which is which is an observable a port can get wrong. Its value
    is masked only on the bodies that carry a live instant, whose byte length is
    not a contract in the first place.

2.  **Bound address and filesystem paths.** A 500 body can carry a storage
    error naming the temporary directory the server was launched with, and that
    directory differs between the oracle run and the graded run by
    construction.

3.  **``index.yaml``'s ``generated`` field, and a pushed chart's ``created``.**
    ``generated`` is ``time.Now()`` at regeneration. ``created`` comes from the
    storage object's mtime, which for a *seeded* chart is the frozen fixture
    epoch and therefore a constant the tests compare exactly -- but for a chart
    pushed during a profile it is the moment of the push, and moves. So
    ``created`` is masked only when it is not the epoch. ``digest`` is a hash of
    frozen chart bytes, ``urls`` is derived from configuration, and entry order
    is the sort the source performs; all three are contract.

    Masking, here and for ``X-Request-Id``, is preferred to dropping wherever the
    field has a *shape*: a dropped field cannot catch a port that stopped
    emitting it, while a masked one still requires it to be present and
    well-formed. A value that fails the shape check keeps its literal text and
    fails the comparison.

4.  **Prometheus exposition.** Sample values move with the run (durations,
    counters), but the metric *names*, ``# HELP``/``# TYPE`` lines and label
    sets do not. ``metrics`` bodies are compared on that structure.

Nothing is masked on the strength of an argument alone: ``capture.py`` replays
the whole corpus against two independently launched oracles and records every
field that disagreed, and only that measured set is treated as volatile. The
reasoning above says what we *expect* to be volatile; the capture decides.
"""

from __future__ import annotations

import json
import re

#: Dropped from every comparison: no contract, or measured elsewhere.
IGNORED_HEADERS = frozenset({
    "date",
    "connection",
    "keep-alive",
    "transfer-encoding",
})

#: Values that are a set of case-insensitive tokens rather than a sequence.
SET_VALUED_HEADERS = frozenset({
    "vary",
    "access-control-allow-methods",
    "access-control-allow-headers",
    "access-control-expose-headers",
    "allow",
    "cache-control",
})

PLACEHOLDER_HOST = "http://HOST:PORT"

#: Headers whose *value* is per-request but whose presence and shape are not.
#: Masked to a placeholder rather than dropped: ChartMuseum stamps
#: ``X-Request-Id`` on every response, and a port that stopped emitting it, or
#: emitted something that is not a UUID, would be invisible if the header were
#: simply ignored. The capture measured this as the single most common volatile
#: field -- 728 of 935, one per case -- which is exactly the signature of a field
#: that should be masked instead.
_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
SHAPED_HEADERS = {"x-request-id": (_UUID, "<<uuid>>")}

_ADDR = re.compile(
    r"https?://(?:127\.0\.0\.1|localhost|\[::1\]|0\.0\.0\.0)(?::\d+)?")

#: The same authority *without* a scheme. Needed because the Prometheus exporter
#: puts the request's Host into a label -- ``host="127.0.0.1:46225"`` -- and the
#: port is chosen per launch. Without this the whole ``/metrics`` body would
#: differ between the oracle and every submission, for a reason that has nothing
#: to do with either. Found by capture: it was the only content difference left in
#: the metrics profile once the timestamps were masked.
_BARE_ADDR = re.compile(
    r"(?<![\w.:-])(?:127\.0\.0\.1|localhost|\[::1\]|0\.0\.0\.0):\d{1,5}\b")
_TMPPATH = re.compile(r"/tmp/[A-Za-z0-9._/-]+")
_WORKSPACE = re.compile(r"/workspace/[A-Za-z0-9._/-]+")
PLACEHOLDER_AUTHORITY = "HOST:PORT"


def scrub_text(text: str, prefixes: tuple[str, ...] = ()) -> str:
    """Remove bound-address and filesystem details from a textual value.

    ``prefixes`` are absolute directories belonging to whichever side is being
    measured: its checkout and the scratch directory its storage lives in. A
    storage-layer error message embeds the object path, so without this the
    comparison would be between the oracle's temp directory and the
    submission's. Longest first, because one prefix can contain another.
    """
    for prefix in sorted(prefixes, key=len, reverse=True):
        if prefix:
            text = text.replace(prefix, "/PATH")
    text = _ADDR.sub(PLACEHOLDER_HOST, text)
    text = _BARE_ADDR.sub(PLACEHOLDER_AUTHORITY, text)
    text = _TMPPATH.sub("/tmp/PATH", text)
    text = _WORKSPACE.sub("/PATH", text)
    return text


def scrub_header_value(name: str, value: str,
                       prefixes: tuple[str, ...] = ()) -> str:
    value = scrub_text(value, prefixes)
    lower = name.lower()
    shaped = SHAPED_HEADERS.get(lower)
    if shaped is not None:
        pattern, placeholder = shaped
        # A value that does NOT match keeps its literal text, so the comparison
        # still fails. Masking is about accepting any *well-shaped* value, not
        # about accepting anything.
        return placeholder if pattern.match(value.strip()) else value
    if lower in SET_VALUED_HEADERS:
        parts = [p.strip().lower() for p in value.split(",") if p.strip()]
        return ", ".join(sorted(parts))
    return value


def header_map(raw_headers: list[tuple[str, str]],
               prefixes: tuple[str, ...] = ()) -> dict[str, list[str]]:
    """Case-insensitive name -> list of scrubbed values, ignored names removed.

    A list, not a string: a repeated header is real and collapsing one would
    hide a difference.
    """
    out: dict[str, list[str]] = {}
    for name, value in raw_headers:
        lower = name.lower()
        if lower in IGNORED_HEADERS:
            continue
        out.setdefault(lower, []).append(
            scrub_header_value(lower, value, prefixes))
    for values in out.values():
        values.sort()
    return out


def scrub_body(body: bytes, prefixes: tuple[str, ...] = ()) -> str:
    """Decode and scrub a body, keeping byte-level fidelity for text.

    Chart archives and provenance files are binary or signature-bearing; those
    cases are compared by length and digest instead, so a placeholder is enough
    here and keeps megabytes of gzip out of the golden file.
    """
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return "<<binary:%d bytes>>" % len(body)
    return scrub_text(text, prefixes)


# ---------------------------------------------------------------------------
# index.yaml
# ---------------------------------------------------------------------------

#: The one field of an index that moves between two runs of State A itself:
#: ``time.Now()`` at regeneration. Masked in the text form and dropped from the
#: parsed form. Everything else in an index is derived from frozen inputs.
#:
#: Shape-gated like ``created``: only a *timestamp-valued* ``generated`` is
#: masked. The mask is applied to every textual body, not only to bodies already
#: known to be indexes, and the shape gate is what makes that safe -- a
#: ``generated:`` line in a web template holding anything other than an RFC3339
#: instant keeps its text.
#:
#: The mtime every published fixture is ``touch``ed to. ChartMuseum reads
#: ``created`` off the storage object, so for a seeded chart this value is a
#: constant the tests can compare directly -- which is the whole reason the
#: fixtures are touched at all.
FIXTURE_EPOCH = "2022-07-01T00:00:00Z"

#: ``created`` for a chart *pushed during a profile* is the moment of the push,
#: so it moves between runs by definition. It is masked to its shape rather than
#: dropped: a port that omitted ``created``, or emitted something that is not a
#: timestamp, would otherwise go unnoticed. Anything already equal to
#: FIXTURE_EPOCH is left alone, so the seeded entries stay exact.
_RFC3339 = (r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}"
            r"(?:\.\d+)?(?:[Zz]|[+-]\d{2}:?\d{2})")
_CREATED_LINE = re.compile(
    # ``- created:`` as well as ``created:``: in an index the field sits on a
    # sequence item, so a leading-whitespace-only prefix never matches.
    r"^(?P<pre>[ \t]*(?:-[ \t]+)?created:[ \t]*)"
    r"(?P<q>[\"']?)(?P<ts>" + _RFC3339 + r")(?P=q)[ \t]*$",
    re.M)
_CREATED_JSON = re.compile(
    r'("created"\s*:\s*")(?P<ts>' + _RFC3339 + r')(")')
_RFC3339_FULL = re.compile(r"^" + _RFC3339 + r"$")
_GENERATED_LINE = re.compile(
    r"^(?P<pre>[ \t]*(?:-[ \t]+)?generated:[ \t]*)"
    r"(?P<q>[\"']?)(?P<ts>" + _RFC3339 + r")(?P=q)[ \t]*$",
    re.M)


def _sub_ts_line(pattern: re.Pattern, text: str, mask) -> str:
    """Rewrite a ``key: <timestamp>`` line's value, keeping quoting intact."""
    def _one(m: "re.Match") -> str:
        masked = mask(m.group("ts"))
        if masked == m.group("ts"):
            return m.group(0)
        return f"{m.group('pre')}{m.group('q')}{masked}{m.group('q')}"

    return pattern.sub(_one, text)


def mask_created(value):
    """Mask one ``created`` value unless it is the frozen fixture epoch."""
    if not isinstance(value, str):
        return value
    if value == FIXTURE_EPOCH:
        return value
    return "<<created>>" if _RFC3339_FULL.match(value) else value


def mask_created_text(text: str) -> str:
    """Mask live ``created`` timestamps in a YAML or JSON body.

    Applied to every textual body, not just indexes: ``/api/charts`` answers with
    the same metadata in JSON, and a push response echoes it. The substitution is
    a no-op on any body that has no ``created`` holding a non-epoch RFC3339
    value, which is every body that was not affected by a live push.
    """
    text = _sub_ts_line(_CREATED_LINE, text, mask_created)

    def _js(m: "re.Match") -> str:
        return m.group(1) + mask_created(m.group("ts")) + m.group(3)

    return _CREATED_JSON.sub(_js, text)


def mask_timestamps(text: str) -> str:
    """Mask both moving instants an index body carries.

    Applied to every textual body. ``generated`` moves on every regeneration and
    ``created`` moves for anything pushed during the profile, so a body that was
    not fully masked here would be volatile for reasons that say nothing about the
    port -- and a volatile body means the body is not compared at all, which is a
    far bigger loss than the two instants.
    """
    return _sub_ts_line(_GENERATED_LINE, mask_created_text(text),
                        lambda ts: "<<generated>>")


#: Named for its caller: the ``--gen-index`` stdout path in :mod:`harness.clirun`
#: masks an index that never was an HTTP body.
mask_index_text = mask_timestamps


def mask_created_deep(obj):
    """Mask every ``created`` value inside a parsed structure, in place-ish."""
    if isinstance(obj, dict):
        return {k: (mask_created(v) if k == "created" else mask_created_deep(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [mask_created_deep(v) for v in obj]
    return obj


def parse_index(text: str):
    """Parse an index.yaml into a comparable structure, or None.

    Returned alongside the masked text rather than instead of it. The text
    catches a serialisation change -- indentation, key order, quoting style --
    which is a real observable, because a client that byte-compares a cached
    index would see it. The structure catches a *semantic* change and says which
    entry and field differ, which the text diff cannot. A test that only had the
    text would be brittle about formatting; one that only had the structure
    would be blind to it.
    """
    import yaml

    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    if not isinstance(doc, dict):
        return None
    doc.pop("generated", None)
    return mask_created_deep(doc)


def index_entry_order(doc) -> dict[str, list[str]]:
    """chart name -> the versions in the order the index lists them.

    Order is not incidental: ``index.go`` sorts entries, so a port that rebuilds
    the index with a map iteration produces a valid document in a random order
    and every ``helm search`` result changes.
    """
    if not isinstance(doc, dict):
        return {}
    entries = doc.get("entries")
    if not isinstance(entries, dict):
        return {}
    out = {}
    for name, versions in entries.items():
        if isinstance(versions, list):
            out[name] = [v.get("version") if isinstance(v, dict) else None
                         for v in versions]
    return out


def index_digests(doc) -> dict[str, str]:
    """"name-version" -> digest, for every entry.

    The digest is a sha256 of the chart bytes, and the charts are frozen at
    image build time, so every digest here is a constant. A mismatch means the
    submission served or indexed different bytes -- which is the failure mode
    that breaks ``helm install`` while leaving every status code correct.
    """
    out: dict[str, str] = {}
    if not isinstance(doc, dict):
        return out
    for name, versions in (doc.get("entries") or {}).items():
        if not isinstance(versions, list):
            continue
        for v in versions:
            if isinstance(v, dict):
                out[f"{name}-{v.get('version')}"] = v.get("digest")
    return out


# ---------------------------------------------------------------------------
# Prometheus exposition
# ---------------------------------------------------------------------------

_METRIC_LINE = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)"
    r"(?:\{(?P<labels>[^}]*)\})?"
    r"\s+(?P<value>\S+)(?:\s+(?P<ts>\S+))?$")


def parse_metrics(text: str) -> dict:
    """Structure of a /metrics body: names, help, type, and label sets.

    Sample *values* are deliberately excluded from the structure. A request
    duration histogram and a process-uptime gauge cannot agree between two runs
    of the same binary, so comparing values would fail State A against itself.
    What must agree is which series exist and how they are labelled -- that is
    what a dashboard and an alert rule are written against, and it is exactly
    what a port of the metrics middleware breaks when it registers a collector
    with a different name or drops a label.

    ``chartmuseum_chart_total``-style gauges are the exception worth naming:
    their values are counts of frozen charts, so they are comparable. They are
    surfaced separately in ``gauge_values`` rather than smuggled into the
    structure.
    """
    help_: dict[str, str] = {}
    type_: dict[str, str] = {}
    series: dict[str, list[str]] = {}
    values: dict[str, str] = {}
    malformed: list[str] = []

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("# HELP "):
            parts = line[len("# HELP "):].split(" ", 1)
            help_[parts[0]] = parts[1] if len(parts) > 1 else ""
            continue
        if line.startswith("# TYPE "):
            parts = line[len("# TYPE "):].split(" ", 1)
            type_[parts[0]] = parts[1] if len(parts) > 1 else ""
            continue
        if line.startswith("#"):
            continue
        m = _METRIC_LINE.match(line)
        if not m:
            malformed.append(line[:120])
            continue
        name = m.group("name")
        labels = m.group("labels") or ""
        # Label order is not significant in the exposition format, so a series
        # is identified by its sorted label set.
        parts = sorted(p.strip() for p in labels.split(",") if p.strip())
        key = "{" + ",".join(parts) + "}"
        series.setdefault(name, []).append(key)
        values[name + key] = m.group("value")

    for keys in series.values():
        keys.sort()
    return {
        "help": help_,
        "type": type_,
        "series": series,
        "names": sorted(series),
        "malformed": malformed,
        "values": values,
    }


def mask_metrics_text(text: str) -> str:
    """Replace every sample value in an exposition body with a placeholder.

    Without this the ``/metrics`` body is volatile as a whole -- a request
    duration sum moves between two runs of State A -- and a volatile body is not
    compared at all, so the *format* of the exposition would go ungraded. That
    format is not incidental here: ``zsais/go-gin-prometheus`` is on the retired
    list, so the port has to re-emit these series itself, and line order, label
    rendering and the HELP/TYPE block are the parts a dashboard depends on.

    Values are not lost, only moved: the ones that can be compared are surfaced
    by :func:`stable_gauge_values`.
    """
    out = []
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            out.append(line)
            continue
        m = _METRIC_LINE.match(stripped)
        if not m:
            out.append(line)
            continue
        head = stripped[:m.start("value")].rstrip()
        tail = line[len(line.rstrip("\r\n")):]
        out.append(f"{head} <<value>>{tail}")
    return "".join(out)


#: Metric names whose values are counts of frozen inputs, and therefore are
#: comparable between runs. Discovered by capture, not asserted here: this is the
#: candidate list, and any name on it that turns out to disagree between two
#: oracle runs is dropped from the comparison by the usual volatility pass.
#:
#: These two are counts of what is in storage, and the ``metrics`` profile is
#: read-only over a fixed seed, so they are deterministic.
#:
#: The names were wrong until they were measured: this read
#: ``("chart_total", "chart_version_total")``, which is not a substring of either
#: name on the wire, so no sample was ever selected and every recorded
#: ``metrics_values`` was ``{}`` -- leaving ``test_metrics_stable_values``
#: comparing ``{} == {}``. The golden file in this image predates the fix, so that
#: test skips rather than compares; see its docstring. The two gauges are graded
#: regardless, and harder, by ``test_chart_gauges_count_what_is_in_storage`` in the
#: audit suite, which computes the expected reading from pushes made during
#: the run instead of replaying a recorded number.
STABLE_GAUGE_HINTS = ("charts_served_total", "chart_versions_served_total")


def stable_gauge_values(parsed: dict) -> dict[str, str]:
    out = {}
    for key, value in parsed.get("values", {}).items():
        if any(hint in key for hint in STABLE_GAUGE_HINTS):
            out[key] = value
    return out


# ---------------------------------------------------------------------------
# One response, frozen
# ---------------------------------------------------------------------------

def content_length_consistent(raw_headers: list[tuple[str, str]], body: bytes,
                              *, method: str = "GET") -> bool | None:
    """Did ``Content-Length`` describe the bytes that actually arrived?

    A separate observation from the header's *value*, and it exists because the
    value is sometimes masked.  Two of the recorded bodies embed a temporary
    directory name whose length moves between runs, so the header is frozen as
    ``<<len>>`` -- which means a port that sent ``Content-Length: 0`` alongside a
    real body would satisfy the header comparison.  This boolean is what still
    catches that, and it is stable by construction: it compares the declaration
    against the delivery within a single response, so nothing outside that
    response can move it.

    ``None`` rather than ``False`` in the two cases where the question does not
    apply: no header was sent (chunked, or a bodiless 304), or the request was a
    HEAD, where the header describes the body a GET *would* have returned and the
    empty delivery is required rather than wrong.
    """
    declared = [v for k, v in raw_headers if k.lower() == "content-length"]
    if not declared or method.upper() == "HEAD":
        return None
    try:
        return int(declared[0].strip()) == len(body)
    except ValueError:
        return False


def record(case_id: str, status: int, raw_headers: list[tuple[str, str]],
           body: bytes, *, body_mode: str = "exact", method: str = "GET",
           prefixes: tuple[str, ...] = ()) -> dict:
    """Freeze one response into the comparable form stored in the golden file.

    Every derived form is computed for every response regardless of
    ``body_mode``: the mode decides what the *tests* compare, and computing the
    rest anyway costs nothing and means a mode can be tightened later without
    re-capturing. The exception is the body text itself for binary responses,
    where the bytes are replaced by a digest and a length -- a chart archive is
    ~4 KiB and there are hundreds of them, and its identity is fully captured by
    its sha256.
    """
    import hashlib

    headers = header_map(raw_headers, prefixes)
    raw_text = scrub_body(body, prefixes)
    is_binary = raw_text.startswith("<<binary:")
    # Whether *scrubbing* -- not timestamp masking -- changed the body. This has to
    # be measured here, before the comparison below, because ``raw_text`` is already
    # the scrubbed text and so ``text != raw_text`` can only ever see the masking
    # step.
    #
    # It matters for exactly one thing, and it was a live bug: a DELETE of something
    # absent answers ``{"error":"remove /tmp/<rundir>/...: no such file or
    # directory"}``. The body is scrubbed to ``/tmp/PATH`` and compares fine, but
    # ``Content-Length`` describes the unscrubbed bytes -- so it encoded the length
    # of a temporary directory name. Capture and grading use different rundirs, and
    # 14 cases failed on a four-character difference that had nothing to do with the
    # submission.
    scrubbed = not is_binary and raw_text != body.decode("utf-8", "replace")
    # Mask the moving instants in the body itself, not only in a derived field:
    # an unmasked body is a volatile body, and a volatile body is not compared at
    # all. Measured -- before this, all 63 index responses had a volatile body.
    text = raw_text if is_binary else mask_timestamps(raw_text)
    if not is_binary and body_mode == "metrics":
        text = mask_metrics_text(text)

    # Every body-derived field is computed over the COMPARABLE form -- the masked
    # text for a text body, the bytes themselves for a binary one -- and never over
    # the raw bytes of a body that was masked.
    #
    # This is not tidiness. ``generated`` has second precision, so two oracle runs
    # that happen inside the same second emit byte-identical indexes, a raw digest
    # then *agrees* and is written into the expectations, and a submission graded
    # in a different second fails it. Two runs agreeing by coincidence is the one
    # failure mode the two-run intersection cannot see, and it was measured: the
    # count of volatile ``body_sha256`` fields moved from 49 to 57 between two
    # captures of an identical corpus, purely on timing luck.
    comparable = body if is_binary else text.encode("utf-8")
    masked = not is_binary and (text != raw_text or scrubbed)

    entry = {
        "id": case_id,
        "status": status,
        "headers": headers,
        "header_names": sorted(headers),
        "body": text if not is_binary else "",
        "body_len": len(comparable),
        "body_sha256": hashlib.sha256(comparable).hexdigest(),
        "body_binary": is_binary,
        "body_masked": masked,
        "body_mode": body_mode,
        "content_length_consistent": content_length_consistent(
            raw_headers, body, method=method),
    }
    if masked:
        # The wire length of a body holding a live instant is not a contract
        # either: Go renders RFC3339 with trailing zeros elided, so the same
        # response is 1199 bytes one second and 1200 the next. Masked to a
        # placeholder rather than dropped, so that a port which stopped sending
        # Content-Length at all is still caught.
        #
        # The same reasoning covers a scrubbed path, which is why ``masked``
        # includes it: neither an instant nor a temporary directory name is part of
        # the interface, and both were being graded through this header. What is
        # still graded is that Content-Length is *present*, and -- through
        # ``content_length_consistent`` below -- that it described the bytes that
        # were actually sent.
        if "content-length" in headers:
            headers["content-length"] = ["<<len>>"]

    # No separate ``index_text``: ``body`` is already the fully masked text, so a
    # second copy of the same string would double the golden file's size for every
    # index response and give a test two names for one observation.
    if not is_binary:
        try:
            parsed = json.loads(text)
        except ValueError:
            entry["json"] = None
        else:
            entry["json"] = parsed
        if body_mode == "index":
            doc = parse_index(text)
            entry["index"] = doc
            entry["index_order"] = index_entry_order(doc)
            entry["index_digests"] = index_digests(doc)
        elif body_mode == "metrics":
            m = parse_metrics(text)
            entry["metrics"] = {k: m[k] for k in
                                ("help", "type", "series", "names", "malformed")}
            entry["metrics_values"] = stable_gauge_values(m)
    else:
        entry["json"] = None

    return entry
