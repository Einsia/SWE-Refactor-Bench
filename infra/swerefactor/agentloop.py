"""The conversation cycle both model-driven stages share.

Send the conversation, run whatever tools came back, append the results, repeat.
The audit verifier and the stage-3 adversaries differ in their prompt and in
the shape of their final answer, and in nothing else, so they share this.

Three decisions:

*Finishing is a tool call, not a paragraph.*  The model ends its run by calling
a submit tool with a typed payload.  Parsing a verdict out of prose means a score
can turn on a sentence written in passing, and "the migration looks incomplete"
appearing in a model's own reasoning is not a finding.

*The transcript is always written.*  A failed audit gate is a zero on a
hundred-point task, so the reasoning behind it has to be inspectable afterwards:
every turn, every tool call, every result, in order.  It is written as JSONL as
the run proceeds, so a run that dies still leaves the part it got through.

*A provider outage does not end the run.*  A retryable failure that survived the
driver's own retries re-sends the same turn, up to a bounded number of times, and
each resume is written to the transcript.  An hour of a model's investigation is
not thrown away because a gateway returned 503 in its fortieth minute -- and a
run that ended that way scored as though the model had failed the task, which is
the reading this prevents.  Non-retryable failures still end the run at once, and a
caller whose run is not hour-scale opts out with ``max_resumes=0`` -- the
adjudicator's scope ruling does, for the same reason it is the one caller that caps
turns.

*The budget is wall-clock; a turn cap exists only where a caller asks for one.*
The stages do not cap turns: a model that has read for ten turns without testing
anything is wasting its hour, and the way to stop that is to say so in the
prompt, not to cut the run off.  A caller may still pass a cap -- small internal
routines do -- and a model that runs one out is a different outcome from one
that looked and found nothing, which the scorer is told.
"""

from __future__ import annotations

import itertools
import json
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from .models import Driver, ModelError, ToolSpec, Turn
from .tools import Toolbox

DEFAULT_BUDGET_SEC = 3600.0

#: How much of each tool result the transcript keeps.  The model always receives
#: the whole thing; this bounds only the log, because a review that reads a 900-line
#: build file eleven times would otherwise write a transcript nobody opens.
#:
#: The cap is announced when it bites, for the same reason ``tools.py`` announces
#: its own: a clipped result that does not say it was clipped is worse than a short
#: one.  A ``fetch_source`` record whose header reads ``lines 1-902 of 902`` and
#: whose body stops at line 83 reads as a review that voted on 9% of the build
#: system -- an accusation against a review that in fact saw all of it, and the
#: transcript is the only place that accusation can be answered.
MAX_LOGGED_RESULT = 4_000

#: How many times one turn may be re-sent after a retryable model failure.
#:
#: `models.Driver.complete` already retries a request four times before it raises,
#: so this is the layer above: the request is gone, the provider is still down, and
#: the question is whether an hour of investigation ends here.  It resumes rather
#: than restarts -- the failing call appended nothing to `messages`, so the same
#: conversation is re-sent and the model does not lose the tree it had read.
#:
#: Only retryable failures.  A bad key or a rejected schema fails identically
#: however long it is left, and a loop that re-sent those would turn a
#: five-second configuration error into a ten-minute one and report a time limit
#: instead of the error that caused it.
#:
#: The turn counter is not consumed by a resume: a re-sent request that succeeds
#: is the same turn of the conversation, and charging it would let a bad afternoon
#: on the provider's side spend a capped run's turns without the model ever
#: having answered.
DEFAULT_MAX_RESUMES = 6
RESUME_BASE_DELAY_SEC = 15.0
RESUME_MAX_DELAY_SEC = 240.0

#: Below this much wall-clock left, a resume is not attempted: the sleep plus one
#: request would run past the budget, and a run that ends as `error` with a
#: message naming the outage is more use to a reader than one that ends as
#: `time-limit` because it spent its last four minutes asleep.
RESUME_MIN_BUDGET_SEC = 30.0

#: How a run ended.  Only ``submitted`` carries an answer; ``error`` means the
#: harness broke and nothing about the submission was learned.
SUBMITTED = "submitted"
#: Reached only by callers that pass an explicit turn cap; the stages do not.
TURN_LIMIT = "turn-limit"
TIME_LIMIT = "time-limit"
ABANDONED = "abandoned"
ERROR = "error"

