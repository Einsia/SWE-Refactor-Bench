"""Response headers must match frozen State A.

Three complementary views:

*   per case, the whole compared multi-map (catches a wrong value anywhere);
*   per case, the *shape* of headers whose value is inherently per-request
    (digest nonces, ETags) -- those cannot be compared literally, but they can
    still be required to look right;
*   per header name, the exact set of cases carrying it (catches a submission
    that sprays a header onto every route, or silently drops one from a few).

Framing headers (Server, Date, Content-Length, Transfer-Encoding, Connection)
are excluded in harness.normalize, matching what instruction.md promises.
"""

from __future__ import annotations

import json
import re

import pytest

from harness import corpus, normalize

pytestmark = pytest.mark.behaviour

IDS = [c["id"] for c in corpus.CASES]


def _describe(case) -> str:
    q = "?" + case["query"] if case["query"] else ""
    return f"{case['method']} {case['path']}{q}"


def _compared(case, headers):
    """Restrict a header map to the names this case compares by value."""
    names = set(normalize.compared_header_names(case))
    skip = set(case.get("nondet") or ())
    return {k: v for k, v in headers.items() if k in names and k not in skip}


@pytest.mark.parametrize("case_id", IDS)
def test_headers(case_id, replay, golden):
    if case_id in replay["errors"]:
        pytest.fail(f"request could not be completed: {replay['errors'][case_id]}")
    case = corpus.CASES_BY_ID[case_id]
    got = _compared(case, replay["responses"][case_id]["headers"])
    want = _compared(case, golden[case_id]["headers"])
    if got == want:
        return

    lines = []
    for name in sorted(set(got) | set(want)):
        a, b = want.get(name), got.get(name)
        if a == b:
            continue
        if a is None:
            lines.append(f"  + {name}: {b!r} (State A did not send this header)")
        elif b is None:
            lines.append(f"  - {name}: missing, State A sent {a!r}")
        else:
            lines.append(f"  ~ {name}: got {b!r}, State A sent {a!r}")
    pytest.fail(f"{_describe(case)} header mismatch:\n" + "\n".join(lines))


# ---------------------------------------------------------------------------
# Per-request header values: compare the shape, not the bytes
# ---------------------------------------------------------------------------

SHAPE_CASES = [
    (c["id"], h)
    for c in corpus.CASES
    for h in (c.get("nondet") or ())
    if h in corpus.NONDET_HEADER_SHAPES
]


@pytest.mark.parametrize("case_id,header", SHAPE_CASES,
                         ids=[f"{i}-{h}" for i, h in SHAPE_CASES])
def test_nondeterministic_header_shape(case_id, header, replay, golden):
    if case_id in replay["errors"]:
        pytest.fail(f"request could not be completed: {replay['errors'][case_id]}")
    case = corpus.CASES_BY_ID[case_id]
    want_present = header in golden[case_id]["headers"]
    values = replay["responses"][case_id]["headers"].get(header)

    if not want_present:
        assert values is None, (
            f"{_describe(case)} sent {header}: {values!r}, State A sent none"
        )
        return

    assert values, (
        f"{_describe(case)} is missing {header}; State A sent "
        f"{golden[case_id]['headers'][header]!r} (value varies per request, so "
        f"only its shape is checked)"
    )
    pattern = re.compile(corpus.NONDET_HEADER_SHAPES[header])
    for value in values:
        assert pattern.search(value), (
            f"{_describe(case)} sent {header}: {value!r}, which does not match "
            f"the State A shape /{pattern.pattern}/"
        )


# ---------------------------------------------------------------------------
# Per header name: which routes carry it at all
# ---------------------------------------------------------------------------

def _presence_sets(records):
    out = {}
    for case in corpus.CASES:
        rec = records.get(case["id"])
        if rec is None:
            continue
        names = set(normalize.compared_header_names(case))
        for name in rec["headers"]:
            if name in names:
                out.setdefault(name, set()).add(case["id"])
    return out


GOLDEN_HEADER_NAMES = sorted({
    n
    for c in corpus.CASES
    for n in normalize.compared_header_names(c)
})


@pytest.mark.parametrize("header", GOLDEN_HEADER_NAMES)
def test_header_presence_set(header, replay, golden):
    """The set of routes emitting a given header must be unchanged."""
    want = _presence_sets(golden).get(header, set())
    got = _presence_sets(replay["responses"]).get(header, set())
    # Cases whose request failed outright are reported by the status tests.
    got |= (want & set(replay["errors"]))
    if got == want:
        return
    extra = sorted(got - want)
    missing = sorted(want - got)
    detail = []
    if missing:
        detail.append(f"no longer sent on {len(missing)}: {missing[:12]}")
    if extra:
        detail.append(f"newly sent on {len(extra)}: {extra[:12]}")
    pytest.fail(f"{header} presence changed -- " + "; ".join(detail))


# ---------------------------------------------------------------------------
# Header discipline that no single case can express
# ---------------------------------------------------------------------------

def test_no_unexpected_semantic_headers(replay, golden):
    """A submission must not invent semantic headers State A never sent."""
    offenders = {}
    for case in corpus.CASES:
        if case["id"] in replay["errors"]:
            continue
        names = set(normalize.compared_header_names(case))
        got = set(replay["responses"][case["id"]]["headers"]) & names
        want = set(golden[case["id"]]["headers"]) & names
        new = got - want
        if new:
            offenders[case["id"]] = sorted(new)
    assert not offenders, (
        "these routes now send semantic headers State A never sent:\n"
        + json.dumps(dict(sorted(offenders.items())), indent=2)[:2500]
    )


def test_header_names_are_latin1_tokens(replay):
    """Header names must stay valid tokens: ASGI byte-strings make it easy to
    emit something a strict client rejects."""
    token = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
    bad = {}
    for case_id, rec in replay["responses"].items():
        for name, _ in rec["raw_headers"]:
            if not token.match(name):
                bad.setdefault(case_id, []).append(name)
    assert not bad, f"invalid header names: {json.dumps(bad, indent=2)[:1200]}"


def test_no_duplicate_singleton_headers(replay, golden):
    """Headers that must appear at most once really do -- unless State A itself
    repeated them.

    ``/response-headers?Content-Type=text/plain`` is the exception that proves
    the rule: State A sends jsonify's ``application/json`` *and* the requested
    ``text/plain``, two Content-Type headers on one response. That is the
    behaviour being preserved, so the golden decides. Cases where State A sent
    one must still send one; cases where it sent two must still send two, which
    the per-case header comparison already pins.
    """
    singletons = (
        "content-type", "location", "content-range", "etag",
        "access-control-allow-origin", "access-control-allow-credentials",
        "allow", "content-encoding",
    )
    bad = {}
    for case_id, rec in replay["responses"].items():
        want = golden.get(case_id, {}).get("headers", {})
        for name in singletons:
            values = rec["headers"].get(name) or []
            allowed = max(1, len(want.get(name) or []))
            if len(values) > allowed:
                bad.setdefault(case_id, {})[name] = {
                    "got": values, "state_a_sent": want.get(name) or [],
                }
    assert not bad, (
        "these responses repeat a header more times than State A did:\n"
        + json.dumps(bad, indent=2)[:1500]
    )
