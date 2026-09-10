"""Requests must not contaminate one another.

The graded corpus is replayed sequentially, one request at a time, into a fresh
connection. That is the friendliest possible schedule, and it hides the single
most common defect in a WSGI-to-ASGI port: per-request state parked somewhere
that is not per-request.

Under Flask, ``request`` is a thread-local proxy and gunicorn/gevent gives each
request its own greenlet, so a module-level ``current_request`` global, a value
cached on the app object, or a ``self._body`` on a shared handler instance all
appear to work. Move the same code onto a single-threaded event loop and every
one of those becomes a data race the moment two requests overlap -- and the
sequential corpus replay will never show it.

So this module asks three things the corpus cannot:

* **Determinism** -- the same request, issued twice, gets the same answer.
* **Isolation** -- N *different* requests issued concurrently each get their own
  answer, not each other's.
* **Order independence** -- a shuffled schedule produces the same answers as the
  recorded one, so nothing depends on having been asked in corpus order.

All three compare the submission against itself, so none of them can be failed
by a State A behaviour this benchmark chose not to pin.
"""

from __future__ import annotations

import concurrent.futures
import json
import random
import sys

import pytest


from harness import client as hclient  # noqa: E402
from harness import corpus, normalize  # noqa: E402

pytestmark = pytest.mark.migration

#: Cases whose answer legitimately differs between two identical requests, as
#: measured during the golden capture by playing the corpus against two
#: identical State A servers and diffing. Nothing here can be checked for
#: repeatability, so nothing here is.
UNSTABLE_IDS = frozenset({
    "cache-no-conditional", "uuid",
    "digest-challenge-MD5", "digest-challenge-SHA-256",
    "digest-challenge-SHA-512", "digest-challenge-auth",
    "digest-challenge-auth-int", "digest-challenge-badalgo",
    "digest-challenge-badqop", "digest-challenge-never-stale",
})

#: ``len`` bodies are unseeded randomness; ``ignore`` bodies are compared by a
#: dedicated structural test. Both are excluded from byte-for-byte repeats.
REPEATABLE = [
    c for c in corpus.CASES
    if c["id"] not in UNSTABLE_IDS and c["body_mode"] not in ("len", "ignore")
]

#: Paths whose response *describes the request*: url, args, headers, form, data.
#: Two of these answered from shared state visibly return each other's content,
#: so a mix-up is unambiguous rather than merely statistical.
ECHO_PREFIXES = ("/get", "/post", "/put", "/patch", "/delete", "/anything",
                 "/headers", "/user-agent", "/response-headers")

#: A cheap, self-identifying subset for the concurrency tests. Every second case
#: is taken so the sample spans the whole corpus instead of clustering in the
#: first group.
IDENTIFYING_CASES = [
    c for c in corpus.CASES
    if c["body_mode"] not in ("len", "ignore")
    and c["id"] not in UNSTABLE_IDS
    and any(c["path"] == p or c["path"].startswith(p + "/")
            for p in ECHO_PREFIXES)
][::2][:60]


def _semantic(case, resp, host, port):
    """The comparable part of a response: status, compared headers, body."""
    record = normalize.record(
        case, resp.status_code, list(resp.headers.multi_items()),
        resp.content, host, port,
    )
    names = normalize.compared_header_names(case)
    nondet = set(case.get("nondet", ()))
    headers = {n: v for n, v in record["headers"].items()
               if n in names and n not in nondet}
    return record["status"], headers, record["body"]


def _send_alone(case, server):
    """Play one case on a client of its own, then throw the client away.

    Every comparison in this module is between two observations this module made
    itself, so both sides must be gathered the same way -- and the only way to
    make a concurrent observation comparable to a sequential one is to give both
    an empty cookie jar. The graded corpus replay deliberately shares one
    client, so ``/cookies/set`` and the digest challenges leave cookies in its
    jar that later echo endpoints report; borrowing that baseline here would
    have compared a request that sent ``Cookie:`` against one that did not.
    """
    with hclient.make_client(timeout=90.0) as c:
        resp = hclient.send_case(c, case, server.base_url)
        return _semantic(case, resp, "127.0.0.1", server.port)


