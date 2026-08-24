"""Talking to models, without committing the benchmark to one vendor.

Two stages need a model: the audit verifier reads a submission and reports
on it, and each stage-3 adversary tries to break one.  Both want the same three
things -- a system prompt, a conversation, and a set of tools the model may call
-- so both go through one interface, and the choice of vendor becomes a line of
configuration rather than a rewrite.

The interface is one turn wide.  ``complete()`` sends what it is given and
returns what came back, including any tool calls; the loop that decides what to
do about those lives in ``agentloop.py``.  Keeping the two apart is what lets a
driver be exercised with no agent and an agent with no network.

Messages use Anthropic's block format.  One driver has to define the shape, and
translating in one place beats translating in three.

Failures split the way results do.  ``ModelError.retryable`` separates "the API
was busy" from "that request was malformed": the first is retried and then
reported as a harness fault, which makes the stage ``error``; the second is an
authoring bug and aborts loudly.  Neither is ever the submission's fault, which
is why no driver returns a verdict.
"""

from __future__ import annotations

import json
import os
import random
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

#: A ceiling, not a dial.  Effort decides how hard a model works; this only has
#: to be high enough that it never becomes the binding constraint, because
#: thinking tokens count against it and a cap sized for a non-thinking answer
#: truncates a thinking one.  128k is the documented max output for Opus 5 and
#: Sonnet 5, and the Sub2API gateway accepts it for the gpt-5.6 family, so it is
#: the highest figure both dialects take.
DEFAULT_MAX_TOKENS = 128000
DEFAULT_TIMEOUT_SEC = 300.0
DEFAULT_RETRIES = 4

#: Worth trying again: rate limits, overload, and the 5xx family.
#:
#: 520-524 are here because the vendor never sends them and they are what decides
#: real runs.  They are Cloudflare's, invented at the edge when the origin does not
#: answer -- 520 unknown, 521 down, 522 connect timeout, 523 unreachable, 524
#: origin timeout -- and a set enumerated from a vendor's documented errors has
#: none of them.  524 in particular is the same event as the 504 already listed,
#: under a different number because a different hop reported it.
#:
#: What it costs to omit one is not a missing retry.  A non-retryable ModelError is
#: a harness fault, which ends an adversary round at ``outcome=error``, and scoring
#: discards the whole stage rather than credit a round that could not run -- so one
#: of these in one of six rounds turns a graded run into an unmeasured one.  This
#: campaign lost eighteen rounds to 524 that way, each 199-1792s and up to 109 tool
#: calls in, every one recorded ``resumes: 0``.
#:
#: A 4xx stays out on its status alone: a malformed request is malformed on the
#: fifth attempt too, and retrying an authoring bug hides it.  One 400 is retried
#: anyway, on its body rather than its code -- see RETRYABLE_BODY.
RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504,
                              520, 521, 522, 523, 524, 529})

#: A 400 whose body says the request was *declined*, not malformed.
#:
#: The upstream safety filter on the OpenAI path refuses some adversary prompts as
#: "flagged for possible cybersecurity risk".  It is stochastic, which is the whole
#: reason this exists: round a01 of pf03 was refused on two attempts and completed on
#: a third, same prompt, same round id, same driver, no change in between.  An earlier
#: version of the comment above asserted that "a filter declining a request declines it
#: identically on the fifth attempt" -- that is what makes intuitive sense, and it is
#: not what the three attempts measured.
#:
#: It costs what the 524 costs.  A non-retryable ModelError is a harness fault, the
#: round ends ``outcome=error``, and scoring discards the whole stage -- so a filter
#: that declines two thirds of the time and a proxy that times out are the same defect
#: wearing a different status code.  Two rounds were lost this way, both pf03 a01.
#:
#: Matched on the body and not on the status, because the two 400s must stay
#: distinguishable: a stage-3 prompt that is genuinely malformed is an authoring bug
#: this campaign needs to hear about on the first attempt, not four backoffs later.
#: The adversary prompt asks a model to find defects in a port of byte-order handling,
#: which is authorised review of a tree the benchmark owns; the refusal is a false
#: positive on legitimate work, so retrying it is recovering a measurement rather than
#: evading a control.
RETRYABLE_BODY: tuple[str, ...] = (
    "flagged for possible cybersecurity risk",
)

