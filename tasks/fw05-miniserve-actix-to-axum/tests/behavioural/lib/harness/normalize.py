"""Turning a live response into something two runs can be compared on.

Four classes of variation have to be removed before two boots of miniserve --
never mind two implementations of it -- can be compared at all. Each is removed
for a stated reason, and nothing is removed because it merely looked risky.

1.  **Per-process facts.** ``Date`` moves. ``Connection`` and
    ``Transfer-Encoding`` are transport details. ``Content-Length`` is a
    function of a body that is already compared byte for byte. Dropped, never
    compared.

2.  **The bound address.** The listing embeds the absolute URI in the QR
    spoiler and in the wget footer, and a redirect puts it in ``Location``. The
    port is assigned per boot, so both are rewritten to a fixed placeholder. The
    *structure* survives the rewrite, which is what the task grades.

3.  **The per-boot nonce routes.** ``MiniserveConfig`` generates the favicon and
    stylesheet routes with ``nanoid!(10, hex)`` on every start, so every HTML
    page carries two ten-hex-digit paths that differ between two runs of State A
    itself. They are placeholdered by *position* -- the ``<link rel="icon">``
    and ``<link rel="stylesheet">`` hrefs are read off the page and substituted
    everywhere they occur -- so a page that stops generating them, or that emits
    a fixed one, still fails. ``--random-route`` extends the same treatment to
    the route prefix.

    Note what is *not* on this list: the QR code. Its SVG path is an encoding of
    the absolute URL, so it would be per-boot volatile if the port were -- but
    the port is pinned per session precisely so it is not, and the QR is
    compared byte for byte like everything else. That is the only practical way
    to grade "the QR encodes the right URL" without shipping a QR decoder.

4.  **The humanised mtime column.** The listing prints each entry's mtime twice:
    once as ``%Y-%m-%d %H:%M:%S %:z``, which the pinned sample-tree mtimes make
    exact, and once through ``chrono_humanize`` as "4 years ago", which drifts
    with the wall clock. Only the second is dropped, and only inside the
    ``<span class="history">`` it lives in, so the exact timestamp beside it
    stays a hard contract.

5.  **The inode inside an ETag.** actix-files builds its ETag from four facts:
    ``"{ino:x}:{size:x}:{mtime_secs:x}:{mtime_nanos:x}"``. Three of those are
    reproducible -- the sample tree pins size and mtime -- but the inode is
    assigned by the filesystem when the tree is written and differs between two
    materialisations of the *same* tree. Only that first field is replaced, with
    ``INO``. The remaining three, the quoting, the field count and the
    separators all stay part of the contract, so a port that invents its own
    ETag scheme, or that drops ETags entirely (which is what a naive
    ``tower-http`` ``ServeDir`` does), still fails on every file response.

The version footer is deliberately *not* normalised: it names miniserve 0.27.1,
the port has to keep saying so, and a footer that changed would be a real
difference a user could see.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import re
import tarfile
import zipfile
import zlib

#: Dropped from every comparison: transport and timing facts with no contract.
IGNORED_HEADERS = frozenset({
    "date",
    "connection",
    "keep-alive",
    "transfer-encoding",
    "content-length",
    "alt-svc",
})

#: Headers whose value is a set of case-insensitive tokens rather than a
#: sequence. HTTP defines the tokens in each of these case-insensitively and
#: order-independently, so both sides are lowercased and sorted first:
#: ``Accept-Encoding, Range`` and ``range, accept-encoding`` ask a cache for the
#: same behaviour.
SET_VALUED_HEADERS = frozenset({
    "vary",
    "allow",
    "cache-control",
    "accept-ranges",
    "access-control-allow-methods",
    "access-control-allow-headers",
    "access-control-expose-headers",
})

PLACEHOLDER_HOST = "http://HOST:PORT"
PLACEHOLDER_HOST_TLS = "https://HOST:PORT"

_ADDR = re.compile(
    r"(?:https?://)?(?:127\.0\.0\.1|localhost|\[::1\]|0\.0\.0\.0)(?::\d+)?")
_ADDR_SCHEMED = re.compile(
    r"https?://(?:127\.0\.0\.1|localhost|\[::1\]|0\.0\.0\.0)(?::\d+)?")
_TMPPATH = re.compile(r"/tmp/[A-Za-z0-9._-]+")
_WORKSPACE = re.compile(r"/workspace/[A-Za-z0-9._/-]+")
_OPT = re.compile(r"/opt/[A-Za-z0-9._/-]+")

#: actix-files' ETag: inode, size, mtime seconds, mtime nanoseconds, all hex,
#: inside a quoted string. Only the inode is substituted; see class 5 above.
_ETAG = re.compile(r'(W/)?"([0-9a-f]+):([0-9a-f]+:[0-9a-f]+:[0-9a-f]+)"')

#: The two nonce routes, as they appear in the page header. Read by position
#: rather than matched by shape, so a page that emits a *different* ten-hex
#: route in the same slot is still normalised, and a page that emits none is
#: still caught -- the substitution simply does not fire and the diff stands.
_ICON_HREF = re.compile(
    r'<link\s+rel="icon"[^>]*\bhref="([^"]*)"', re.I)
_CSS_HREF = re.compile(
    r'<link\s+rel="stylesheet"[^>]*\bhref="([^"]*)"', re.I)

#: The humanised relative time, inside the span the renderer puts it in.
_HISTORY_SPAN = re.compile(
    r'(<span\s+class="history">)(.*?)(</span>)', re.I | re.S)

#: chrono-humanize output, for the places it appears without the span (the
#: ``title`` attribute on a listing row carries the exact time, but a raw
#: listing prints the humanised form bare).
_HUMANISED = re.compile(
    r"\b(?:now|\d+\s+(?:seconds?|minutes?|hours?|days?|weeks?|months?|years?)"
    r"\s+ago|in\s+\d+\s+\w+|a\s+(?:second|minute|hour|day|week|month|year)"
    r"\s+ago)\b", re.I)


def scrub_text(text: str, prefixes: tuple[str, ...] = ()) -> str:
    """Remove bound-address and filesystem details from a textual value.

    ``prefixes`` are absolute directories belonging to whichever side is being
    measured -- its checkout and the scratch directory its sample tree lives
    in. Several responses legitimately embed a path: a startup error names the
    serve path, and an upload conflict names the file. Without this the
    comparison would be between the oracle's scratch directory and the
    submission's. Longest first, because one prefix can contain another.
    """
    for prefix in sorted(prefixes, key=len, reverse=True):
        if prefix:
            text = text.replace(prefix, "/PATH")
    text = _ADDR_SCHEMED.sub(PLACEHOLDER_HOST, text)
    text = _TMPPATH.sub("/tmp/PATH", text)
    text = _WORKSPACE.sub("/PATH", text)
    text = _OPT.sub("/PATH", text)
    return text


def scrub_header_value(name: str, value: str,
                       prefixes: tuple[str, ...] = ()) -> str:
    lower = name.lower()
    value = scrub_text(value, prefixes)
    if lower == "location":
        # A redirect to a directory is relative in the baseline, but an absolute
        # form is equally valid HTTP, so the host part is placeholdered rather
        # than being allowed to decide the comparison.
        value = _ADDR.sub("HOST:PORT", value)
    if lower in ("etag", "if-none-match", "if-match"):
        value = _ETAG.sub(lambda m: f'{m.group(1) or ""}"INO:{m.group(3)}"',
                          value)
    if lower in SET_VALUED_HEADERS:
        parts = [p.strip().lower() for p in value.split(",") if p.strip()]
        return ", ".join(sorted(parts))
    return value


def header_map(raw_headers: list[tuple[str, str]],
               prefixes: tuple[str, ...] = ()) -> dict[str, list[str]]:
    """Case-insensitive name -> list of scrubbed values, ignored names removed.

    A list, not a string: a repeated header is real HTTP and collapsing it would
    hide a difference. miniserve's ``--header`` can be given twice, and the
    baseline emits both.
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


