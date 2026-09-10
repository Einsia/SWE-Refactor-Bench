"""How a recorded response is compared with the golden one.

Kept in one place so the four behavioural suites cannot drift into disagreeing
about what counts as a difference. Two rules matter:

*   **Volatility is per field, not per case.** The golden file lists the leaf
    paths that differed between its two capture passes. A field on that list is
    skipped; every other field of the same case is still compared. A case whose
    ETag varies is still graded on its status, its content type, its length and
    its body.
*   **A missing recording is a failure, not a skip.** If the submission could not
    be booted for a session, or answered nothing for a case, the case fails. The
    alternative -- skipping -- would let a submission improve its score by
    refusing to start.

Diffs are truncated hard. A wrong listing page can differ in fifty thousand
characters, and a report that prints all of them is a report nobody reads.
"""

from __future__ import annotations

import json

#: Compared for every case regardless of what the case declares.
#:
#: ``content-length`` and ``transfer-encoding`` are deliberately absent: whether
#: a generated listing arrives with a declared length or chunked is a framework
#: choice no client can tell apart, and the bytes themselves are compared anyway.
#: ``x-srb-transport`` is the harness's own marker for a connection that was
#: refused or closed without an answer -- compared, because "hangs up on a
#: malformed request line" is behaviour like any other.
ALWAYS = ("content-type", "content-encoding", "content-disposition",
          "accept-ranges", "content-range", "location", "www-authenticate",
          "vary", "cache-control", "allow", "x-srb-transport")

MAX_DIFF = 1600


def _trim(value, limit: int = MAX_DIFF) -> str:
    text = value if isinstance(value, str) else json.dumps(value, indent=1,
                                                           sort_keys=True,
                                                           default=repr)
    if len(text) <= limit:
        return text
    half = limit // 2
    return (f"{text[:half]}\n    ... [{len(text) - limit} characters elided] "
            f"...\n{text[-half:]}")


def require(got, want, session_id: str, case_id: str):
    """Both recordings exist, or a failure that says which side is missing."""
    if want is None:
        raise AssertionError(
            f"{session_id}::{case_id}: no golden recording. The corpus and the "
            f"golden file are out of step, which is a harness bug, not a "
            f"submission failure.")
    if got is None:
        raise AssertionError(
            f"{session_id}::{case_id}: the submission produced no response. "
            f"Either the server for session {session_id!r} could not be "
            f"started, or it died during the session.")


MISSING = "<absent>"


def canonical(value):
    """Put a value in the shape it would have after a JSON round trip.

    The golden side has been through ``json.dump``/``json.load``; the live side
    has not. Without this, a tuple and a list of the same strings -- which
    ``re.findall`` produces interchangeably depending on how many capture groups
    a pattern happens to have -- compare unequal while *rendering identically* in
    the failure message. That is the worst possible failure mode for a grader: a
    report that says two visibly identical values differ.
    """
    if isinstance(value, tuple):
        return [canonical(item) for item in value]
    if isinstance(value, list):
        return [canonical(item) for item in value]
    if isinstance(value, dict):
        return {str(k): canonical(v) for k, v in value.items()}
    return value


def resolve(entry: dict, field: str):
    """Read a leaf by its dotted path -- ``html.timestamps``, ``nonce.css_href``.

    A recording keeps ``headers``, ``html`` and ``nonce`` as nested maps, while
    the capture's volatility survey names leaves by the flattened path. Both have
    to agree, so every read here goes through the same walk. Getting this wrong is
    quiet rather than loud: ``entry.get("html.timestamps")`` is ``None`` on both
    sides, and a comparison of ``None`` with ``None`` passes.
    """
    node = entry
    for part in field.split("."):
        if not isinstance(node, dict) or part not in node:
            return MISSING
        node = node[part]
    return canonical(node)


def volatile(want: dict, field: str | None = None):
    """The volatile leaf paths, or whether one specific path is among them."""
    names = set(want.get("volatile", ()))
    return names if field is None else field in names