#: A body that makes an otherwise-retryable status permanent.  The inverse of
#: RETRYABLE_BODY, and needed for the same reason: the status is decided by whichever
#: hop reported the failure, so it cannot say whether the request itself is the problem.
#:
#: sub2api wraps upstream failures as ``502 {"type":"upstream_error"}``, and 502 is in
#: RETRYABLE_STATUS on purpose -- the 524 lesson above put it there.  So a context
#: overflow, which is the one failure that is *guaranteed* to repeat, arrives wearing
#: the status reserved for failures that are guaranteed not to.
#:
#: Measured on lang07/high's regrade, round a03: turn 61 raised
#: ``502 ... "Your input exceeds the context window of this model"`` and the loop
#: retried it six times with exponential backoff -- 19s, 35s, 33s, 105s, 342s, 306s,
#: ~841s of sleeping -- re-sending a byte-identical payload each time, then failed
#: anyway with 276s of round budget still unspent.  Sleeping does not shrink a prompt.
#:
#: The cost is not only the wasted budget.  ``retryable: true`` on a permanent failure
#: is a false statement in the one record anybody reads afterwards: I first took those
#: six resumes for a flaky gateway, when the cause was a prompt that had grown past the
#: window over 61 turns and 183 tool calls.  Note what this does NOT do -- the round is
#: lost either way, because the overflow is real; it ends honestly and 14 minutes
#: sooner.
#:
#: Phrases across the three dialects the campaign speaks.  Each must be one no
#: transient failure could contain, since this vetoes a retry that would otherwise
#: happen and a false positive here costs a recoverable round.
FATAL_BODY: tuple[str, ...] = (
    "exceeds the context window",        # sub2api / upstream_error
    "maximum context length",            # OpenAI native
    "prompt is too long",                # Anthropic native
)


def _body_fatal(detail: str) -> bool:
    """Does this body describe a request that cannot succeed on any attempt?

    Checked on the body alone, with no status condition: the whole point is that the
    status is the wrong witness here.  Applied as a veto over
    ``status in RETRYABLE_STATUS``, so a phrase appearing in a 500 stops that retry
    too -- which is correct, because the request is what is too large.
    """
    return any(sig in detail for sig in FATAL_BODY)


def _body_retryable(status: int, detail: str) -> bool:
    """Is this 400 a refusal that a later attempt may not repeat?

    Narrow on purpose: only status 400, and only a body carrying one of a named set
    of vendor phrases.  A substring test over a 600-char error body is a blunt
    instrument, so the phrases have to be ones no malformed-request error would
    contain.
    """
    if status != 400:
        return False
    return any(sig in detail for sig in RETRYABLE_BODY)


def _is_retryable(status: int, detail: str) -> bool:
    """The whole retry decision for an HTTP failure, in one place.

    The two HTTPError handlers below -- streaming and ``_post`` -- had byte-identical
    two-line expressions, and adding the fatal veto would have made them byte-identical
    three-line ones.  Duplicated policy is exactly how the host and image copies of
    ``srb-agent-loop.sh`` drifted in both directions at once, so the precedence lives
    here and the call sites ask rather than restate.

    Precedence, and the order matters:

      1. a fatal body vetoes everything.  A request too large for the window is too
         large on every attempt, whatever status the reporting hop chose.
      2. otherwise a retryable status retries.
      3. otherwise a named body phrase can still retry a 400 (RETRYABLE_BODY).

    Rules 1 and 3 cannot collide -- one is about the request's size, the other about a
    safety filter's verdict -- but if a body ever matched both, refusing to retry is the
    safer resolution: rule 3 recovers a round that might have worked, and rule 1 exists
    because that hope is already known to be false.
    """
    if _body_fatal(detail):
        return False
    return status in RETRYABLE_STATUS or _body_retryable(status, detail)

#: Reasoning effort is named, not numbered, and both dialects take the same five
#: names -- OpenAI at ``reasoning_effort``, Anthropic at ``output_config.effort``
#: -- so the name configuration asked for is the name that goes on the wire,
#: untranslated.  It is a behavioural signal to the model, not a token budget:
#: an earlier version of this file mapped each name to an Anthropic
#: ``thinking.budget_tokens`` figure, which was wrong twice over.  It sent a
#: request shape that returns 400 on Opus 4.7 and later, and it made effort look
#: like a quantity of tokens, which meant `xhigh` and `max` -- distinct levels
#: at the endpoint -- collapsed into one number and became the same request.
EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")