def decompress(body: bytes, scheme: str) -> bytes:
    if scheme == "gzip":
        return gzip.decompress(body)
    if scheme == "deflate":
        try:
            return zlib.decompress(body)
        except zlib.error:
            return zlib.decompress(body, -zlib.MAX_WBITS)
    if scheme == "br":
        try:
            import brotli                                  # noqa: PLC0415
        except ImportError:
            raise ValueError("brotli is not available in the grader")
        return brotli.decompress(body)
    if scheme == "zstd":
        try:
            import zstandard                               # noqa: PLC0415
        except ImportError:
            raise ValueError("zstandard is not available in the grader")
        return zstandard.ZstdDecompressor().decompressobj().decompress(body)
    raise ValueError(f"unsupported scheme {scheme}")


# ---------------------------------------------------------------------------
# HTML normalisation
# ---------------------------------------------------------------------------

def placeholder_nonce_routes(text: str) -> tuple[str, dict]:
    """Replace the per-boot favicon and stylesheet routes with placeholders.

    Returns the rewritten text and what was found, so a test can assert on the
    *shape* of what was replaced (a ten-hex nonce under the route prefix)
    without the value entering the comparison.
    """
    found: dict[str, object] = {}
    icon = _ICON_HREF.search(text)
    css = _CSS_HREF.search(text)
    if icon:
        found["icon_href"] = icon.group(1)
    if css:
        found["css_href"] = css.group(1)

    # Longest first: with a route prefix the two hrefs share it, and replacing
    # the shorter one first would corrupt the longer.
    for key, placeholder in (("icon_href", "/NONCE-FAVICON"),
                             ("css_href", "/NONCE-CSS")):
        href = found.get(key)
        if isinstance(href, str) and len(href) > 1:
            text = text.replace(href, placeholder)
    return text, found


