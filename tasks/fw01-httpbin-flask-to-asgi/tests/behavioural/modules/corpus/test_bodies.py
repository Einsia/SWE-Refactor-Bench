"""Response bodies must match frozen State A.

Each case declares how its body is compared (``body_mode`` in harness.corpus):

``exact``        byte-for-byte after host/peer scrubbing
``json``         parsed and compared as data (key order still checked separately)
``gzip-json``    compared as data; the transport already inflated it
``deflate-json`` idem, zlib
``br-json``      idem, brotli
``len``          only the length (bodies with per-request random content)
``ignore``       no byte diff here; compared structurally elsewhere -- usually
                 test_structural.py, but /spec.json and the UI page in
                 test_openapi.py, and two cases (delay-clamped,
                 response-headers-unicode) deliberately not at all, each saying so
                 in its own corpus note

On the three compressed families, note *what the client sees*. httpx inflates a
response whose ``Content-Encoding`` it recognises, whatever the request asked
for, so neither the golden capture nor this replay ever held the compressed
bytes: both sides recorded the inflated payload (the golden's ``body_len`` is the
inflated length, while its captured ``content-length`` header is the compressed
one). Comparing them as data is therefore the honest comparison here, and it
still catches a server that declares an encoding it did not apply, because the
inflate would fail. That the bytes on the wire really are a valid stream of the
declared type is asserted separately, over a raw socket, in
``test_compression.py``.

Splitting the JSON cases into a data comparison *and* a separate serialisation
check means a submission that gets the data right but the formatting wrong loses
one point rather than all of them, and the failure says which.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from harness import corpus, normalize

pytestmark = pytest.mark.behaviour

COMPARED = [c["id"] for c in corpus.CASES if c["body_mode"] != "ignore"]
JSONISH = [
    c["id"] for c in corpus.CASES
    if c["body_mode"] in ("json", "gzip-json", "deflate-json", "br-json")
]


def _style_checked() -> list[str]:
    """The cases whose State A body is a JSON document, decided at collection.

    The serialisation contract only exists where State A emitted JSON: /image,
    /robots.txt, /html, /deny and the 406 page are not JSON documents in State A
    either, and there is no indent to preserve in a PNG.

    The skip was read from the *recording*, never from the submission, so it
    fired identically for every candidate -- but an unlicensed skip is rewritten
    to a failure by the module contract, on the grounds that in a fixed offline
    environment nothing is skipped unless an artefact was never produced.  That
    cost unmodified State A 252 of 1752 checks in a module weighted 0.36, which
    is the whole stage for answering exactly as recorded.

    Licensing the skip with ``srb_skip_ok`` would have been the smaller edit and
    the wrong one: the marker is function-scoped, so it would also have licensed
    the guard above it, which reads the *submission* ("request failed; reported
    by the status test") and must keep costing a submission points.  So the list
    is filtered instead, and the pool contains only cases the contract applies
    to.

    Read from the same frozen file the ``golden`` fixture reads.  If it cannot be
    read at collection time, fall back to the declared JSON body modes; the
    fixture will then fail the module loudly for the real reason.
    """
    suite = Path(os.environ.get("SRB_SUITE_DIR", "/tests/behavioural"))
    try:
        recorded = json.loads(
            (suite / "data" / "responses.json").read_text())["responses"]
    except (OSError, ValueError, KeyError):
        return list(JSONISH)
    out = []
    for case in corpus.CASES:
        rec = recorded.get(case["id"])
        if not rec:
            continue
        try:
            if normalize.is_httpbin_json(normalize.decode_body(rec["body"])):
                out.append(case["id"])
        except Exception:
            continue
    return out


STYLE_CHECKED = _style_checked()


def _describe(case) -> str:
    q = "?" + case["query"] if case["query"] else ""
    return f"{case['method']} {case['path']}{q}"


def _payload(case, rec):
    """The bytes to compare for this case.

    No decompression happens here: for the compressed families the transport
    inflated the body before either side ever recorded it, so both the golden
    and the replay already hold plain JSON. Decompressing again would fail on
    unmodified State A -- and did, before this was measured.
    """
    return normalize.decode_body(rec["body"])


def _excerpt(raw: bytes, limit: int = 700) -> str:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return repr(raw[:limit])
    return text if len(text) <= limit else text[:limit] + f"... (+{len(text) - limit}B)"


def _first_difference(a: bytes, b: bytes) -> str:
    n = min(len(a), len(b))
    for i in range(n):
        if a[i] != b[i]:
            lo = max(0, i - 40)
            return (
                f"first differs at byte {i}:\n"
                f"  State A ...{a[lo:i + 40]!r}\n"
                f"  got     ...{b[lo:i + 40]!r}"
            )
    return f"one is a prefix of the other: State A {len(a)}B, got {len(b)}B"


@pytest.mark.parametrize("case_id", COMPARED)
def test_body(case_id, replay, golden):
    if case_id in replay["errors"]:
        pytest.fail(f"request could not be completed: {replay['errors'][case_id]}")
    case = corpus.CASES_BY_ID[case_id]
    mode = case["body_mode"]
    got_rec = replay["responses"][case_id]
    want_rec = golden[case_id]

    if mode == "len":
        assert got_rec["body_len"] == want_rec["body_len"], (
            f"{_describe(case)} body is {got_rec['body_len']}B, "
            f"State A produced {want_rec['body_len']}B "
            f"(content is per-request, only the length is compared)"
        )
        return

    try:
        want = _payload(case, want_rec)
    except Exception as exc:  # pragma: no cover - golden is validated at build
        pytest.fail(f"golden body for {case_id} is undecodable: {exc}")
    try:
        got = _payload(case, got_rec)
    except Exception as exc:
        pytest.fail(
            f"{_describe(case)} body could not be decoded as {mode}: "
            f"{type(exc).__name__}: {exc}"
        )

    if mode == "exact":
        if got == want:
            return
        pytest.fail(
            f"{_describe(case)} body mismatch ({len(got)}B vs "
            f"{len(want)}B).\n{_first_difference(want, got)}\n"
            f"--- State A ---\n{_excerpt(want)}\n--- got ---\n{_excerpt(got)}"
        )

    # json-ish: compare as data
    try:
        want_data = normalize.parse_json_body(want)
    except ValueError as exc:  # pragma: no cover
        pytest.fail(f"golden body for {case_id} is not JSON: {exc}")
    try:
        got_data = normalize.parse_json_body(got)
    except ValueError as exc:
        pytest.fail(
            f"{_describe(case)} did not return JSON: {exc}\n"
            f"--- got ---\n{_excerpt(got)}"
        )
    assert got_data == want_data, (
        f"{_describe(case)} JSON payload differs.\n"
        f"--- State A ---\n{json.dumps(want_data, indent=2, sort_keys=True)[:900]}\n"
        f"--- got ---\n{json.dumps(got_data, indent=2, sort_keys=True)[:900]}"
    )


# ---------------------------------------------------------------------------
# The serialisation contract
# ---------------------------------------------------------------------------

#: Whether a case is style-checked is decided from the golden, not assumed:
#: /status/406, /image and /stream/N genuinely emit compact JSON in State A, and
#: demanding pretty-printing there would be demanding a behaviour change.  The
#: cases where State A emitted no JSON at all are not parametrised in -- see
#: ``_style_checked`` for why that is a filter rather than a skip.
@pytest.mark.parametrize("case_id", STYLE_CHECKED)
def test_json_serialisation_style(case_id, replay, golden):
    """Where State A pretty-printed, State B must too -- and vice versa.

    httpbin's JSON is two-space indented with sorted keys and a trailing
    newline. That is observable output, so it is part of the contract; a
    submission that switches to ``json.dumps`` defaults changes every payload.
    """
    if case_id in replay["errors"]:
        pytest.skip("request failed; reported by the status test")
    case = corpus.CASES_BY_ID[case_id]
    want_raw = normalize.decode_body(golden[case_id]["body"])
    got_raw = normalize.decode_body(replay["responses"][case_id]["body"])
    want_violations = normalize.json_style_violations(want_raw)
    got_violations = normalize.json_style_violations(got_raw)

    if not want_violations:
        assert not got_violations, (
            f"{_describe(case)} broke the JSON serialisation contract "
            f"(two-space indent, sorted keys, trailing newline): "
            f"{got_violations}\n--- got ---\n{_excerpt(got_raw, 400)}"
        )
    else:
        # State A itself is compact/NDJSON here: the submission must not
        # "improve" it into pretty-printed JSON.
        assert got_violations, (
            f"{_describe(case)} now pretty-prints its body, but State A emitted "
            f"it compactly ({want_violations}). This changes the bytes clients "
            f"receive."
        )


def test_body_length_matches_payload(replay):
    """A declared Content-Length must equal the bytes actually delivered."""
    bad = {}
    for case_id, rec in replay["responses"].items():
        raw = replay["raw"].get(case_id, b"")
        if rec["body_len"] != len(raw):
            bad[case_id] = (rec["body_len"], len(raw))
    assert not bad, f"recorded length disagrees with delivered bytes: {bad}"