@dataclass
class LoopResult:
    """What one model run produced, and how it ended."""

    outcome: str
    #: The submit tool's arguments, or None if the model never submitted.
    payload: dict[str, Any] | None = None
    turns: int = 0
    #: Calls to the stage's own tools.  The submit call is excluded: it is the
    #: answer, not a probe, and counting it would put a floor under the error rate
    #: below -- one refused read plus a submit is exactly half, so the run that
    #: learned least would be the one that looked most reasonable.
    tool_calls: int = 0
    #: Of those calls, how many came back as ``ERROR``.  A round whose every call
    #: failed did not probe anything, however long it ran, and the scorer needs to
    #: be able to tell that from a round that looked properly and found nothing.
    tool_errors: int = 0
    elapsed_sec: float = 0.0
    #: Turns that had to be re-sent after a retryable model failure.  Published
    #: because a run that resumed four times is not the same evidence as one that
    #: never stalled, even when both end in a submission: the resumed one spent
    #: part of its wall-clock budget waiting rather than investigating, and a
    #: reader comparing its elapsed time against another run's needs to know that.
    resumes: int = 0
    usage: dict[str, Any] = field(default_factory=dict)
    transcript: str = ""
    #: Set when ``outcome == ERROR``: the harness fault, in one sentence.
    error: str = ""
    #: The model's last prose, kept for the report when it never submitted.
    last_text: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome == SUBMITTED and self.payload is not None

    @property
    def harness_fault(self) -> bool:
        return self.outcome == ERROR

    def to_dict(self) -> dict[str, Any]:
        out = {
            "outcome": self.outcome,
            "turns": self.turns,
            "tool_calls": self.tool_calls,
            "tool_errors": self.tool_errors,
            "elapsed_sec": round(self.elapsed_sec, 2),
            # Written even at 0, like `tool_errors` beside it.  Omitting it would
            # make one key mean two things -- "the loop looked and this run never
            # stalled" and "written by a harness with no resume loop" -- and this
            # corpus has already paid for a field whose absence was ambiguous.  A
            # reader comparing six rounds' elapsed times needs the first answer.
            "resumes": self.resumes,
            "usage": self.usage,
        }
        if self.transcript:
            out["transcript"] = self.transcript
        if self.error:
            out["error"] = self.error
        return out


class _Transcript:
    """JSONL, flushed per line, so a killed run still leaves what it did."""

    def __init__(self, path: str | Path | None) -> None:
        self.path = Path(path) if path else None
        self.handle = None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.handle = self.path.open("w", encoding="utf-8")

    def write(self, kind: str, **fields: Any) -> None:
        if not self.handle:
            return
        record = {"t": round(time.time(), 3), "kind": kind}
        record.update(fields)
        self.handle.write(json.dumps(record, default=str) + "\n")
        self.handle.flush()

    def close(self) -> None:
        if self.handle:
            self.handle.close()
            self.handle = None