def placeholder_random_route(text: str, prefix: str | None) -> str:
    """Replace a ``--random-route`` prefix wherever it appears in a page."""
    if prefix and len(prefix) > 1:
        text = text.replace(prefix, "/RANDOM-ROUTE")
    return text


def drop_humanised_times(text: str) -> tuple[str, int]:
    """Blank the humanised mtime column, keeping the exact timestamp beside it.

    The count is returned so a submission that stops rendering the column at
    all is still distinguishable from one that renders it.
    """
    n = 0

    def sub(match: re.Match) -> str:
        nonlocal n
        n += 1
        return match.group(1) + "HUMANISED" + match.group(3)

    text = _HISTORY_SPAN.sub(sub, text)
    return text, n


_LISTING_ROW = re.compile(r"<tr>.*?</tr>", re.S)
_ROW_NAME = re.compile(
    r'<a [^>]*class="(?:file|directory|symlink)"[^>]*>(.*?)</a>', re.S)
_ROW_SIZE = re.compile(r'<td class="size-cell">([^<]*)</td>')
_ROW_DATE = re.compile(r'<td class="date-cell"><span>([^<]*)</span>')


def _row_key(row: str, method: str) -> tuple[str, str, bool]:
    name = _ROW_NAME.search(row)
    cell = (_ROW_SIZE if method == "size" else _ROW_DATE).search(row)
    return (re.sub(r"<[^>]+>", "", name.group(1)).strip() if name else "",
            cell.group(1) if cell else "",
            'class="directory"' in row)


def order_ties_by_name(text: str, method: str, dirs_first: bool = False) -> str:
    """Order rows a size or date sort cannot separate by name.

    The sample tree gives every file the same 14 bytes and every entry the same
    pinned mtime, so those two sorts leave most of a listing tied, and what
    orders a tied run is the order the entries came back from the filesystem --
    a property of the filesystem the tree was materialised on, not of the server
    reading it. The sequence of keys is still compared exactly, and so is the
    directories-first grouping where the flag asks for one.
    """
    if method not in ("size", "date"):
        return text
    rows = list(_LISTING_ROW.finditer(text))
    if not rows:
        return text
    order: list[int] = []
    run: list[tuple[tuple[bool, str], str, int]] = []
    for index, match in enumerate(rows):
        name, cell, isdir = _row_key(match.group(0), method)
        group = (isdir and dirs_first, cell)
        if run and group != run[0][0]:
            order += [i for _, _, i in sorted(run)]
            run = []
        run.append((group, name, index))
    order += [i for _, _, i in sorted(run)]
    if order == list(range(len(rows))):
        return text
    out, last = [], 0
    for slot, source in zip(rows, order):
        out.append(text[last:slot.start()])
        out.append(rows[source].group(0))
        last = slot.end()
    out.append(text[last:])
    return "".join(out)