# ---------------------------------------------------------------------------
# Determinism: ask twice, get the same answer
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def two_passes(server):
    """Play the repeatable corpus twice, each request on a fresh client."""
    passes = []
    for _ in range(2):
        out = {}
        for case in REPEATABLE:
            try:
                out[case["id"]] = ("ok", _send_alone(case, server))
            except Exception as exc:
                out[case["id"]] = ("error", f"{type(exc).__name__}: {exc}")
        passes.append(out)
    return passes


@pytest.mark.parametrize("case", REPEATABLE, ids=[c["id"] for c in REPEATABLE])
def test_request_is_deterministic(case, two_passes, server):
    """The same request twice must produce the same answer.

    A response that drifts between two identical requests means state is being
    carried across them.
    """
    first_kind, first_semantic = two_passes[0][case["id"]]
    second_kind, second = two_passes[1][case["id"]]
    assert first_kind == "ok", (
        f"{case['id']} could not be played at all: {first_semantic}"
    )
    assert second_kind == "ok", (
        f"{case['id']} failed on the second pass but not the first: {second}"
    )
    assert first_semantic == second, (
        f"{case['method']} {case['path']}"
        f"{'?' + case['query'] if case['query'] else ''} answered differently "
        f"on two identical requests.\n"
        f"  first : status={first_semantic[0]} headers={first_semantic[1]}\n"
        f"  second: status={second[0]} headers={second[1]}\n"
        f"  bodies {'match' if first_semantic[2] == second[2] else 'DIFFER'}\n"
        f"State A answers this one identically both times, so something is "
        f"being carried from one request to the next."
    )


# ---------------------------------------------------------------------------
# Isolation: many different requests at once
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def concurrent_answers(server):
    """Fire the identifying subset all at once and collect each answer.

    One client per request, so nothing is serialised by connection reuse and
    the server genuinely sees them overlap.
    """
    def one(case):
        with hclient.make_client(timeout=90.0) as c:
            resp = hclient.send_case(c, case, server.base_url)
            return _semantic(case, resp, "127.0.0.1", server.port)

    out = {}
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=len(IDENTIFYING_CASES)) as pool:
        futures = {pool.submit(one, c): c for c in IDENTIFYING_CASES}
        for fut, case in futures.items():
            try:
                out[case["id"]] = ("ok", fut.result())
            except Exception as exc:
                out[case["id"]] = ("error", f"{type(exc).__name__}: {exc}")
    return out


@pytest.mark.parametrize("case", IDENTIFYING_CASES,
                         ids=[c["id"] for c in IDENTIFYING_CASES])
def test_concurrent_requests_do_not_cross_talk(case, concurrent_answers,
                                               two_passes):
    """Answered correctly even when 60 different requests are in flight.

    Each of these routes echoes its own request back, so if per-request state is
    shared, a request gets a neighbour's URL, method, headers or body -- which
    shows up here as this case's answer not matching its own sequential one.

    The sequential baseline is this module's own first pass, gathered the same
    jar-free way as the concurrent answers, so the only difference between the
    two observations is the schedule.
    """
    kind, want = two_passes[0][case["id"]]
    if kind != "ok":
        pytest.skip("the sequential pass could not reach the server")
    kind, got = concurrent_answers[case["id"]]
    assert kind == "ok", (
        f"{case['id']} failed when issued concurrently with "
        f"{len(IDENTIFYING_CASES) - 1} others, but succeeded on its own: {got}"
    )
    assert want == got, (
        f"{case['method']} {case['path']} answered differently when issued "
        f"concurrently with {len(IDENTIFYING_CASES) - 1} other requests than "
        f"when issued alone.\n"
        f"  alone      : status={want[0]} headers={want[1]}\n"
        f"  concurrent : status={got[0]} headers={got[1]}\n"
        f"  bodies {'match' if want[2] == got[2] else 'DIFFER'}\n"
        f"This is the signature of per-request state that is not per-request: "
        f"a module-level global, a value cached on the app, or an attribute on "
        f"a shared handler instance. Under Flask each request had its own "
        f"greenlet and thread-local, so the same code worked; on an event loop "
        f"it does not."
    )


