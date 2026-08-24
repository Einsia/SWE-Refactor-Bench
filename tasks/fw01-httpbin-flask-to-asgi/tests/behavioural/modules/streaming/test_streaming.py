"""Streaming, framing and timing -- behaviours a captured response cannot hold.

The corpus records complete responses, so it cannot tell whether the bytes
arrived incrementally or all at once, and normalize.py deliberately ignores
Content-Length everywhere (uvicorn and Werkzeug frame differently, and
instruction.md puts framing out of contract *in general*). But the split between
streamed and buffered endpoints is not framing noise, it is httpbin behaviour:

*   ``/stream/N`` and ``/stream-bytes/N`` stream, and therefore cannot declare a
    length;
*   ``/drip`` streams but sets ``Content-Length`` by hand, so clients can show a
    progress bar;
*   ``/bytes/N`` is buffered and declares its length.

A port that collects every generator into one ``bytes`` before responding
produces identical bodies and would sail through the corpus tests. It is still
the wrong behaviour, and these tests are what notice.
"""

from __future__ import annotations

import json
import time

import pytest

pytestmark = [pytest.mark.behaviour]

CHUNKED_PATHS = [
    "/stream/1", "/stream/2", "/stream/5", "/stream/10", "/stream/100",
    "/stream-bytes/1?seed=1", "/stream-bytes/128?seed=1",
    "/stream-bytes/1024?seed=2", "/stream-bytes/10240?seed=3",
    "/stream-bytes/100?seed=1&chunk_size=7",
]


@pytest.mark.parametrize("path", CHUNKED_PATHS)
def test_streaming_endpoints_do_not_declare_a_length(path, http, base_url):
    r = http.get(base_url + path)
    assert r.status_code == 200
    assert "content-length" not in r.headers, (
        f"{path} declared Content-Length: {r.headers['content-length']}. "
        f"State A streams this route, so the length is not known when the "
        f"headers are sent -- declaring one means the body was buffered first."
    )


SIZED_PATHS = [
    ("/bytes/16?seed=1", 16),
    ("/bytes/1024?seed=7", 1024),
    ("/drip?duration=0&numbytes=10", 10),
    ("/drip?duration=0&numbytes=5&code=418", 5),
]


@pytest.mark.parametrize("path,length", SIZED_PATHS)
def test_sized_endpoints_declare_their_length(path, length, http, base_url):
    r = http.get(base_url + path)
    assert r.headers.get("content-length") == str(length), (
        f"{path} must declare Content-Length: {length}, got "
        f"{r.headers.get('content-length')!r}"
    )
    assert len(r.content) == length


# ---------------------------------------------------------------------------
# Incremental delivery
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_drip_delivers_progressively(http, base_url):
    """``/drip`` must spread its bytes over the requested duration.

    The assertion is on how long the whole response took, not on the gap between
    the first and last chunk. Both catch a buffered implementation, but the gap
    is measured on the *client's* side of a socket, so anything that coalesces
    chunks -- a proxy, a client read buffer, a scheduling hiccup on a busy runner
    -- shrinks it without the server having done anything wrong. Total elapsed
    time cannot be coalesced away: a server that ignores ``duration`` answers in
    milliseconds, and one that honours it cannot finish before it has waited.

    The gap is still measured, and still asserted, but only when more than one
    chunk actually arrived separately -- which is the only case where it means
    anything.
    """
    marks = []
    started = time.monotonic()
    with http.stream("GET", base_url + "/drip?duration=3&numbytes=6") as r:
        assert r.status_code == 200
        seen = 0
        for chunk in r.iter_bytes():
            if not chunk:
                continue
            marks.append(time.monotonic() - started)
            seen += len(chunk)
    elapsed = time.monotonic() - started
    assert seen == 6, f"/drip?numbytes=6 delivered {seen} bytes"
    assert marks, "/drip produced no bytes"
    assert elapsed > 1.0, (
        f"/drip?duration=3 finished in {elapsed:.2f}s; it cannot have spread 6 "
        f"bytes over 3 seconds, so the duration parameter is being ignored"
    )
    if len(marks) > 1:
        assert marks[-1] - marks[0] > 0.5, (
            f"/drip?duration=3 took {elapsed:.2f}s but delivered every byte "
            f"within {marks[-1] - marks[0]:.2f}s at the end; the payload is "
            f"being built up and flushed once instead of dripped"
        )


@pytest.mark.slow
def test_drip_initial_delay_is_honoured(http, base_url):
    started = time.monotonic()
    with http.stream("GET",
                     base_url + "/drip?duration=0&numbytes=2&delay=2") as r:
        first = None
        for chunk in r.iter_bytes():
            if chunk and first is None:
                first = time.monotonic() - started
    assert first is not None, "/drip?delay=2 produced no bytes"
    assert first >= 1.5, (
        f"/drip?delay=2 produced its first byte after {first:.2f}s; the initial "
        f"delay is not being applied"
    )