def html_records(text: str) -> dict:
    """Structural facts about an HTML page, extracted for their own assertions.

    A byte comparison already covers all of this. It is extracted anyway
    because a byte comparison that fails says only "the page differs", while
    these say which part: the entry list, the link targets, the row order, the
    breadcrumb trail, the footer.
    """
    # The baseline emits class before href (`<a class="file" href="...">`), but
    # attribute order is not something a port should be graded on here -- the
    # byte comparison already covers it -- so both orders are recognised and the
    # extracted facts stay meaningful either way.
    entries = []
    for match in re.finditer(r"<a\s+([^>]*?)>(.*?)</a>", text, re.I | re.S):
        attrs, inner = match.group(1), match.group(2)
        href = re.search(r'\bhref="([^"]*)"', attrs)
        cls = re.search(r'\bclass="([^"]*)"', attrs)
        if not href or not cls:
            continue
        if cls.group(1) not in ("file", "directory", "symlink", "root"):
            continue
        entries.append({
            "href": href.group(1),
            "class": cls.group(1),
            # The visible name, with the <bdi>/<span> wrapping the renderer adds
            # stripped, so a name with an ampersand in it is compared as the
            # text a user sees rather than as markup.
            "name": re.sub(r"<[^>]+>", "", inner).strip(),
        })
    breadcrumbs = re.findall(
        r'<h1\s+class="title"[^>]*>(.*?)</h1>', text, re.I | re.S)
    crumb_names = []
    if breadcrumbs:
        crumb_names = [re.sub(r"<[^>]+>", "", c).strip()
                       for c in re.findall(r"<bdi>(.*?)</bdi>", breadcrumbs[0],
                                           re.I | re.S)]
    return {
        "title": (re.findall(r"<title>(.*?)</title>", text, re.I | re.S)
                  or [None])[0],
        "entries": entries,
        "entry_names": [e["name"] for e in entries],
        "entry_hrefs": [e["href"] for e in entries],
        "entry_classes": sorted({e["class"] for e in entries}),
        "breadcrumbs": crumb_names,
        "form_actions": re.findall(r'<form[^>]*\baction="([^"]*)"', text, re.I),
        "has_upload_form": 'id="file_submit"' in text,
        "has_mkdir_form": 'id="mkdir"' in text,
        "has_qr": 'class="qrcode"' in text,
        # The QR's SVG module count. A port that renders a QR for a different
        # string, or at a different error-correction level, produces a different
        # number of modules -- so this is a cheap structural check on top of the
        # byte comparison of the same markup.
        "qr_modules": len(re.findall(r"h1v1h-1", text)),
        "qr_viewbox": (re.findall(r'<svg viewBox="([^"]*)"', text) or [None])[0],
        "has_wget_footer": 'class="downloadDirectory"' in text,
        "version_footer": (re.findall(r'<div class="version">([^<]*)</div>',
                                      text) or [None])[0],
        "sort_links": sorted(set(re.findall(r'href="([^"]*\bsort=[^"]*)"',
                                            text))),
        "archive_links": sorted(set(re.findall(r'href="([^"]*download=[^"]*)"',
                                               text))),
        "script_count": len(re.findall(r"<script", text, re.I)),
        "theme_options": re.findall(r'<li[^>]*\bdata-theme="([^"]*)"', text,
                                    re.I),
        "readme_filename": (re.findall(
            r'<h3 id="readme-filename">([^<]*)</h3>', text) or [None])[0],
        "has_readme": 'id="readme"' in text,
        # The exact-timestamp column, which the pinned sample-tree mtimes make a hard
        # contract. Extracted separately from the humanised span beside it.
        "timestamps": sorted(set(re.findall(
            r"<span>(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [+-]\d{2}:\d{2})\s*"
            r"</span>", text))),
        "size_cells": re.findall(r'<td class="size-cell">([^<]*)</td>', text),
        # Two capture groups, so findall yields tuples; listed explicitly because
        # a tuple and a list of the same strings render identically in a failure
        # message but do not compare equal once the golden side has been through
        # JSON. The graders normalise this too, belt and braces.
        "error_message": [list(pair) for pair in re.findall(
            r'<div class="error"><p>([^<]*)</p><p>([^<]*)</p>', text)],
    }


# ---------------------------------------------------------------------------
# Archive normalisation
# ---------------------------------------------------------------------------