class ModelError(Exception):
    """A call to a model failed.

    ``retryable`` is the interesting field.  A stage that ends in a retryable
    ModelError reports ``status="error"`` and no verdict, so the submission is
    re-run rather than scored zero on the strength of an outage.
    """

    def __init__(self, message: str, *, retryable: bool = False,
                 status: int | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status = status


@dataclass
class ToolSpec:
    """A tool offered to the model.  ``schema`` is JSON Schema for the input."""

    name: str
    description: str
    schema: dict[str, Any]

    def anthropic(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description,
                "input_schema": self.schema}

    def openai(self) -> dict[str, Any]:
        return {"type": "function",
                "function": {"name": self.name,
                             "description": self.description,
                             "parameters": self.schema}}


@dataclass
class ToolCall:
    """One tool invocation the model asked for."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Turn:
    """One model response."""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    #: What the endpoint says the call cost in its own billing unit, when it says
    #: so at all.  Recorded because token counts are not always a measurement:
    #: a gateway that rewrites requests reports tokens for what it forwarded, not
    #: for what was sent, and one graded round here reported 8k input tokens for
    #: eighty turns whose transcript could not have been sent in under ~600k.
    #: Where an endpoint bills in its own unit, that unit is the honest figure.
    credits: float = 0.0
    #: The provider's own assistant content, kept verbatim.  Some models return
    #: blocks that must be echoed back unchanged on the next request (reasoning
    #: blocks, signatures); reconstructing them from ``text`` would drop those.
    raw: Any = None

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class Driver:
    """Base class: retry policy, HTTP, and accounting.

    Subclasses implement ``_once()``.  The retry loop is here so every vendor
    gets the same backoff and the same accounting of what it spent.
    """

    name = "driver"

    def __init__(self, model: str, *, max_tokens: int = DEFAULT_MAX_TOKENS,
                 temperature: float | None = None,
                 timeout_sec: float = DEFAULT_TIMEOUT_SEC,
                 retries: int = DEFAULT_RETRIES,
                 options: dict[str, Any] | None = None) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.timeout_sec = timeout_sec
        self.retries = max(0, retries)
        self.options = dict(options or {})
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.credits = 0.0

    # -- public ------------------------------------------------------------- #

    def complete(self, system: str, messages: list[dict[str, Any]],
                 tools: Sequence[ToolSpec] = ()) -> Turn:
        last: ModelError | None = None
        for attempt in range(self.retries + 1):
            try:
                turn = self._once(system, messages, tools)
            except ModelError as exc:
                if not exc.retryable or attempt == self.retries:
                    raise
                last = exc
                # Full jitter: several adversaries share one endpoint, and
                # synchronised retries are how a rate limit becomes a stall.
                delay = min(60.0, 2.0 ** attempt) * (0.5 + random.random())
                time.sleep(delay)
                continue
            self.calls += 1
            self.input_tokens += turn.input_tokens
            self.output_tokens += turn.output_tokens
            self.credits += turn.credits
            return turn
        raise last or ModelError("retries exhausted", retryable=True)

    def usage(self) -> dict[str, Any]:
        # The effort is recorded next to the model because it is part of what was
        # run, not a detail of how it was run: a stage graded at one effort is not
        # the same measurement as the same stage graded at another.  Recorded only
        # when set, so a roster line that forgot it reads as absent here instead
        # of borrowing whatever the endpoint's default happens to be.
        record = {"driver": self.name, "model": self.model, "calls": self.calls,
                  "input_tokens": self.input_tokens,
                  "output_tokens": self.output_tokens}
        effort = self.options.get("reasoning_effort")
        if effort:
            record["reasoning_effort"] = str(effort).strip().lower()
        # Only when the endpoint actually billed in credits.  A zero written here
        # unconditionally would read as "this round was free" for every endpoint
        # that bills in tokens, which is worse than saying nothing.
        if self.credits:
            record["credits"] = round(self.credits, 6)
        return record

    # -- for subclasses ----------------------------------------------------- #

    def _once(self, system: str, messages: list[dict[str, Any]],
              tools: Sequence[ToolSpec]) -> Turn:
        raise NotImplementedError

    #: Env var holding extra headers as a JSON object, per driver name.
    extra_headers_env = ""

    def _headers(self, base: dict[str, str]) -> dict[str, str]:
        """Auth headers, plus whatever the environment and config add.

        Gateways in front of a vendor's API are common, and some of them decide
        what to accept on headers this file has no reason to know about.  Two
        ways in, because the two kinds of header differ in who may see them:

        * ``$<DRIVER>_EXTRA_HEADERS``, a JSON object, for headers that belong to
          one deployment's endpoint.  Those go next to the key, in the operator's
          environment, for the same reason the key does -- a task file is public
          and an endpoint's admission rules are not.
        * ``options.extra_headers`` in the stage's ``driver_options``, for a
          header that is genuinely part of what the task asks for.

        Config wins over the environment, and both may override what is set
        here: a gateway wanting ``authorization`` where the vendor wants
        ``x-api-key`` is exactly the case this exists for.
        """
        merged = dict(base)
        for source, raw in self._header_sources():
            if not raw:
                continue
            if not isinstance(raw, dict):
                raise ModelError(
                    f"{self.name}: {source} must be a table of header names to "
                    f"string values, not {type(raw).__name__}"
                )
            merged.update({str(k): str(v) for k, v in raw.items()})
        return merged

    def _header_sources(self) -> list[tuple[str, Any]]:
        sources: list[tuple[str, Any]] = []
        if self.extra_headers_env:
            blob = os.environ.get(self.extra_headers_env, "").strip()
            if blob:
                try:
                    sources.append((f"${self.extra_headers_env}", json.loads(blob)))
                except json.JSONDecodeError as exc:
                    raise ModelError(
                        f"{self.name}: ${self.extra_headers_env} is not JSON: {exc}"
                    ) from exc
        sources.append(("options.extra_headers", self.options.get("extra_headers")))
        return sources

    def _post_sse(self, url: str, headers: dict[str, str],
                  payload: dict[str, Any]) -> dict[str, Any]:
        """POST an Anthropic request and rebuild the non-streaming reply from SSE.

        Returns the same ``{"content": [...], "usage": {...}, "stop_reason": ...}``
        a non-streaming call returns, so callers cannot tell the difference and
        there is one parser for both.

        Errors keep the classification ``_post`` gives them.  Two are specific to
        streaming and both are retryable, because both mean the measurement is
        absent rather than negative: an ``error`` event mid-stream, and a stream
        that stops before ``message_stop``.  A truncated stream silently treated as
        a finished one is the dangerous case -- it would hand the adjudicator a
        half-written answer and score it as the adversary's verdict.

        ``stream`` is forced here rather than trusted from the caller.  A caller
        that forgot it would get one JSON object on a single ``data:`` line, this
        parser would find no ``message_stop``, and the round would fail as "the
        answer is incomplete" -- a message naming nothing about the actual cause.
        """
        payload = {**payload, "stream": True}
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST")
        for key, value in headers.items():
            req.add_header(key, value)
        req.add_header("content-type", "application/json")

        blocks: list[dict[str, Any]] = []
        # tool_use arguments arrive as `partial_json` fragments that are only
        # parseable once concatenated, so text and JSON accumulate separately.
        partial: dict[int, list[str]] = {}
        usage: dict[str, Any] = {}
        stop_reason = ""
        saw_stop = False

        try:
            with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                event = ""
                for raw in resp:
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    if not line:
                        event = ""
                        continue
                    if line.startswith("event:"):
                        event = line[6:].strip()
                        continue
                    if not line.startswith("data:"):
                        continue
                    chunk = line[5:].strip()
                    if not chunk or chunk == "[DONE]":
                        continue
                    try:
                        ev = json.loads(chunk)
                    except json.JSONDecodeError:
                        continue
                    kind = ev.get("type") or event

                    if kind == "error":
                        err = ev.get("error") or {}
                        raise ModelError(
                            f"{self.name}: stream error from {url}: "
                            f"{err.get('type', '?')}: {err.get('message', '')}"[:600],
                            retryable=True,
                        )
                    if kind == "message_start":
                        msg = ev.get("message") or {}
                        usage.update(msg.get("usage") or {})
                        stop_reason = str(msg.get("stop_reason") or "") or stop_reason
                    elif kind == "content_block_start":
                        idx = int(ev.get("index", len(blocks)))
                        block = dict(ev.get("content_block") or {})
                        while len(blocks) <= idx:
                            blocks.append({})
                        blocks[idx] = block
                        if block.get("type") == "tool_use":
                            partial[idx] = []
                    elif kind == "content_block_delta":
                        idx = int(ev.get("index", 0))
                        delta = ev.get("delta") or {}
                        if idx >= len(blocks):
                            continue
                        dt = delta.get("type")
                        if dt == "text_delta":
                            blocks[idx]["text"] = (blocks[idx].get("text") or "") \
                                + str(delta.get("text") or "")
                        elif dt == "thinking_delta":
                            blocks[idx]["thinking"] = (blocks[idx].get("thinking") or "") \
                                + str(delta.get("thinking") or "")
                        elif dt == "input_json_delta":
                            partial.setdefault(idx, []).append(
                                str(delta.get("partial_json") or ""))
                    elif kind == "content_block_stop":
                        idx = int(ev.get("index", 0))
                        if idx in partial and idx < len(blocks):
                            joined = "".join(partial.pop(idx))
                            # An empty argument list is `{}` on the wire only
                            # sometimes; an unparseable one must not become {} and
                            # read as "the tool was called with no arguments".
                            if joined.strip():
                                try:
                                    blocks[idx]["input"] = json.loads(joined)
                                except json.JSONDecodeError as exc:
                                    raise ModelError(
                                        f"{self.name}: tool arguments were not "
                                        f"valid JSON after {len(joined)} bytes: {exc}",
                                        retryable=True,
                                    ) from exc
                            else:
                                blocks[idx].setdefault("input", {})
                    elif kind == "message_delta":
                        delta = ev.get("delta") or {}
                        stop_reason = str(delta.get("stop_reason") or "") or stop_reason
                        usage.update(ev.get("usage") or {})
                    elif kind == "message_stop":
                        saw_stop = True
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:600]
            except Exception:
                pass
            raise ModelError(
                f"{self.name}: HTTP {exc.code} from {url}: {detail}",
                retryable=_is_retryable(exc.code, detail),
                status=exc.code,
            ) from exc
        except urllib.error.URLError as exc:
            raise ModelError(f"{self.name}: cannot reach {url}: {exc.reason}",
                             retryable=True) from exc
        except TimeoutError as exc:
            raise ModelError(f"{self.name}: timed out after {self.timeout_sec}s",
                             retryable=True) from exc

        if not saw_stop:
            raise ModelError(
                f"{self.name}: stream ended after {len(blocks)} block(s) without "
                f"message_stop; the answer is incomplete",
                retryable=True,
            )
        return {"content": [b for b in blocks if b],
                "usage": usage,
                "stop_reason": stop_reason}

    def _post(self, url: str, headers: dict[str, str],
              payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST")
        for key, value in headers.items():
            req.add_header(key, value)
        req.add_header("content-type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:600]
            except Exception:
                pass
            raise ModelError(
                f"{self.name}: HTTP {exc.code} from {url}: {detail}",
                retryable=_is_retryable(exc.code, detail),
                status=exc.code,
            ) from exc
        except urllib.error.URLError as exc:
            raise ModelError(f"{self.name}: cannot reach {url}: {exc.reason}",
                             retryable=True) from exc
        except TimeoutError as exc:
            raise ModelError(f"{self.name}: timed out after {self.timeout_sec}s",
                             retryable=True) from exc
        except json.JSONDecodeError as exc:
            raise ModelError(f"{self.name}: response was not JSON: {exc}",
                             retryable=True) from exc


class AnthropicDriver(Driver):
    """Anthropic Messages API over plain HTTP.

    No SDK: the verifier image should not need a package index to be gradeable
    two years from now, and this is one POST with a documented body.
    """

    name = "anthropic"
    API_VERSION = "2023-06-01"
    extra_headers_env = "ANTHROPIC_EXTRA_HEADERS"

    def __init__(self, model: str, **kw: Any) -> None:
        super().__init__(model, **kw)
        self.base_url = str(
            self.options.get("base_url")
            or os.environ.get("ANTHROPIC_BASE_URL")
            or "https://api.anthropic.com"
        ).rstrip("/")
        self.api_key = str(
            self.options.get("api_key_env")
            and os.environ.get(str(self.options["api_key_env"]), "")
            # AUTH_TOKEN is what a gateway in front of the API usually calls it.
            or os.environ.get("ANTHROPIC_API_KEY", "")
            or os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
        )

    def _once(self, system, messages, tools):
        if not self.api_key:
            raise ModelError(
                "anthropic: ANTHROPIC_API_KEY is unset; the verifier cannot run"
            )
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": messages,
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = [t.anthropic() for t in tools]
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        for key in ("thinking", "output_config", "top_p", "stop_sequences",
                    "top_k"):
            if key in self.options:
                payload[key] = self.options[key]
        # Effort is named in config so one key means the same thing whichever
        # driver reads it.  An explicit ``output_config`` wins: someone who
        # wrote one by hand meant it.
        effort = self.options.get("reasoning_effort")
        if effort and "output_config" not in payload:
            payload["output_config"] = {"effort": _effort_name(self.name, effort)}

        # Streamed, and not for latency -- for admission.  A non-streaming
        # /v1/messages sends no bytes until the whole completion exists, and this
        # gateway is behind Cloudflare, which invents a 524 when the origin has
        # been silent for ~100s.  A reasoning model asked for up to 128k tokens is
        # over that on nearly every adversary turn, so the round ended
        # `outcome=error` and scoring discarded the entire stage.
        #
        # Retrying cannot fix it and this campaign proved that the expensive way:
        # 520-524 were added to RETRYABLE_STATUS earlier today, and the very next
        # regrades still lost a06 on build03/none and build03/low, because five
        # attempts at a request that structurally cannot return in 100s are five
        # 524s.  Measured on this endpoint with one 8000-token ask: unstreamed,
        # first byte at 90.7s; streamed, first byte at 1.3s and a clean 131.7s
        # finish -- i.e. the same work fails only for want of an early byte.
        #
        # The SSE is reassembled into the shape the non-streaming API returns, so
        # the block parsing below is unchanged and stays the single reader of it.
        data = self._post_sse(
            f"{self.base_url}/v1/messages",
            self._headers({"x-api-key": self.api_key,
                           "anthropic-version": self.API_VERSION,
                           "accept": "text/event-stream"}),
            {**payload, "stream": True},
        )
        blocks = data.get("content") or []
        text_parts, calls = [], []
        for block in blocks:
            kind = block.get("type")
            if kind == "text":
                text_parts.append(block.get("text", ""))
            elif kind == "tool_use":
                calls.append(ToolCall(id=str(block.get("id", "")),
                                      name=str(block.get("name", "")),
                                      arguments=block.get("input") or {}))
        usage = data.get("usage") or {}
        return Turn(
            text="\n".join(p for p in text_parts if p),
            tool_calls=calls,
            stop_reason=str(data.get("stop_reason") or ""),
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            credits=float(usage.get("credit_usage") or 0.0),
            raw=blocks,
        )


def _effort_name(driver: str, effort: Any) -> str:
    """Check a named reasoning effort, or raise a listing error.

    Nothing is translated here.  The point is to fail on a typo *now*, at
    configuration time, instead of taking a 400 from the endpoint partway
    through a graded run -- and to fail loudly, since a silently dropped effort
    would run the whole stage at the endpoint's default and look like a result.
    """
    key = str(effort).strip().lower()
    if key not in EFFORT_LEVELS:
        raise ModelError(
            f"{driver}: unknown reasoning_effort {effort!r}; known efforts are "
            f"{', '.join(EFFORT_LEVELS)}"
        )
    return key


class OpenAIDriver(Driver):
    """Any endpoint speaking OpenAI's chat-completions dialect.

    Stage 3 asks for six *kinds* of model, which will not all be one vendor's.
    This dialect is what the others have converged on -- OpenAI itself, most
    gateways, vLLM, llama.cpp -- so one driver covers the rest of the field, at
    the cost of translating the block format on the way in and out.
    """

    name = "openai"
    extra_headers_env = "OPENAI_EXTRA_HEADERS"

    def __init__(self, model: str, **kw: Any) -> None:
        super().__init__(model, **kw)
        self.base_url = str(
            self.options.get("base_url")
            or os.environ.get("OPENAI_BASE_URL")
            or "https://api.openai.com/v1"
        ).rstrip("/")
        env_key = str(self.options.get("api_key_env") or "OPENAI_API_KEY")
        self.api_key = os.environ.get(env_key, "")
        self.env_key = env_key

    def _once(self, system, messages, tools):
        if not self.api_key:
            raise ModelError(f"openai: {self.env_key} is unset")
        chat: list[dict[str, Any]] = []
        if system:
            chat.append({"role": "system", "content": system})
        chat.extend(_to_openai_messages(messages))
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": chat,
            "max_completion_tokens": self.max_tokens,
        }
        if tools:
            payload["tools"] = [t.openai() for t in tools]
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        for key in ("top_p", "stop", "seed"):
            if key in self.options:
                payload[key] = self.options[key]
        # This dialect names the parameter the way configuration does, so the
        # name goes straight out -- but through the same check as the other
        # driver, so a typo fails here rather than at the endpoint.
        if "reasoning_effort" in self.options:
            payload["reasoning_effort"] = _effort_name(
                self.name, self.options["reasoning_effort"])

        data = self._post(
            f"{self.base_url}/chat/completions",
            self._headers({"authorization": f"Bearer {self.api_key}"}),
            payload,
        )
        choices = data.get("choices") or []
        if not choices:
            raise ModelError("openai: response had no choices", retryable=True)
        message = choices[0].get("message") or {}
        calls = []
        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            raw_args = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except json.JSONDecodeError:
                # A malformed argument string is the model's mistake, not the
                # transport's; hand it up as text so the loop can say so.
                args = {"__unparsed__": raw_args}
            calls.append(ToolCall(id=str(call.get("id", "")),
                                  name=str(fn.get("name", "")),
                                  arguments=args))
        usage = data.get("usage") or {}
        return Turn(
            text=str(message.get("content") or ""),
            tool_calls=calls,
            stop_reason=str(choices[0].get("finish_reason") or ""),
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
        )


def _to_openai_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Anthropic blocks -> OpenAI messages.

    The shapes disagree in one structural way: Anthropic puts tool results in a
    user message, OpenAI gives them their own role.  So one input message can
    become several output messages.
    """
    out: list[dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content")
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        results: list[dict[str, Any]] = []
        for block in content or []:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                text_parts.append(block.get("text", ""))
            elif kind == "tool_use":
                tool_calls.append({
                    "id": block.get("id", ""),
                    "type": "function",
                    "function": {"name": block.get("name", ""),
                                 "arguments": json.dumps(block.get("input") or {})},
                })
            elif kind == "tool_result":
                results.append({
                    "role": "tool",
                    "tool_call_id": block.get("tool_use_id", ""),
                    "content": _flatten_result(block.get("content")),
                })
        if role == "assistant":
            entry: dict[str, Any] = {"role": "assistant",
                                     "content": "\n".join(text_parts) or None}
            if tool_calls:
                entry["tool_calls"] = tool_calls
            out.append(entry)
        else:
            if text_parts:
                out.append({"role": "user", "content": "\n".join(text_parts)})
            out.extend(results)
    return out


def _flatten_result(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return json.dumps(content) if content is not None else ""


class CommandDriver(Driver):
    """Delegates a turn to an external command: JSON in on stdin, JSON out.

    The escape hatch for a model this file cannot reach -- one behind a bespoke
    gateway, one that only has a CLI, a local weights file.  Whoever adds such a
    model writes a script instead of a driver:

        {"model":..., "system":..., "messages":[...], "tools":[...]}
        -> {"text":..., "tool_calls":[{"id","name","arguments"}], "usage":{...}}
    """

    name = "command"

    def _once(self, system, messages, tools):
        argv = self.options.get("command")
        if not argv or not isinstance(argv, list):
            raise ModelError("command driver: options.command must be a list")
        request = json.dumps({
            "model": self.model,
            "system": system,
            "messages": messages,
            "tools": [t.anthropic() for t in tools],
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        })
        try:
            proc = subprocess.run(
                [str(a) for a in argv], input=request, capture_output=True,
                text=True, timeout=self.timeout_sec,
            )
        except subprocess.TimeoutExpired as exc:
            raise ModelError(f"command driver: timed out after {self.timeout_sec}s",
                             retryable=True) from exc
        except OSError as exc:
            raise ModelError(f"command driver: cannot run {argv[0]!r}: {exc}") from exc
        if proc.returncode != 0:
            raise ModelError(
                f"command driver: exit {proc.returncode}: "
                f"{(proc.stderr or '').strip()[:600]}",
                retryable=True,
            )
        try:
            data = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise ModelError(f"command driver: stdout was not JSON: {exc}") from exc
        usage = data.get("usage") or {}
        return Turn(
            text=str(data.get("text") or ""),
            tool_calls=[
                ToolCall(id=str(c.get("id") or f"call_{i}"),
                         name=str(c.get("name") or ""),
                         arguments=c.get("arguments") or {})
                for i, c in enumerate(data.get("tool_calls") or [])
            ],
            stop_reason=str(data.get("stop_reason") or ""),
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
        )


class ScriptedDriver(Driver):
    """Replays turns from a file, for tests and dry runs.

    The stage runners are the parts most likely to break, and they are exactly
    the parts that cost money to exercise.  This makes them testable: write the
    turns a model would have produced, and assert on what the runner did with
    them.  ``--driver scripted`` also lets a new task's prompt be smoke-tested
    without spending a budget on it.
    """

    name = "scripted"

    def __init__(self, model: str, **kw: Any) -> None:
        super().__init__(model, **kw)
        self.index = 0
        turns = self.options.get("turns")
        if turns is None:
            path = self.options.get("script")
            if not path:
                raise ModelError("scripted driver: needs options.script or .turns")
            try:
                turns = json.loads(Path(str(path)).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ModelError(f"scripted driver: cannot read {path}: {exc}") from exc
        if isinstance(turns, dict):
            turns = turns.get("turns") or []
        self.turns = list(turns)

    def _once(self, system, messages, tools):
        if self.index >= len(self.turns):
            raise ModelError(
                f"scripted driver: exhausted after {len(self.turns)} turns; the "
                f"loop asked for more than the script provides"
            )
        spec = self.turns[self.index]
        self.index += 1
        if isinstance(spec, str):
            spec = {"text": spec}
        return Turn(
            text=str(spec.get("text") or ""),
            tool_calls=[
                ToolCall(id=str(c.get("id") or f"scripted_{self.index}_{i}"),
                         name=str(c.get("name") or ""),
                         arguments=c.get("arguments") or {})
                for i, c in enumerate(spec.get("tool_calls") or [])
            ],
            stop_reason=str(spec.get("stop_reason") or ""),
        )


DRIVERS: dict[str, type[Driver]] = {
    "anthropic": AnthropicDriver,
    "openai": OpenAIDriver,
    "command": CommandDriver,
    "scripted": ScriptedDriver,
}


#: Which dialect a model family speaks, by name prefix.  Only families whose
#: dialect is not in question are listed; anything unrecognised gets no opinion,
#: because a gateway may serve a model under a name of its own choosing.
_DIALECT_BY_PREFIX: tuple[tuple[str, str], ...] = (
    ("claude-", "anthropic"),
    ("gpt-", "openai"),
    ("o1-", "openai"),
    ("o3-", "openai"),
    ("o4-", "openai"),
)


def dialect_for(model: str) -> str | None:
    """The driver this model's name implies, or None if the name does not say.

    A mismatch here is silent and expensive: the two drivers differ in their URL,
    their tool-schema key and their message shape, so asking the wrong one for a
    model produces a request the endpoint rejects -- or, worse, one it accepts and
    answers without tools.  The name is the only thing declaring the dialect, so
    it is worth checking against.
    """
    name = model.strip().lower()
    for prefix, driver in _DIALECT_BY_PREFIX:
        if name.startswith(prefix):
            return driver
    return None


def build(driver: str, model: str, *, options: dict[str, Any] | None = None,
          **kw: Any) -> Driver:
    """Construct a driver by name, with a message that lists the alternatives."""
    try:
        cls = DRIVERS[driver]
    except KeyError:
        raise ModelError(
            f"unknown driver {driver!r}; known drivers are "
            f"{', '.join(sorted(DRIVERS))}"
        ) from None
    opts = dict(options or {})
    for key in ("max_tokens", "temperature", "timeout_sec", "retries"):
        if key in opts and key not in kw:
            kw[key] = opts.pop(key)
    return cls(model, options=opts, **kw)
