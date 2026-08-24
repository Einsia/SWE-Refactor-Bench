"""Reasoning effort must reach the endpoint as the name configuration gave.

These tests assert on the outgoing request body, not on the driver's fields,
because that is where the bug they exist to prevent lived.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from swerefactor import models  # noqa: E402

ASK = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]


@pytest.fixture()
def sent(monkeypatch):
    """Capture the one request a driver would post, instead of posting it."""
    seen: list[dict] = []

    def stub(self, url, headers, payload):
        seen.append(payload)
        raise models.ModelError("stub: not sent")

    # Both transports, because the subject here is the body and not the way it
    # travels.  The Anthropic path posts through `_post_sse` -- so a reasoning turn
    # sends its first byte before Cloudflare invents a 524 at ~100s -- and the
    # OpenAI path through `_post`.  Stubbing one leaves the other reaching the real
    # `urlopen`, which fails with whatever the network says and never names the
    # effort dial these tests exist to guard.
    monkeypatch.setattr(models.Driver, "_post", stub)
    monkeypatch.setattr(models.Driver, "_post_sse", stub)
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    return seen


def body(driver: str, effort: str, sent: list[dict]) -> dict:
    drv = models.build(driver, "m", options={"reasoning_effort": effort})
    with pytest.raises(models.ModelError):
        drv._once("s", ASK, None)
    assert sent, "driver posted nothing"
    return sent[0]


@pytest.mark.parametrize("effort", models.EFFORT_LEVELS)
def test_openai_sends_the_name_it_was_given(effort, sent):
    assert body("openai", effort, sent)["reasoning_effort"] == effort


@pytest.mark.parametrize("effort", models.EFFORT_LEVELS)
def test_anthropic_sends_the_name_it_was_given(effort, sent):
    out = body("anthropic", effort, sent)
    assert out["output_config"] == {"effort": effort}
    # The legacy shape is a 400 on every model stage 3 uses.  It must be gone,
    # not merely unused: a driver that sends both would fail the same way.
    assert "thinking" not in out


def test_adjacent_levels_are_different_requests(sent):
    """`xhigh` and `max` collapsed into one budget once.  They must not again."""
    for driver in ("openai", "anthropic"):
        sent.clear()
        high = body(driver, "xhigh", sent)
        sent.clear()
        top = body(driver, "max", sent)
        assert high != top, f"{driver}: xhigh and max sent the same body"


def test_an_unknown_effort_fails_before_the_request(sent):
    """A typo must not reach the endpoint, where it would 400 mid-run -- nor be
    dropped, which would run the stage at the endpoint's default and score it."""
    for driver in ("openai", "anthropic"):
        drv = models.build(driver, "m", options={"reasoning_effort": "ultra"})
        with pytest.raises(models.ModelError, match="unknown reasoning_effort"):
            drv._once("s", ASK, None)
        assert not sent, f"{driver}: posted despite an unknown effort"


def test_a_hand_written_output_config_wins(sent):
    """Someone who wrote the vendor field by hand meant it."""
    drv = models.build("anthropic", "m", options={
        "reasoning_effort": "low", "output_config": {"effort": "max"}})
    with pytest.raises(models.ModelError):
        drv._once("s", ASK, None)
    assert sent[0]["output_config"] == {"effort": "max"}


def test_usage_records_the_effort_it_ran_at(sent):
    """A result file that names only the model under-describes the run.

    Two stages graded at different efforts are different measurements, so the
    effort belongs in the artifact alongside the model -- otherwise the only
    proof of what ran is a test like this one, and none of it survives into the
    scored output where a reader would look for it.
    """
    for driver in ("openai", "anthropic"):
        drv = models.build(driver, "m", options={"reasoning_effort": "xhigh"})
        assert drv.usage()["reasoning_effort"] == "xhigh"


def test_usage_omits_an_effort_that_was_never_set(sent):
    """Absent must read as absent: inventing a default here would report an
    effort the request never carried."""
    drv = models.build("openai", "m")
    assert "reasoning_effort" not in drv.usage()


@pytest.fixture()
def answered(monkeypatch):
    """Let a driver complete, with the endpoint's reply under the test's control."""
    reply: dict = {}

    def stub(self, url, headers, payload):
        return reply

    # Both, for the reason given on `sent` above.  These two tests are about what
    # the driver does with a reply, and `_post_sse` returns the same shape a
    # non-streaming call does precisely so that this stays one question: the SSE
    # reassembly is covered in test_stream.py, against the wire format.
    monkeypatch.setattr(models.Driver, "_post", stub)
    monkeypatch.setattr(models.Driver, "_post_sse", stub)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    return reply


def test_usage_records_the_credits_the_endpoint_charged(answered):
    """Token counts are not always a measurement, so the billing unit is kept.

    One graded round reported 8k input tokens across eighty turns whose
    transcript could not have been sent in under about 600k -- the gateway counts
    what it forwards, not what was sent.  Where the endpoint states a cost in its
    own unit, that figure is the only honest one available, and dropping it left
    the Claude rounds with no reportable cost at all.
    """
    answered.update({"content": [{"type": "text", "text": "ok"}],
                     "stop_reason": "end_turn",
                     "usage": {"input_tokens": 11, "output_tokens": 3,
                               "credit_usage": 0.25}})
    drv = models.build("anthropic", "m")
    drv.complete("s", ASK)
    drv.complete("s", ASK)
    assert drv.usage()["credits"] == 0.5, "credits must accumulate across calls"


def test_usage_omits_credits_an_endpoint_never_charged(answered):
    """A token-billed endpoint must not be reported as having cost zero credits:
    silence and free are different claims."""
    answered.update({"content": [{"type": "text", "text": "ok"}],
                     "stop_reason": "end_turn",
                     "usage": {"input_tokens": 11, "output_tokens": 3}})
    drv = models.build("anthropic", "m")
    drv.complete("s", ASK)
    assert "credits" not in drv.usage()


def test_the_token_cap_is_not_the_effort_dial(sent):
    """Effort must not move `max_tokens`: it is a ceiling, and one that is high
    enough to not bind, since thinking counts against it."""
    caps = set()
    for effort in models.EFFORT_LEVELS:
        sent.clear()
        caps.add(body("anthropic", effort, sent)["max_tokens"])
    assert caps == {models.DEFAULT_MAX_TOKENS}
    assert models.DEFAULT_MAX_TOKENS >= 64000
