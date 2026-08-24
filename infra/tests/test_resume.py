"""A provider outage ended a run.  Now it re-sends the turn.

``models.Driver`` retries a request four times and then raises;
``agentloop.run`` turned that into ``outcome=error``, discarding however much of
the hour the model had already spent reading -- and a run that ended that way was
scored as though the model had failed the task.  It now re-sends the same
conversation, up to a bounded number of times, within the same wall-clock budget.

The property tested hardest is the one whose absence is silent.  A loop that
re-sends a *non*-retryable failure turns a bad API key into six sleeps and an
outcome of ``time-limit`` -- the budget named as the cause of a run that never had
a working endpoint -- and that passes any test which only counts resumes on the
retryable case.  So the non-retryable path is asserted on the attempt count and on
the absence of a sleep, not on the outcome alone.
"""

from __future__ import annotations

import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from swerefactor import agentloop, models
from swerefactor.models import ModelError, ToolCall, ToolSpec, Turn
from swerefactor.tools import Toolbox

# --------------------------------------------------------------------------- #
# a retryable outage resumes the run instead of ending it
# --------------------------------------------------------------------------- #

SUBMIT = ToolSpec(name="submit", description="finish", schema={"type": "object"})


class _Flaky(models.Driver):
    """Fails ``fails`` times with ``exc``, then submits.

    ``retries=0``: this stands in for a driver whose own retry budget is already
    spent, which is the only situation the loop's resume is for.  Subclassing the
    real Driver rather than duck-typing keeps ``usage()`` honest, since the loop
    records it.
    """

    name = "flaky"

    def __init__(self, fails: int, exc: ModelError) -> None:
        super().__init__("flaky-model", retries=0)
        self.fails = fails
        self.exc = exc
        self.attempts = 0
        self.seen_messages: list[int] = []

    def _once(self, system, messages, tools):
        self.attempts += 1
        self.seen_messages.append(len(messages))
        if self.attempts <= self.fails:
            raise self.exc
        return Turn(text="done", stop_reason="tool_use",
                    tool_calls=[ToolCall(id="c1", name="submit",
                                         arguments={"ok": True})])


def _run(driver: models.Driver, tmp_path: Path, **over) -> agentloop.LoopResult:
    kwargs = dict(transcript=tmp_path / "t.jsonl", budget_sec=60.0)
    kwargs.update(over)
    return agentloop.run(driver, "sys", "go", Toolbox(roots={}), SUBMIT, **kwargs)


@pytest.fixture(autouse=True)
def _no_sleeping(monkeypatch):
    """The backoff is real seconds; the test asserts on the record, not the wait."""
    slept: list[float] = []
    monkeypatch.setattr(agentloop.time, "sleep", slept.append)
    return slept


def test_a_retryable_outage_resumes_and_the_run_submits(tmp_path, _no_sleeping):
    """The property the change exists for: 503 in turn one is not a zero."""
    driver = _Flaky(2, ModelError("HTTP 503", retryable=True, status=503))
    res = _run(driver, tmp_path)
    assert res.outcome == agentloop.SUBMITTED
    assert res.payload == {"ok": True}
    assert res.resumes == 2
    assert driver.attempts == 3
    assert len(_no_sleeping) == 2


def test_a_resume_does_not_consume_a_turn(tmp_path):
    """Two failed sends and one answer is turn 1, not turn 3.

    A resume that charged the counter would let an outage spend a capped run's
    turns without the model ever having answered, and the run would then be
    reported as ``turn-limit`` -- an outcome that reads as a model which would not
    finish.
    """
    driver = _Flaky(2, ModelError("HTTP 503", retryable=True, status=503))
    res = _run(driver, tmp_path, max_turns=1)
    assert res.outcome == agentloop.SUBMITTED
    assert res.turns == 1


def test_a_resume_re_sends_the_same_conversation(tmp_path):
    """It resumes rather than restarts.

    The failing call appended nothing to ``messages``, so the model gets back the
    conversation it had -- including everything it had already read.  Asserted on
    the message count each attempt saw: a loop that rebuilt the opening, or one
    that appended an error turn, would not give three identical numbers.
    """
    driver = _Flaky(2, ModelError("HTTP 503", retryable=True, status=503))
    _run(driver, tmp_path)
    assert driver.seen_messages == [1, 1, 1]


def test_a_non_retryable_error_still_ends_the_run(tmp_path, _no_sleeping):
    """A bad key fails identically however long it is left.

    Resuming these is the failure mode worth guarding: a wrong credential would
    become six sleeps and an outcome of ``time-limit``, which names the budget as
    the cause of a run that never had a working endpoint.
    """
    driver = _Flaky(99, ModelError("HTTP 401 invalid api key", retryable=False))
    res = _run(driver, tmp_path)
    assert res.outcome == agentloop.ERROR
    assert "401" in res.error
    assert res.resumes == 0
    assert driver.attempts == 1
    assert _no_sleeping == []