def test_no_answer_was_lost_under_concurrency(concurrent_answers):
    """Every concurrent request must have produced an answer."""
    errored = {cid: detail for cid, (kind, detail) in concurrent_answers.items()
               if kind != "ok"}
    assert not errored, (
        f"{len(errored)} of {len(IDENTIFYING_CASES)} requests failed when "
        f"issued concurrently:\n{json.dumps(errored, indent=2)}"
    )


def test_identical_requests_in_flight_together_agree(server):
    """The same request 40 times at once must give 40 identical answers.

    A shared mutable buffer shows up as a torn or duplicated body long before
    it shows up as an exception.
    """
    case = corpus.CASES_BY_ID["post-form"]

    def one():
        with hclient.make_client(timeout=90.0) as c:
            resp = hclient.send_case(c, case, server.base_url)
            return resp.status_code, resp.content

    with concurrent.futures.ThreadPoolExecutor(max_workers=40) as pool:
        results = [f.result() for f in
                   [pool.submit(one) for _ in range(40)]]
    distinct = {r for r in results}
    assert len(distinct) == 1, (
        f"the same request issued 40 times concurrently produced "
        f"{len(distinct)} different answers:\n"
        + "\n".join(f"  HTTP {st}, {len(b)}B, {b[:160]!r}"
                    for st, b in sorted(distinct)[:6])
    )


def test_distinct_bodies_are_not_swapped(server):
    """Twenty POSTs with distinguishable bodies, all at once, echoed correctly.

    ``/post`` echoes the request body, so a swap is unambiguous: the response
    names the wrong sender.
    """
    payloads = [f"marker-{i:03d}-" + ("x" * (i * 7)) for i in range(20)]

    def one(payload):
        with hclient.make_client(timeout=90.0) as c:
            resp = c.post(server.base_url + "/post", content=payload.encode(),
                          headers={"Content-Type": "text/plain"})
            return payload, resp.status_code, resp.json().get("data")

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
        results = [f.result() for f in
                   [pool.submit(one, p) for p in payloads]]

    wrong = {sent: got for sent, status, got in results
             if status != 200 or got != sent}
    assert not wrong, (
        f"{len(wrong)} of 20 concurrent POSTs were echoed the wrong body back. "
        f"Each request sent a distinct marker, so this is bodies being read "
        f"from shared state:\n"
        + "\n".join(f"  sent {s[:24]!r} -> got {g if g is None else g[:24]!r}"
                    for s, g in list(wrong.items())[:8])
    )


def test_distinct_query_strings_are_not_swapped(server):
    """Same shape for the request line: 30 concurrent /get with unique args."""
    def one(i):
        with hclient.make_client(timeout=90.0) as c:
            resp = c.get(server.base_url + f"/get?marker={i}&pad=" + "y" * i)
            return i, resp.status_code, resp.json().get("args", {}).get("marker")

    with concurrent.futures.ThreadPoolExecutor(max_workers=30) as pool:
        results = [f.result() for f in [pool.submit(one, i) for i in range(30)]]

    wrong = {i: got for i, status, got in results
             if status != 200 or got != str(i)}
    assert not wrong, (
        f"{len(wrong)} of 30 concurrent /get requests reported another "
        f"request's query string: {wrong}"
    )