def run(driver: Driver, system: str, opening: str, toolbox: Toolbox,
        submit: ToolSpec, *, max_turns: int | None = None,
        budget_sec: float = DEFAULT_BUDGET_SEC,
        transcript: str | Path | None = None,
        extra_tools: Sequence[ToolSpec] = (),
        extra_dispatch: Callable[[str, dict[str, Any]], str] | None = None,
        max_resumes: int = DEFAULT_MAX_RESUMES,
        on_submit: Callable[[dict[str, Any]], str | None] | None = None) -> LoopResult:
    """Drive one model until it submits, or until a budget runs out.

    ``submit`` is the tool that ends the run; its schema is the stage's answer
    format.  ``on_submit`` may reject a submission and return a sentence saying
    why, which is handed back to the model as the tool's result -- that is how a
    verifier submission missing its required evidence gets one chance to be
    corrected instead of being silently accepted.

    ``budget_sec`` is the default ceiling; ``max_turns`` is None by default and
    the run then has no turn cap at all.  ``extra_tools`` and ``extra_dispatch``
    let a stage add its own verbs; stage 3 uses them to let an adversary
    actually try a candidate test.

    A retryable model failure resumes the run rather than ending it: the same
    conversation is re-sent up to ``max_resumes`` times with a jittered backoff,
    within the same wall-clock budget.  Pass 0 to get the old behaviour, where any
    ``ModelError`` that reached here ended the run as ``error``.
    """
    tools = list(toolbox.specs()) + list(extra_tools) + [submit]
    # Clamped, because a negative one is not a smaller budget: it would make the
    # attempt loop below empty, so the request is never sent at all and the run
    # ends as `abandoned` with no error -- a model reported as having given up
    # without a single call having been made.  `Driver.__init__` clamps its own
    # `retries` the same way and for the same reason.
    max_resumes = max(0, max_resumes)
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": [{"type": "text", "text": opening}]}
    ]
    log = _Transcript(transcript)
    started = time.monotonic()
    result = LoopResult(outcome=ABANDONED,
                        transcript=str(transcript) if transcript else "")
    nudged = False

    log.write("start", model=driver.model, driver=driver.name,
              max_turns=max_turns, budget_sec=budget_sec,
              max_resumes=max_resumes,
              tools=[t.name for t in tools])
    log.write("system", text=system)
    log.write("user", text=opening)

    turns = range(1, max_turns + 1) if max_turns is not None else itertools.count(1)
    try:
        for turn_no in turns:
            elapsed = time.monotonic() - started
            if elapsed >= budget_sec:
                result.outcome = TIME_LIMIT
                log.write("stop", reason=TIME_LIMIT, elapsed_sec=round(elapsed, 1))
                break

            turn = None
            for attempt in range(max_resumes + 1):
                try:
                    turn = driver.complete(system, messages, tools)
                    break
                except ModelError as exc:
                    remaining = budget_sec - (time.monotonic() - started)
                    give_up = (not exc.retryable
                               or attempt == max_resumes
                               or remaining <= RESUME_MIN_BUDGET_SEC)
                    if give_up:
                        result.outcome = ERROR
                        result.error = str(exc)
                        log.write("error", message=str(exc),
                                  retryable=exc.retryable, turn=turn_no,
                                  resumes=result.resumes,
                                  resumes_left=max_resumes - attempt,
                                  budget_left_sec=round(remaining, 1))
                        break
                    delay = min(RESUME_MAX_DELAY_SEC, RESUME_BASE_DELAY_SEC
                                * 2 ** attempt) * (0.5 + random.random())
                    delay = min(delay, max(0.0, remaining - RESUME_MIN_BUDGET_SEC))
                    result.resumes += 1
                    log.write("resume", turn=turn_no, attempt=attempt + 1,
                              of=max_resumes, message=str(exc),
                              sleep_sec=round(delay, 1),
                              budget_left_sec=round(remaining, 1))
                    time.sleep(delay)
            if turn is None:
                break

            result.turns = turn_no
            if turn.text:
                result.last_text = turn.text
            log.write("assistant", turn=turn_no, text=turn.text,
                      stop_reason=turn.stop_reason,
                      tool_calls=[{"name": c.name, "arguments": c.arguments}
                                  for c in turn.tool_calls])
            messages.append({"role": "assistant", "content": _assistant_blocks(turn)})

            if not turn.wants_tools:
                if nudged:
                    result.outcome = ABANDONED
                    log.write("stop", reason=ABANDONED)
                    break
                nudged = True
                reminder = (
                    f"You have not called {submit.name} yet. Nothing you write "
                    f"outside that tool call is recorded as your answer. Continue "
                    f"investigating if you need to, then call {submit.name}."
                )
                messages.append({"role": "user",
                                 "content": [{"type": "text", "text": reminder}]})
                log.write("nudge", text=reminder)
                continue

            blocks: list[dict[str, Any]] = []
            finished = False
            for call in turn.tool_calls:
                if call.name == submit.name:
                    payload = call.arguments or {}
                    complaint = on_submit(payload) if on_submit else None
                    if complaint:
                        log.write("submit_rejected", reason=complaint,
                                  payload=payload)
                        blocks.append(_tool_result(call.id, complaint))
                        continue
                    result.payload = payload
                    result.outcome = SUBMITTED
                    finished = True
                    log.write("submit", payload=payload)
                    blocks.append(_tool_result(call.id, "recorded"))
                    continue
                result.tool_calls += 1
                text = _dispatch(toolbox, extra_dispatch, call.name, call.arguments)
                if text.startswith("ERROR"):
                    result.tool_errors += 1
                if len(text) > MAX_LOGGED_RESULT:
                    logged = (text[:MAX_LOGGED_RESULT]
                              + f"\n... [transcript kept {MAX_LOGGED_RESULT} B of "
                                f"{len(text)} B; the model received all of it]")
                else:
                    logged = text
                log.write("tool", name=call.name, arguments=call.arguments,
                          result=logged, result_bytes=len(text))
                blocks.append(_tool_result(call.id, text))
            messages.append({"role": "user", "content": blocks})
            if finished:
                break
        else:
            # Only reachable with an explicit turn cap.
            result.outcome = TURN_LIMIT
            log.write("stop", reason=TURN_LIMIT, turns=max_turns)
    finally:
        result.elapsed_sec = time.monotonic() - started
        result.usage = driver.usage()
        log.write("end", outcome=result.outcome, resumes=result.resumes,
                  elapsed_sec=round(result.elapsed_sec, 1), usage=result.usage)
        log.close()
    return result


def _dispatch(toolbox: Toolbox,
              extra: Callable[[str, dict[str, Any]], str] | None,
              name: str, args: dict[str, Any]) -> str:
    if "__unparsed__" in (args or {}):
        return ("ERROR: your tool arguments were not valid JSON, so nothing ran. "
                "Send the call again with well-formed arguments.")
    if toolbox.handles(name):
        return toolbox.dispatch(name, args or {})
    if extra is not None:
        try:
            return extra(name, args or {})
        except Exception as exc:  # a stage tool misbehaving is the model's problem
            return f"ERROR: {type(exc).__name__}: {exc}"
    return f"ERROR: no such tool {name!r}"


def _assistant_blocks(turn: Turn) -> list[dict[str, Any]]:
    """Echo the provider's own blocks when it gave us any.

    Some models sign their reasoning blocks and reject a follow-up request that
    dropped them, so a reconstruction from ``text`` is not good enough.
    """
    if isinstance(turn.raw, list) and turn.raw:
        return turn.raw
    blocks: list[dict[str, Any]] = []
    if turn.text:
        blocks.append({"type": "text", "text": turn.text})
    for call in turn.tool_calls:
        blocks.append({"type": "tool_use", "id": call.id, "name": call.name,
                       "input": call.arguments})
    return blocks or [{"type": "text", "text": ""}]


def _tool_result(call_id: str, text: str) -> dict[str, Any]:
    return {"type": "tool_result", "tool_use_id": call_id,
            "content": [{"type": "text", "text": text}]}