def test_an_outage_that_outlasts_the_resumes_is_still_an_error(tmp_path):
    """Bounded.  The error reported is the last one, not a time limit."""
    driver = _Flaky(99, ModelError("HTTP 503", retryable=True, status=503))
    res = _run(driver, tmp_path, max_resumes=3)
    assert res.outcome == agentloop.ERROR
    assert "503" in res.error
    assert res.resumes == 3
    assert driver.attempts == 4


@pytest.mark.parametrize("bound", [0, -1])
def test_no_resume_budget_is_the_old_behaviour(tmp_path, bound):
    """The escape hatch, pinned so it keeps meaning what it says.

    ``-1`` is here because it is the value that fails silently rather than
    loudly: unclamped it empties the attempt loop, so the request is never sent
    and the run reports ``abandoned`` -- a model recorded as having given up
    without one call having been made.
    """
    driver = _Flaky(1, ModelError("HTTP 503", retryable=True, status=503))
    res = _run(driver, tmp_path, max_resumes=bound)
    assert res.outcome == agentloop.ERROR
    assert res.resumes == 0
    assert driver.attempts == 1


def test_a_resume_is_not_attempted_with_no_budget_left(tmp_path, _no_sleeping):
    """The sleep must not be what ends the run.

    With seconds left, a four-minute backoff plus one request runs past the
    budget, and the run would be reported ``time-limit`` -- the budget named as
    the cause of an outage.  ``error`` naming the 503 is the honest outcome.
    """
    driver = _Flaky(99, ModelError("HTTP 503", retryable=True, status=503))
    res = _run(driver, tmp_path, budget_sec=agentloop.RESUME_MIN_BUDGET_SEC / 2)
    assert res.outcome == agentloop.ERROR
    assert "503" in res.error
    assert res.resumes == 0
    assert _no_sleeping == []


def test_each_resume_is_written_to_the_transcript(tmp_path):
    """A run that stalled is not the same evidence as one that did not.

    Both submit, and without a record the only difference is an elapsed time that
    a reader would attribute to the model thinking.
    """
    driver = _Flaky(2, ModelError("HTTP 503", retryable=True, status=503))
    res = _run(driver, tmp_path)
    lines = [json.loads(line) for line
             in (tmp_path / "t.jsonl").read_text(encoding="utf-8").splitlines()]
    resumes = [r for r in lines if r["kind"] == "resume"]
    assert len(resumes) == 2
    assert [r["attempt"] for r in resumes] == [1, 2]
    assert all(r["turn"] == 1 for r in resumes)
    assert all("503" in r["message"] for r in resumes)

    start = next(r for r in lines if r["kind"] == "start")
    assert start["max_resumes"] == agentloop.DEFAULT_MAX_RESUMES
    end = next(r for r in lines if r["kind"] == "end")
    assert end["resumes"] == 2
    assert res.to_dict()["resumes"] == 2
    # No `error` record: nothing about the run failed, and `error` is read as
    # evidence of how a run ended.
    assert not [r for r in lines if r["kind"] == "error"]


def test_a_clean_run_publishes_resumes_zero(tmp_path):
    """Zero is stated, not left out.

    An absent key would mean both "this run never stalled" and "written by a
    harness with no resume loop", and only the first is a fact about the run.
    """
    driver = _Flaky(0, ModelError("unused"))
    res = _run(driver, tmp_path)
    assert res.outcome == agentloop.SUBMITTED
    assert res.to_dict()["resumes"] == 0


def test_both_stages_carry_the_count_into_their_own_result(tmp_path):
    """The loop counting resumes is no use if no stage records them.

    ``docs/SCHEMA.md`` states that the count reaches the stage's own ``resumes``
    field, and a document that names a field is a claim about the code.  Both
    stages are checked because they consume ``LoopResult`` independently: stage 1
    copies six of its fields onto a ``Sample``, stage 3 copies nine onto a
    ``Round``, and a field added to one is not thereby in the other.
    """
    from swerefactor.verification import Round
    from swerefactor.audit import Sample

    assert Sample(index=1, outcome="submitted", resumes=3).to_dict()["resumes"] == 3
    assert Round(adversary="a1", model="m", outcome="survived",
                 resumes=3).to_dict()["resumes"] == 3