def compare_field(got: dict, want: dict, field: str, session_id: str,
                  case_id: str, *, note: str = ""):
    """One leaf field, skipped if the capture found it volatile."""
    if volatile(want, field):
        return
    a, b = resolve(got, field), resolve(want, field)
    if a != b:
        raise AssertionError(
            f"{session_id}::{case_id}: {field} differs"
            + (f" ({note})" if note else "")
            + f"\n  expected: {_trim(b, 700)}\n  actual:   {_trim(a, 700)}")


def compare_headers(got: dict, want: dict, extra: tuple[str, ...],
                    skip: tuple[str, ...], session_id: str, case_id: str):
    """The always-compared headers plus whatever the case names."""
    vol = volatile(want)
    skipped = {h.lower() for h in skip}
    names = [h for h in (tuple(ALWAYS) + tuple(extra))
             if h.lower() not in skipped]
    got_headers = got.get("headers") or {}
    want_headers = want.get("headers") or {}
    problems = []
    for name in dict.fromkeys(names):
        if f"headers.{name}" in vol:
            continue
        a = canonical(got_headers.get(name, MISSING))
        b = canonical(want_headers.get(name, MISSING))
        if a != b:
            problems.append(f"  {name}:\n    expected: {_trim(b, 300)}"
                            f"\n    actual:   {_trim(a, 300)}")
    if problems:
        raise AssertionError(f"{session_id}::{case_id}: header mismatch\n"
                             + "\n".join(problems))


def compare_body(got: dict, want: dict, session_id: str, case_id: str):
    """The body, by whichever means the case's body_mode calls for."""
    vol = volatile(want)
    mode = want.get("body_mode", "exact")

    if mode == "archive":
        if "archive" not in vol:
            compare_field(got, want, "archive", session_id, case_id,
                          note="archive member list")
        return

    if mode == "binary" or not want.get("is_text", True):
        if "body_len" not in vol:
            compare_field(got, want, "body_len", session_id, case_id)
        if "body_sha256" not in vol:
            compare_field(got, want, "body_sha256", session_id, case_id,
                          note="binary body digest")
        return

    if mode == "shape":
        # The body legitimately differs beyond nonces, so structure only. For an
        # HTML page that is its element records; otherwise its scrubbed length.
        if want.get("html") is not None:
            _compare_html_shape(got, want, session_id, case_id)
        else:
            compare_field(got, want, "text_len", session_id, case_id)
        return

    if "body" not in vol:
        got_body = got.get("body")
        want_body = want.get("body")
        if got_body != want_body:
            raise AssertionError(
                f"{session_id}::{case_id}: body differs\n"
                f"  expected ({len(want_body or '')} chars):\n"
                f"{_trim(want_body)}\n"
                f"  actual ({len(got_body or '')} chars):\n{_trim(got_body)}")


#: The structural facts compared for a ``shape`` body -- the ones that survive a
#: page whose links embed a per-boot random route. Deliberately excludes
#: ``entry_hrefs``, ``form_actions``, ``sort_links`` and ``archive_links``, which
#: are exactly the fields that carry the route.
SHAPE_KEYS = ("title", "entry_names", "entry_classes", "breadcrumbs",
              "has_upload_form", "has_mkdir_form", "has_qr", "qr_modules",
              "qr_viewbox", "has_wget_footer", "version_footer",
              "script_count", "theme_options", "readme_filename", "has_readme",
              "timestamps", "size_cells", "error_message")


def _compare_html_shape(got: dict, want: dict, session_id: str, case_id: str):
    a, b = got.get("html") or {}, want.get("html") or {}
    vol = volatile(want)
    problems = []
    for key in SHAPE_KEYS:
        if f"html.{key}" in vol:
            continue
        if canonical(a.get(key, MISSING)) != canonical(b.get(key, MISSING)):
            problems.append(f"  html.{key}:\n"
                            f"    expected: {_trim(b.get(key), 320)}\n"
                            f"    actual:   {_trim(a.get(key), 320)}")
    if problems:
        raise AssertionError(f"{session_id}::{case_id}: page structure differs\n"
                             + "\n".join(problems))