def test_distinct_headers_are_not_swapped(server):
    """And for request headers, which /headers echoes verbatim."""
    def one(i):
        with hclient.make_client(timeout=90.0) as c:
            resp = c.get(server.base_url + "/headers",
                         headers={"X-Marker": f"m{i}"})
            got = resp.json().get("headers", {})
            # Header names are title-cased by httpbin's echo; accept either.
            value = got.get("X-Marker") or got.get("x-marker")
            return i, resp.status_code, value

    with concurrent.futures.ThreadPoolExecutor(max_workers=30) as pool:
        results = [f.result() for f in [pool.submit(one, i) for i in range(30)]]

    wrong = {i: got for i, status, got in results
             if status != 200 or got != f"m{i}"}
    assert not wrong, (
        f"{len(wrong)} of 30 concurrent /headers requests echoed another "
        f"request's headers: {wrong}"
    )


def test_cookies_are_not_shared_between_clients(server):
    """One client's cookie jar must not become another's.

    ``/cookies/set`` writes a cookie and ``/cookies`` reads it back, so cookies
    parked in application state instead of travelling on the wire show up as one
    client seeing another's value.
    """
    def one(i):
        with hclient.make_client(timeout=90.0) as c:
            c.get(server.base_url + f"/cookies/set?who={i}")
            seen = c.get(server.base_url + "/cookies").json().get("cookies", {})
            return i, seen

    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        results = [f.result() for f in [pool.submit(one, i) for i in range(12)]]

    wrong = {i: seen for i, seen in results if seen.get("who") != str(i)}
    assert not wrong, (
        f"{len(wrong)} of 12 clients read back a cookie value that was not "
        f"their own, so cookie state is being kept on the server: {wrong}"
    )


# ---------------------------------------------------------------------------
# Order independence
# ---------------------------------------------------------------------------

SHUFFLE_SAMPLE = 80


@pytest.fixture(scope="module")
def shuffled_answers(server):
    """Replay a sample of the corpus in a deliberately different order."""
    sample = [c for c in REPEATABLE]
    random.Random(20260729).shuffle(sample)
    sample = sample[:SHUFFLE_SAMPLE]
    out = {}
    for case in sample:
        try:
            out[case["id"]] = ("ok", _send_alone(case, server))
        except Exception as exc:
            out[case["id"]] = ("error", f"{type(exc).__name__}: {exc}")
    return out


def test_answers_do_not_depend_on_corpus_order(shuffled_answers, two_passes):
    """A shuffled schedule must produce the same answers as corpus order.

    An implementation that advances a counter per request, or that returns the
    n-th canned response, passes the corpus in order and fails here.
    """
    mismatched = {}
    for cid, (kind, got) in shuffled_answers.items():
        want_kind, want = two_passes[0][cid]
        if want_kind != "ok":
            continue
        if kind != "ok":
            mismatched[cid] = got
            continue
        if want != got:
            mismatched[cid] = (
                f"status {want[0]} -> {got[0]}; "
                f"bodies {'match' if want[2] == got[2] else 'differ'}"
            )
    assert not mismatched, (
        f"{len(mismatched)} of {len(shuffled_answers)} cases answered "
        f"differently when the corpus was played in a different order:\n"
        f"{json.dumps(mismatched, indent=2)[:2000]}"
    )


def test_server_survives_the_whole_suite(server, http):
    """After everything above, the server is still the same healthy process.

    A submission that leaks file descriptors, tasks or memory per request, or
    that wedges its event loop under load, fails here even though every
    individual response was correct.
    """
    assert server.proc.poll() is None, (
        f"the server process exited during the suite with code "
        f"{server.proc.returncode}\n{server.log_tail()}"
    )
    resp = http.get(server.base_url + "/get")
    assert resp.status_code == 200, (
        f"/get answered {resp.status_code} after the suite had run; the server "
        f"degraded under load\n{server.log_tail()}"
    )
    assert resp.json()["url"].endswith("/get")