# --------------------------------------------------------------------------- #
# a proxy's own timeout is an outage, not a malformed request
# --------------------------------------------------------------------------- #
#
# Everything above is asserted on 503, which every list of retryable statuses
# contains.  The statuses that decide real runs are the ones a *proxy* invents
# when it cannot reach the model: Cloudflare answers 520-524 rather than 502/504,
# and a set enumerated from the vendor's documented errors will not have them.
#
# The cost is not a retry that did not happen.  A non-retryable ModelError is
# `harness_fault`, which ends the round at `outcome=error`, and scoring then
# discards the whole stage on the grounds that "an adversary that could not run
# has not demonstrated a defect" -- so one 524 in one of six rounds turns a graded
# run into an unmeasured one.  Eighteen rounds were lost that way on this
# campaign, at 199s-1792s each and up to 109 tool calls deep, every one with
# `resumes: 0`.


class _HTTPFailing(models.Driver):
    """Raises a real ``HTTPError`` from ``_post``, then submits.

    Goes through ``_post`` rather than raising ``ModelError`` directly, because
    the classification under test happens *there*: a test that constructed the
    ModelError itself would be asserting on its own ``retryable=`` argument.
    """

    name = "httpfailing"

    def __init__(self, code: int, fails: int = 1) -> None:
        super().__init__("proxied-model", retries=0)
        self.code = code
        self.fails = fails
        self.attempts = 0

    def _once(self, system, messages, tools):
        self.attempts += 1
        if self.attempts <= self.fails:
            self._post("https://proxy.example/v1/messages", {}, {"m": self.model})
        return Turn(text="done", stop_reason="tool_use",
                    tool_calls=[ToolCall(id="c1", name="submit",
                                         arguments={"ok": True})])


@pytest.fixture
def _http_error(monkeypatch):
    """Make ``_post``'s urlopen raise the status asked for.

    The body is a parameter because one status is classified on it: a 400 is not
    retryable on its code, and the filter refusal below is retried on its text.  A
    fixture that hardcoded a body could not tell the two 400s apart.
    """
    def install(code: int, body: bytes = b"origin did not respond") -> None:
        def boom(req, timeout=None):
            raise urllib.error.HTTPError(
                req.full_url, code, "proxy timeout", {}, io.BytesIO(body))
        monkeypatch.setattr(models.urllib.request, "urlopen", boom)
    return install


# 520 unknown, 521 origin down, 522 connection timed out, 523 origin
# unreachable, 524 origin timed out.  None of the five says anything about the
# request: each one says the proxy did not get an answer to pass back.
@pytest.mark.parametrize("code", [520, 521, 522, 523, 524])
def test_a_proxy_origin_timeout_is_retryable(code):
    """The classification, at the point where it is made."""
    assert code in models.RETRYABLE_STATUS, (
        f"HTTP {code} is a proxy reporting no answer from the origin, the same "
        f"outage 504 describes; leaving it out makes it non-retryable, which is "
        f"the classification reserved for a malformed request"
    )


def test_a_524_resumes_the_round_instead_of_ending_it(tmp_path, _no_sleeping,
                                                      _http_error):
    """End to end: the 524 that cost this campaign eighteen rounds.

    Asserted through ``_post`` and the loop together, since the defect needed
    both -- the status was not retryable, so neither the driver's four retries
    nor the loop's resumes fired, and ``resumes: 0`` was recorded on every one.
    """
    _http_error(524)
    driver = _HTTPFailing(524, fails=2)
    res = _run(driver, tmp_path)
    assert res.outcome == agentloop.SUBMITTED
    assert res.resumes == 2
    assert driver.attempts == 3
    assert not res.harness_fault


def test_a_400_from_the_same_proxy_still_ends_the_round(tmp_path, _no_sleeping,
                                                        _http_error):
    """The other half of the split, or the fix would retry a refusal.

    This campaign's other grading failure was a 400 whose body read "flagged for
    possible cybersecurity risk" -- a filter declining the request, identical on
    every attempt.  Retrying it would spend four backoffs and report the outage
    wording for a decision the endpoint had already made.
    """
    _http_error(400)
    driver = _HTTPFailing(400, fails=2)
    res = _run(driver, tmp_path)
    assert res.outcome == agentloop.ERROR
    assert "400" in res.error
    assert res.resumes == 0
    assert driver.attempts == 1
    assert _no_sleeping == []