@pytest.mark.slow
def test_stream_delivers_line_by_line(http, base_url):
    """``/stream/N`` emits N newline-delimited JSON objects, each complete."""
    seen = []
    with http.stream("GET", base_url + "/stream/5") as r:
        assert r.status_code == 200
        for line in r.iter_lines():
            if line.strip():
                seen.append(json.loads(line))
    assert len(seen) == 5, f"/stream/5 produced {len(seen)} objects"
    assert [o["id"] for o in seen] == [0, 1, 2, 3, 4], (
        f"/stream/5 ids are {[o.get('id') for o in seen]}, expected 0..4"
    )
    for obj in seen:
        assert {"url", "args", "headers", "origin", "id"} <= set(obj), (
            f"a streamed object is missing keys: {sorted(obj)}"
        )


@pytest.mark.slow
def test_range_duration_spreads_the_response(http, base_url):
    """``/range/N?duration=`` paces its chunks the same way.

    Timed on total elapsed rather than the client-observed gap, for the reason
    given in ``test_drip_delivers_progressively``.
    """
    marks = []
    started = time.monotonic()
    with http.stream("GET",
                     base_url + "/range/100?duration=3&chunk_size=10") as r:
        assert r.status_code == 200
        total = 0
        for chunk in r.iter_bytes():
            if not chunk:
                continue
            marks.append(time.monotonic() - started)
            total += len(chunk)
    elapsed = time.monotonic() - started
    assert total == 100
    assert elapsed > 1.0, (
        f"/range/100?duration=3 finished in {elapsed:.2f}s; the duration "
        f"parameter is being ignored"
    )
    if len(marks) > 1:
        assert marks[-1] - marks[0] > 0.5, (
            f"/range/100?duration=3 took {elapsed:.2f}s but delivered every "
            f"chunk within {marks[-1] - marks[0]:.2f}s at the end; the response "
            f"is being buffered instead of paced"
        )


# ---------------------------------------------------------------------------
# /delay
# ---------------------------------------------------------------------------

@pytest.mark.slow
@pytest.mark.parametrize("seconds", [1, 2])
def test_delay_waits(seconds, http, base_url):
    started = time.monotonic()
    r = http.get(base_url + f"/delay/{seconds}")
    elapsed = time.monotonic() - started
    assert r.status_code == 200
    assert elapsed >= seconds * 0.85, (
        f"/delay/{seconds} answered in {elapsed:.2f}s; the delay is not applied"
    )
    assert elapsed < seconds + 8, (
        f"/delay/{seconds} took {elapsed:.2f}s, far longer than requested"
    )
    body = r.json()
    assert {"url", "args", "form", "data", "origin", "headers", "files"} <= set(body)


@pytest.mark.slow
def test_delay_is_clamped_to_ten_seconds(http, base_url):
    """``/delay/999`` clamps to 10s rather than hanging forever."""
    started = time.monotonic()
    r = http.get(base_url + "/delay/999", timeout=40)
    elapsed = time.monotonic() - started
    assert r.status_code == 200
    # The clamp is 10s and the unclamped alternative is 999s, so the ceiling only
    # has to sit between those two. 25 does, with room for a loaded runner.
    assert elapsed < 25, (
        f"/delay/999 took {elapsed:.1f}s; State A clamps the delay to 10s"
    )
    assert elapsed >= 9, (
        f"/delay/999 answered in {elapsed:.1f}s; State A sleeps the full "
        f"clamped 10s"
    )


# ---------------------------------------------------------------------------
# Large payload audit
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("n", [100_000, 102_400])
def test_large_stream_bytes_are_complete(n, http, base_url):
    """A chunked body must arrive whole, with no truncation at a chunk edge."""
    r = http.get(base_url + f"/stream-bytes/{n}?seed=1")
    assert r.status_code == 200
    expected = min(n, 100 * 1024)
    assert len(r.content) == expected, (
        f"/stream-bytes/{n} delivered {len(r.content)}B, expected {expected}B"
    )


def test_stream_bytes_matches_bytes_for_the_same_seed(http, base_url):
    """The two routes share one seeded generator, so they must agree."""
    for seed in (1, 2, 42):
        buffered = http.get(base_url + f"/bytes/256?seed={seed}").content
        streamed = http.get(base_url + f"/stream-bytes/256?seed={seed}").content
        assert buffered == streamed, (
            f"/bytes/256?seed={seed} and /stream-bytes/256?seed={seed} produced "
            f"different bytes; both must use the same seeded generator"
        )


def test_seeded_bytes_are_reproducible(http, base_url):
    """The same seed must give the same bytes on every request."""
    a = http.get(base_url + "/bytes/512?seed=12345").content
    b = http.get(base_url + "/bytes/512?seed=12345").content
    assert a == b, "/bytes is not deterministic for a fixed seed"
    c = http.get(base_url + "/bytes/512?seed=12346").content
    assert a != c, "/bytes returns the same bytes for different seeds"


def test_unseeded_bytes_vary(http, base_url):
    """Without a seed the payload must be random, not a frozen constant."""
    seen = {http.get(base_url + "/bytes/64").content for _ in range(4)}
    assert len(seen) > 1, (
        "/bytes/64 without a seed returned identical bytes every time; the "
        "generator must be seeded from the clock when no seed is given"
    )