def archive_records(body: bytes, kind: str) -> dict:
    """Member list and unpackability of a tar / tar.gz / zip stream.

    Compared instead of the bytes: a tar's block padding and a zip's central
    directory carry per-implementation detail (extra fields, compression level,
    the order the walker happened to visit) that no two implementations agree
    on byte for byte, while what a *user* gets -- which files, at which paths,
    with which contents -- is exactly reproducible and is what this returns.
    """
    out: dict[str, object] = {"kind": kind, "ok": False, "error": None,
                             "members": [], "count": 0, "digest": None}
    try:
        if kind in ("tar", "tar.gz"):
            mode = "r:gz" if kind == "tar.gz" else "r:"
            with tarfile.open(fileobj=io.BytesIO(body), mode=mode) as tf:
                members = []
                content = hashlib.sha256()
                for info in sorted(tf.getmembers(), key=lambda m: m.name):
                    members.append({
                        "name": info.name,
                        "size": info.size,
                        "isdir": info.isdir(),
                        "islink": info.issym() or info.islnk(),
                    })
                    if info.isfile():
                        handle = tf.extractfile(info)
                        data = handle.read() if handle else b""
                        content.update(info.name.encode("utf-8", "replace"))
                        content.update(data)
                out["members"] = members
                out["digest"] = content.hexdigest()
        elif kind == "zip":
            with zipfile.ZipFile(io.BytesIO(body)) as zf:
                bad = zf.testzip()
                if bad is not None:
                    out["error"] = f"corrupt member {bad}"
                    return out
                members = []
                content = hashlib.sha256()
                for info in sorted(zf.infolist(), key=lambda i: i.filename):
                    members.append({
                        "name": info.filename,
                        "size": info.file_size,
                        "isdir": info.is_dir(),
                        "islink": False,
                    })
                    if not info.is_dir():
                        content.update(info.filename.encode("utf-8", "replace"))
                        content.update(zf.read(info))
                out["members"] = members
                out["digest"] = content.hexdigest()
        else:
            out["error"] = f"unknown archive kind {kind}"
            return out
    except Exception as exc:                                # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out

    out["ok"] = True
    out["count"] = len(out["members"])
    out["names"] = sorted(m["name"] for m in out["members"])
    return out


ARCHIVE_KIND_BY_QUERY = {
    "tar": "tar",
    "tar_gz": "tar.gz",
    "zip": "zip",
}


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------

def scrub_body_text(body: bytes, prefixes: tuple[str, ...] = ()) -> str | None:
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return scrub_text(text, prefixes)


def record(case_id: str, status: int, raw_headers: list[tuple[str, str]],
           body: bytes, *, body_mode: str = "exact",
           decompress_scheme: str | None = None,
           archive_kind: str | None = None,
           random_route: str | None = None,
           prefixes: tuple[str, ...] = ()) -> dict:
    """Freeze one response into the comparable form stored in the golden file."""
    raw = body
    decompressed_ok = None
    if decompress_scheme:
        try:
            body = decompress(body, decompress_scheme)
            decompressed_ok = True
        except Exception:                                   # noqa: BLE001
            decompressed_ok = False

    headers = header_map(raw_headers, prefixes)
    text = scrub_body_text(body, prefixes)

    # Recorded outside the header map, which drops content-length and
    # transfer-encoding on purpose: whether a generated listing arrives with a
    # declared length or chunked is a framework choice no client can tell apart,
    # and grading it would be gratuitous. What is *not* a choice is whether a
    # declared length matches the bytes that follow it, so the raw values are
    # kept here for that check and for nothing else.
    lowered = {k.lower(): v for k, v in raw_headers}
    declared = lowered.get("content-length")
    try:
        declared_length = int(declared) if declared is not None else None
    except ValueError:
        declared_length = -1        # present but not a number, which is a bug

    entry: dict[str, object] = {
        "id": case_id,
        "status": status,
        "headers": headers,
        "header_names": sorted(headers),
        # Three lengths, because they answer three different questions and
        # conflating them cost a real bug:
        #
        #   raw_len   bytes on the wire, before any decompression
        #   body_len  bytes after decompression, before scrubbing -- the number a
        #             declared Content-Length has to agree with
        #   text_len  characters after scrubbing, which is the only one that is
        #             comparable across runs
        #
        # A body that embeds a filesystem path (miniserve's 500 pages name the
        # path they failed to create) has a body_len that depends on where the
        # harness happened to materialise its sample tree. Grading that compares
        # workdir names, not behaviour.
        "body_len": len(body),
        "raw_len": len(raw),
        "text_len": None,
        "decompressed": decompressed_ok,
        "body_sha256": hashlib.sha256(body).hexdigest(),
        "declared_length": declared_length,
        "chunked": "chunked" in (lowered.get("transfer-encoding") or "").lower(),
        "body_mode": body_mode,
        "is_text": text is not None,
        "nonce": None,
        "humanised_spans": 0,
        "html": None,
        "archive": None,
    }

    if text is None:
        entry["body"] = f"<<binary:{len(body)} bytes>>"
    else:
        if body_mode == "html" or (text.lstrip()[:9].lower() == "<!doctype"):
            text, nonce = placeholder_nonce_routes(text)
            text = placeholder_random_route(text, random_route)
            entry["nonce"] = nonce
            entry["html"] = html_records(text)
            text, spans = drop_humanised_times(text)
            entry["humanised_spans"] = spans
        else:
            text = placeholder_random_route(text, random_route)
        entry["body"] = text
        entry["text_len"] = len(text)

    if archive_kind:
        entry["archive"] = archive_records(body, archive_kind)

    return entry