# The second status that decided real rounds, and the one that looks least like a
# transient failure: HTTP 400.
#
# The upstream safety filter on the OpenAI path refuses some stage-3 prompts as
# "flagged for possible cybersecurity risk".  A 400 is the classification reserved
# for a malformed request, so the refusal ends the round at `outcome=error` and the
# stage is discarded -- the same cost as the 524, two rounds of pf03's a01.
#
# What makes it retryable is a measurement, not a reading of the vendor's docs: the
# same prompt, in the same round id, through the same driver, was refused twice and
# completed on a third attempt with nothing changed in between.  Intuition says a
# filter declining a request declines it identically on the fifth attempt, and that
# is not what the three attempts showed.
#
# The pair of tests below is the point.  Retrying every 400 would be the easy fix and
# it would hide authoring bugs: a genuinely malformed stage-3 request has to fail on
# the first attempt, loudly, not four backoffs later.  So one asserts the refusal is
# retried and the other asserts a plain 400 is still not.

FILTER_400 = (b'{"error":{"message":"This content was flagged for possible '
              b'cybersecurity risk. If this seems wrong, try rephrasing your '
              b'request.","type":"invalid_request_error"}}')


def test_a_filter_refusal_is_retried_though_it_is_a_400(tmp_path, _no_sleeping,
                                                       _http_error):
    """Measured stochastic, so recoverable: refused twice, then submits."""
    _http_error(400, FILTER_400)
    driver = _HTTPFailing(400, fails=2)
    res = _run(driver, tmp_path)
    assert res.outcome == agentloop.SUBMITTED, (
        "a filter refusal that a later attempt does not repeat must not end the "
        "round; this one was observed to clear on a third attempt"
    )
    assert res.resumes == 2
    assert driver.attempts == 3


def test_a_plain_400_still_ends_the_run(tmp_path, _no_sleeping, _http_error):
    """The other half of the rule: a malformed request must not be retried.

    Asserted on the attempt count, not the outcome.  A loop that retried this would
    still end at `error` after its four backoffs, so an outcome-only assertion passes
    on the defect and reports an authoring bug as an outage.
    """
    _http_error(400, b'{"error":{"message":"unknown parameter: reasoning_efort"}}')
    driver = _HTTPFailing(400, fails=99)
    res = _run(driver, tmp_path)
    assert res.outcome != agentloop.SUBMITTED
    assert driver.attempts == 1, (
        f"a malformed request was attempted {driver.attempts} times; it must fail "
        f"on the first, or an authoring bug reads as a provider outage"
    )
    assert res.resumes == 0


def test_the_refusal_signature_is_not_matched_by_a_bare_status():
    """`_body_retryable` is about the body, and only for 400."""
    sig = models.RETRYABLE_BODY[0]
    assert models._body_retryable(400, f'{{"message":"{sig}"}}')
    assert not models._body_retryable(400, '{"message":"unknown parameter"}')
    # Not a blanket "any status with this text": the signature exists to widen 400
    # alone, and a 401 carrying it is still an auth failure.
    assert not models._body_retryable(401, f'{{"message":"{sig}"}}')


def test_a_context_overflow_is_fatal_whatever_status_reports_it():
    """The veto, and its precedence over both other rules.

    sub2api wraps upstream failures as ``502``, which is retryable on purpose -- the
    524 lesson put it there -- so the one failure guaranteed to repeat arrives wearing
    the status reserved for failures guaranteed not to.  Measured on lang07/high's
    a03: turn 61 raised ``502 ... "Your input exceeds the context window of this
    model"``, and the loop re-sent a byte-identical payload six times over ~841s of
    backoff before failing anyway with 276s of round budget unspent.  Sleeping does
    not shrink a prompt.  The round is lost either way; it now ends honestly, and 14
    minutes sooner.

    Checked across all three dialects and against every retryable status, because the
    point is that the status is the wrong witness: whichever hop reported the failure
    chose the number, and the request's size is what makes it hopeless.
    """
    for phrase in models.FATAL_BODY:
        body = f'{{"type":"upstream_error","message":"... {phrase} ..."}}'
        for status in sorted(models.RETRYABLE_STATUS):
            assert not models._is_retryable(status, body), (
                f"{status} carrying {phrase!r} was called retryable; a request too "
                f"large for the window is too large on every attempt"
            )

    # A body with neither signature keeps the status's own verdict, so the veto has
    # not simply turned the retryable set off.
    assert models._is_retryable(502, '{"type":"upstream_error"}')

    # And the veto outranks the filter-refusal widening.  The two cannot collide
    # today -- one is about the request's size, the other about a safety verdict --
    # but the resolution is asserted rather than left to whichever `or` came first.
    both = f"{models.RETRYABLE_BODY[0]} / {models.FATAL_BODY[0]}"
    assert models._body_retryable(400, both), "precondition: rule 3 would fire"
    assert not models._is_retryable(400, both), (
        "a body matching both rules must not be retried: rule 1 exists because the "
        "hope rule 3 encodes is already known to be false"
    )
