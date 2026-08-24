"""Rebuilding a reply from SSE, and the two ways a stream can lie about being one.

The Anthropic path streams, and not for latency.  A non-streaming ``/v1/messages``
sends no bytes until the whole completion exists; the gateway sits behind
Cloudflare, which invents a 524 once the origin has been silent for ~100s.  A
reasoning model asked for up to 128k tokens is over that on nearly every adversary
turn, so the round ended ``outcome=error`` and scoring discarded the entire stage --
eighteen rounds on this campaign, 199s-1792s and up to 109 tool calls in.  Retrying
could not fix it: five attempts at a request that structurally cannot answer in 100s
are five 524s, which is how build03/none and build03/low lost a06 again after 520-524
were added to RETRYABLE_STATUS.  Measured on the endpoint with one 8000-token ask:
unstreamed, first byte at 90.7s; streamed, first byte at 1.3s and a clean 131.7s
finish -- the same work failing only for want of an early byte.

That fix shipped, and this file did not exist: it was verified by hand against the
live endpoint and left uncovered, while ``_post_sse`` reassembles a wire format with
several ways to be subtly wrong.  The dangerous one is not a crash.  A stream that
stops early looks exactly like a short answer, and a truncated adversary verdict
scored as a complete one is a defect the benchmark reports as the model's.

Asserted against literal SSE bytes rather than a mocked parser, because the format is
the part that can drift: a fixture that emitted already-parsed events would agree with
any reassembly, including one that never saw a ``data:`` line.
"""
from __future__ import annotations

import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from swerefactor import models  # noqa: E402

ASK = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]


def sse(*events: tuple[str, dict]) -> bytes:
    """The wire form: an ``event:`` name, a ``data:`` JSON line, a blank line."""
    out = []
    for name, payload in events:
        out.append(f"event: {name}\ndata: {json.dumps(payload)}\n\n")
    return "".join(out).encode("utf-8")


def text_stream(text: str = "ok", **usage) -> bytes:
    """A minimal well-formed reply carrying one text block."""
    return sse(
        ("message_start", {"type": "message_start",
                           "message": {"usage": {"input_tokens": 11, **usage}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": text}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta",
                           "delta": {"stop_reason": "end_turn"},
                           "usage": {"output_tokens": 3}}),
        ("message_stop", {"type": "message_stop"}),
    )


@pytest.fixture()
def wire(monkeypatch):
    """Serve fixed bytes as the response body, and record the request.

    ``urlopen`` is replaced rather than ``_post_sse``, so the parser under test runs
    on the bytes.  The response is iterated line-by-line by the parser, which is what
    a real ``http.client`` response supports -- ``io.BytesIO`` iterates the same way.
    """
    seen: dict = {}

    def install(body: bytes) -> dict:
        def fake(req, timeout=None):
            seen["url"] = req.full_url
            seen["headers"] = {k.lower(): v for k, v in req.headers.items()}
            seen["payload"] = json.loads(req.data.decode("utf-8"))
            resp = io.BytesIO(body)
            resp.__enter__ = lambda: resp          # used as a context manager
            resp.__exit__ = lambda *a: None
            return resp
        monkeypatch.setattr(models.urllib.request, "urlopen", fake)
        return seen

    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    return install


def test_a_streamed_reply_parses_into_the_non_streaming_shape(wire):
    """One parser for both transports, so callers cannot tell which one ran.

    ``_post_sse`` exists to return what ``_post`` returns.  If it did not, the block
    handling in ``AnthropicDriver._once`` would need a second branch, and the two
    would drift the way the host and image copies of the loop script did.
    """
    wire(text_stream("hello"))
    drv = models.build("anthropic", "m")
    turn = drv.complete("s", ASK)
    assert turn.text == "hello"
    assert turn.stop_reason == "end_turn"
    assert drv.usage()["input_tokens"] == 11


def test_the_request_says_stream_and_asks_for_the_stream_media_type(wire):
    """The whole point of the change, asserted on the outgoing request.

    A payload without ``stream`` gets one JSON object on a single ``data:`` line and
    no ``message_stop``, which this parser reports as an incomplete answer -- a
    message naming nothing about the real cause.  So it is forced in ``_post_sse``
    rather than trusted from the caller, and that is what this checks.
    """
    seen = wire(text_stream())
    models.build("anthropic", "m").complete("s", ASK)
    assert seen["payload"]["stream"] is True
    assert seen["headers"]["accept"] == "text/event-stream"
    assert seen["url"].endswith("/v1/messages")


def test_tool_arguments_are_rebuilt_from_their_fragments(wire):
    """``input_json_delta`` arrives in pieces that are only parseable once joined.

    Each fragment is invalid JSON on its own, so a parser that tried to decode them
    as they came would drop every tool call -- and an adversary round whose tool calls
    vanish reads as a model that chose not to investigate.
    """
    wire(sse(
        ("message_start", {"type": "message_start", "message": {"usage": {}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "tool_use", "id": "c1",
                                                   "name": "run", "input": {}}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "input_json_delta",
                                           "partial_json": '{"cmd": "ls'}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "input_json_delta",
                                           "partial_json": ' -la"}'}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta",
                           "delta": {"stop_reason": "tool_use"}, "usage": {}}),
        ("message_stop", {"type": "message_stop"}),
    ))
    turn = models.build("anthropic", "m").complete("s", ASK)
    assert [(c.name, c.arguments) for c in turn.tool_calls] == \
        [("run", {"cmd": "ls -la"})]


def test_unparseable_tool_arguments_fail_rather_than_becoming_empty(wire):
    """``{}`` is a claim: "the tool was called with no arguments".

    Defaulting to it on a decode failure would hand the round a call it can execute
    -- with every argument silently dropped -- and the transcript would record the
    model as having asked for exactly that.  Retryable, because a mangled stream is
    an absent measurement rather than a negative one.
    """
    wire(sse(
        ("message_start", {"type": "message_start", "message": {"usage": {}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "tool_use", "id": "c1",
                                                   "name": "run", "input": {}}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "input_json_delta",
                                           "partial_json": '{"cmd": "ls'}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_stop", {"type": "message_stop"}),
    ))
    drv = models.build("anthropic", "m", retries=0)
    with pytest.raises(models.ModelError) as exc:
        drv.complete("s", ASK)
    assert "not valid JSON" in str(exc.value)
    assert exc.value.retryable is True


def test_a_stream_that_stops_early_is_not_a_short_answer(wire):
    """The one failure that would be scored rather than reported.

    Everything arrived except ``message_stop``.  Treated as finished, this is a
    half-written adversary verdict presented to the adjudicator as the model's
    conclusion; the round would be graded, and nothing downstream could tell.
    """
    truncated = text_stream("half an ans")
    truncated = truncated[:truncated.rindex(b"event: message_stop")]
    wire(truncated)
    drv = models.build("anthropic", "m", retries=0)
    with pytest.raises(models.ModelError) as exc:
        drv.complete("s", ASK)
    assert "without message_stop" in str(exc.value)
    assert exc.value.retryable is True


def test_an_error_event_mid_stream_is_raised_not_parsed(wire):
    """A 200 that carries its failure in the body.

    The status line is already sent by the time the origin fails, so the error
    arrives as an event.  Reading it as content would score the endpoint's error
    message as an answer.
    """
    wire(sse(
        ("message_start", {"type": "message_start", "message": {"usage": {}}}),
        ("error", {"type": "error", "error": {"type": "overloaded_error",
                                              "message": "upstream is busy"}}),
    ))
    drv = models.build("anthropic", "m", retries=0)
    with pytest.raises(models.ModelError) as exc:
        drv.complete("s", ASK)
    assert "overloaded_error" in str(exc.value)
    assert exc.value.retryable is True


def test_the_streaming_path_classifies_a_524_the_way_the_other_one_does(wire,
                                                                       monkeypatch):
    """The status that caused all of this must still be retryable through here.

    ``_post_sse`` repeats ``_post``'s ``HTTPError`` handling, and a copy is a place a
    policy can drift: both call ``_is_retryable``, and this asserts the streaming copy
    actually does.  524 is Cloudflare's origin timeout -- the same event as the 504
    the vendor would have sent, under a different number because a different hop
    reported it.
    """
    def boom(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 524, "origin timeout", {},
                                     io.BytesIO(b"origin did not respond"))
    monkeypatch.setattr(models.urllib.request, "urlopen", boom)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    drv = models.build("anthropic", "m", retries=0)
    with pytest.raises(models.ModelError) as exc:
        drv.complete("s", ASK)
    assert exc.value.status == 524
    assert exc.value.retryable is True


def test_a_context_overflow_stays_fatal_through_the_streaming_path(wire,
                                                                  monkeypatch):
    """And the veto must survive here too, for the same reason.

    sub2api wraps upstream failures as ``502``, which is retryable on purpose -- so
    the one failure guaranteed to repeat arrives wearing the status reserved for
    failures guaranteed not to.  lang07/high's a03 retried a byte-identical
    over-window payload six times, ~841s of backoff, then failed anyway.  Sleeping
    does not shrink a prompt.
    """
    body = b'{"type":"upstream_error","message":"Your input exceeds the context ' \
           b'window of this model"}'

    def boom(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 502, "bad gateway", {},
                                     io.BytesIO(body))
    monkeypatch.setattr(models.urllib.request, "urlopen", boom)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    drv = models.build("anthropic", "m", retries=0)
    with pytest.raises(models.ModelError) as exc:
        drv.complete("s", ASK)
    assert exc.value.retryable is False, \
        "a 502 whose body is a context overflow must not be retried"
