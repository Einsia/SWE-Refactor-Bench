#!/usr/bin/env python3
"""Case execution and differential comparison.

One executor, because this task has one instrument: a probe that reads a JSON
request per line on stdin and writes a JSON response per line on stdout.  Both
sides go through `ProbeRunner` -- it holds an argv *prefix* rather than a binary
path, which is what lets one runner serve both.

Here the two prefixes are `["node", "<reference>/dist/probe.js"]` and `["node",
"<submission>/dist/probe.js"]`.  That is unusual and it is the shape of this task:
the old runtime and the new runtime are the same runtime, so there is no
asymmetry between the sides to compensate for and no toolchain difference that
could excuse a difference in the answers.  It also means the reference is never
run in the grading image -- expectations are frozen at image build time, from a
tree that is then removed -- because a JavaScript reference sitting beside a
`node` that must exist anyway is a copyable answer key.

It holds no expected value of its own.  Expected output is whatever the pinned
JavaScript reference produced under identical input, which is why jsonata's
quirks are graded as behaviour rather than as defects to correct.

Why the framing is what it is
-----------------------------
The protocol is line-delimited JSON in both directions, which is the probe's
public contract and not negotiable here -- `instruction.md` publishes it and a
submission implements against it.  That gives this file one problem a
length-prefixed framing would not have: a response line is only self-delimiting
if the probe never writes a bare newline inside one.  A submission that
pretty-prints its JSON produces output that parses as several malformed lines
rather than one good one, and the failure has to be attributed to the case that
caused it instead of voiding the batch.

Pairing therefore works two ways, and the choice is per invocation rather than
global:

  * Several requests in one process are paired by the `id` each response echoes.
    `run_all` only batches requests whose answering id is unique and recoverable,
    so within a batch the key is exact, and it is the only thing that survives a
    crash -- the ids present are how the resume loop learns which requests were
    reached.
  * One request in one process is paired by position: whatever came out is the
    answer to it.  This is the case that cannot be id-keyed, because half the
    `_protocol` family is answered under id 0 (the probe reports `0` when it
    could not recover an id at all), and several of those under one key would
    overwrite each other.

`BatchOutcome.ordered` is the positional view assembled from whichever of the two
applied, and it is the view callers grade against.  The id-keyed dict cannot be
graded through: the cases it merges under one key are exactly the ones whose answers
would then be compared against a neighbour's.

Multi-line cases
----------------
Ten of this task's protocol cases send more than one line -- a malformed line
followed by a good one, a repeated id, five lines at once -- because the thing
being graded is what the *stream* does, not what one request does.  Those cases
travel as a `list[str]`, always alone in a process, and their answer is the whole
of stdout rather than one line of it.  Anything else would throw the property away:
`_protocol/p0001-then-good` is exactly the claim that a bad line draws one answer
and does not consume the next request, and that claim only exists in the
relationship between two response lines.

A crash is the other case.  When the probe dies mid-batch the ids after the
victim never arrive; the batch is re-driven from the request after the last one
that answered, so a probe that dies on one expression still gets graded on the
rest.  `MAX_RESUMES` bounds that: a probe that dies on every case would otherwise
restart once per case, and 13,000 process spawns is a timeout rather than a
report.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import vlib
from vlib import Log

# Requests per process. Large enough that process startup is amortised -- node
# takes ~40ms against 13,940 cases, so per-request spawning would spend ten minutes
# on `node` alone -- and small enough that a probe which leaks memory per request
# does not accumulate across the whole corpus. It also bounds the loss from a crash
# the resume logic cannot localise.
BATCH_SIZE = 400

# One batch. Generous: the reference answers the entire 13,940-case corpus in about
# three seconds, so a 400-request batch is milliseconds of work and this only fires
# on a probe that hangs. `repeat` cases, which evaluate one expression up to eight
# times to catch state left between evaluations, are the one place where a
# legitimate response takes measurable time.
BATCH_TIMEOUT = 300.0

# How many times a batch may be restarted after a crash before the rest of it is
# written off. Eight allows a handful of genuinely fatal documents inside one batch
# while bounding a pathologically crashy probe at 8 spawns per 400 cases.
MAX_RESUMES = 8

# A single case, run alone. Used when a batch failure has to be attributed and
# when the verification stage runs one candidate document.
SINGLE_TIMEOUT = 30.0


@dataclass
class Response:
    """One parsed response line, kept in both parsed and raw form.

    Both, because the two are graded differently: `raw` is what byte-exact
    comparison uses, and `parsed` is what the report needs in order to say
    something more useful than "these bytes differ" -- an error kind, a message, a
    missing field.
    """

    case_id: str
    raw: bytes
    parsed: dict | None
    malformed: str = ""

    @property
    def ok(self) -> bool:
        return isinstance(self.parsed, dict) and self.parsed.get("ok") is True

    @property
    def kind(self) -> str:
        """The error kind, or "" for a success.

        Reads `error.kind`, which is the payload-level name. Deliberately not
        called `category`: the case-level verdict this suite reports elsewhere
        uses that word for something else, and conflating the two has cost real
        diagnoses before.
        """
        if isinstance(self.parsed, dict):
            err = self.parsed.get("error")
            if isinstance(err, dict):
                return str(err.get("kind", ""))
        return ""

    @property
    def message(self) -> str:
        if isinstance(self.parsed, dict):
            err = self.parsed.get("error")
            if isinstance(err, dict):
                return str(err.get("message", ""))
        return ""


def parse_stream(stream: bytes) -> tuple[dict[str, Response], list[str]]:
    """Split a probe's stdout into responses keyed by the id each echoes.

    Returns the responses and a list of complaints about lines that could not be
    used. A complaint is not a case failure by itself -- the caller decides, since
    a probe that writes a banner to stdout before its first response has one
    unusable line and 400 good ones.

    Ordering is preserved but not judged here. `responses` is an ordinary dict, so
    iterating it yields the ids in the order their lines arrived, while every module
    that only needs answers gets them regardless of the order they came in.

    Response order is graded, but not from this function and not from this process
    shape: `structure._check_probe_answers_in_order` opens its own pipe, because a
    probe that ends up writing its answers in the right order for the wrong reason --
    reading all of stdin first, answering concurrently, flushing as promises settle --
    is indistinguishable here and distinguishable there. A dict this function
    populated after the process exited cannot say when a line arrived.
    """
    responses: dict[str, Response] = {}
    complaints: list[str] = []

    for lineno, line in enumerate(stream.split(b"\n"), 1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except ValueError as exc:
            complaints.append(
                f"line {lineno} is not JSON ({exc.args[0][:80]}): "
                f"{line[:120]!r}")
            continue
        if not isinstance(payload, dict):
            complaints.append(
                f"line {lineno} is JSON but not an object: {line[:120]!r}")
            continue
        if "id" not in payload:
            complaints.append(
                f"line {lineno} has no id, so it cannot be matched to a "
                f"request: {line[:120]!r}")
            continue
        case_id = str(payload["id"])
        if case_id in responses:
            complaints.append(
                f"line {lineno} repeats id {case_id}, which was already "
                f"answered; the first answer is kept")
            continue
        responses[case_id] = Response(
            case_id=case_id, raw=line, parsed=payload)

    return responses, complaints


# What one case may put on the wire. A dict is a request to serialise; a str is a
# line that goes out verbatim; a list of str is several such lines, in order, always
# alone in a process. The alias exists because all four of `encode_request`,
# `answer_id`, `_has_recoverable_id` and `_invoke` have to agree about the set, and
# they drifted apart once already.
Request = dict | str | list


def encode_request(request: Request) -> bytes:
    """The exact bytes one request puts on stdin, newline(s) included.

    Both sides of the differential go through here, and that is the whole reason it
    exists as a function. A freeze step that encoded with `sort_keys=True` and
    default separators while `ProbeRunner._invoke` used compact separators and
    insertion order would ask the reference a differently-serialised question than
    the submission -- and for a case whose expectation is that the error names the
    *first* unexpected field in document order, that difference is the answer. One
    encoder means it cannot disagree anywhere.

    A `str` request is a line that goes on the wire verbatim. The raw protocol cases
    are not JSON objects at all -- `not json`, a truncated `{"id":1,"op":`, a line
    of trailing garbage, an empty line -- and passing them through `json.dumps`
    would wrap each one in quotes, turning a dozen distinct malformed inputs into a
    dozen valid JSON string literals that all test the single thing one case already
    tests. The malformed line has to reach the probe malformed or the family grades
    nothing.

    A `list` is several lines in one process. Ten protocol cases need that, because
    what they assert is a property of the stream: that a bad line draws exactly one
    answer and does not swallow the request behind it, that a repeated id is
    answered twice, that five lines draw five answers in order. Each element is
    encoded by the same rules, so a list may mix a raw line with a JSON object --
    which `_protocol/p0001-then-good` does, and it is the point of it.

    `ensure_ascii=True`, and not as a stylistic choice: two fixture cases ask what
    `$encodeUrl` does with a lone surrogate, so the corpus contains one, and
    `json.dumps(..., ensure_ascii=False)` produces a `str` that `.encode("utf-8")`
    refuses outright -- `surrogates not allowed`. Escaping is not a change to the
    question being asked. `\\ud800` is the only way that codepoint can appear in
    conforming JSON at all, node's parser recovers the same string from it that no
    byte sequence could have carried, and `gen.write_corpus` already writes the
    corpus this way for the same reason, so the two now agree by construction.

    The `str` branch deliberately does not escape: `_protocol/p0001-bom` is a line
    prefixed with a real U+FEFF, and its whole subject is that the probe receives
    those bytes and rejects them. Escaping it would send a valid request with a
    strange first key instead.
    """
    if isinstance(request, list):
        return b"".join(encode_request(item) for item in request)
    if isinstance(request, str):
        return request.encode("utf-8") + b"\n"
    blob = json.dumps(request, separators=(",", ":"), ensure_ascii=True)
    return blob.encode("utf-8") + b"\n"


def answer_id(request: Request) -> str:
    """The id a request will be answered under.

    Not the same as `request["id"]`. The probe reports the id it recovered, and when
    it recovered none -- no `id` key, a non-integer one, a line that is not JSON at
    all -- it reports `0`, measured against the reference. Matching on the sent
    value would leave those cases permanently unanswered and report them as crashes.

    A raw line and a multi-line case both answer under 0 here, for the same reason:
    nothing can be predicted about a line the probe could not parse, and a
    multi-line case has no single answer to predict an id for.

    This is a *prediction*, and it is only used where being wrong is harmless or
    impossible: deciding what may share a batch, and naming the victim in a resume
    warning. It is deliberately not used to pair answers with cases -- it cannot.
    Two requests can share a predicted id, and one request can be answered under an
    id this does not predict (`_protocol/dup-id-key` is a JSON object with a
    repeated `id` key, so the probe takes the last and answers under that while this
    returns "0"). Pairing goes through `BatchOutcome.ordered`.
    """
    if isinstance(request, (str, list)):
        return "0"
    value = request.get("id")
    if isinstance(value, bool) or not isinstance(value, int):
        return "0"
    return str(value)


def _has_recoverable_id(request: Request) -> bool:
    """Whether this request can share a batch.

    False for anything answered under the fallback id, since several of those in one
    batch collide. `0` as an explicit id is also excluded: it is indistinguishable
    on the wire from the fallback, and the corpus generator is free to use it
    deliberately as a boundary value. Raw lines and multi-line cases are never
    batchable -- the first all answer under 0, and the second have no one answer at
    all.
    """
    if isinstance(request, (str, list)):
        return False
    value = request.get("id")
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    return value != 0


@dataclass
class BatchOutcome:
    """What one process invocation produced.

    Two views of the same answers, because neither alone can pair every case with
    its response:

    `ordered` is positional -- `ordered[i]` answers `requests[i]`, or is None if
    that request went unanswered. This is the view callers should grade against,
    and it is the view that matches how the expectations were captured:
    `freeze.capture` drives the reference through this same class and pairs
    `ordered` to cases by position, asserting the counts are equal. Both sides of
    every differential in this suite therefore agree on what answers what, including
    the two cases where that is not obvious -- a solo request paired by position, and
    a multi-line protocol case whose answer is the whole of stdout.

    For a solo request `ordered[0]` may hold *several* response lines: a multi-line
    protocol case is answered by everything the process wrote, and splitting that
    into one `Response` per line would discard the only thing the case asserts --
    how many answers came back and in what order.

    `responses` is keyed by the id each response echoed. It is what makes crash
    attribution possible -- after a batch dies partway, the ids present say which
    requests were reached -- and it is what a caller reads when the question is "did
    anything answer under this id at all". It cannot pair every case, and that is not
    a fixable property of it: many protocol cases are answered under id 0 because the
    probe could not recover an id from them, so they share one key no matter how few
    run per process.

    It cannot be graded through, and that is the reason `ordered` exists.
    `_has_recoverable_id` already runs every id-0 case in its own process, which stops
    them colliding *within* a process and does nothing about the merge *across*
    processes: each one lands under key "0" and overwrites the last, so all but the
    final one would be compared against a neighbour's answer. The cost scales with how
    many cases share the key rather than with how many run per process, and this task
    has 117 protocol cases.
    """

    responses: dict[str, Response] = field(default_factory=dict)
    ordered: list[Response | None] = field(default_factory=list)
    complaints: list[str] = field(default_factory=list)
    crashed: bool = False
    returncode: int = 0
    stderr_tail: str = ""
    spawns: int = 1


class ProbeRunner:
    """Drives one probe.

    `prefix` is an argv list, not a path. Here both sides happen to be `["node",
    "<somewhere>/dist/probe.js"]`, but the indirection is still load-bearing: the two
    relocation cases in `structure` invoke the same build through a different path,
    and stage 3 drives two trees at once. A runner that took a directory and appended
    `dist/probe.js` itself could express none of that.

    Everything downstream of the prefix is identical between invocations, which is the
    property that makes the comparison meaningful.
    """

    def __init__(
        self,
        prefix: list[str],
        cwd: Path,
        log: Log,
        env: dict[str, str] | None = None,
        label: str = "probe",
    ) -> None:
        self.prefix = list(prefix)
        self.cwd = Path(cwd)
        self.log = log
        self.env = env if env is not None else vlib.base_env()
        self.label = label
        self.spawns = 0

    # -- one invocation ----------------------------------------------------

    @staticmethod
    def _solo_answer(request: Request, stdout: bytes,
                     responses: dict[str, Response],
                     complaints: list[str]) -> Response | None:
        """The answer to a request that had a process to itself.

        Two rules, because the two kinds of solo case assert different things:

        A single-line case is answered by the first response line in stream order,
        parsed, so `compare` can say *which part* of a JSON object diverged instead of
        showing two hundred bytes of diff. Extra lines beyond the first are a
        complaint rather than part of the answer -- a probe that also wrote a banner
        has one bad line and one good one, and grading the banner as part of the
        response would report the wrong defect.

        A multi-line case is answered by **stdout verbatim**, newlines and all. That
        is the whole point of those cases: `_protocol/p0001-then-good` asserts that a
        malformed line draws exactly one answer and does not consume the good request
        behind it, and that claim exists only in the relationship between two lines.
        Parsing them into separate responses and grading the first would silently
        reduce the case to "a bad line draws an error", which a dozen single-line
        cases already grade.

        Verbatim includes the trailing newline, so a probe that writes its last
        response without one differs here. Nothing else in the suite can see that: a
        parsed response never carries its own terminator.

        A multi-line case also discards `parse_stream`'s complaints about repeated
        ids, and that is not a convenience. Several of these cases send two lines the
        probe cannot recover an id from, so two answers under id 0 is the observation
        they were written to make. `parse_stream` is right to say a dict cannot hold
        both; it is wrong to call it a problem here, and leaving the complaint in
        would put three lines of noise into every grading run of a passing submission
        -- which is how a report stops being read.
        """
        if isinstance(request, list):
            complaints[:] = [c for c in complaints if "repeats id" not in c]
            if not stdout:
                return None
            first_line = stdout.split(b"\n", 1)[0]
            parsed: dict | None = None
            try:
                candidate = json.loads(first_line)
                if isinstance(candidate, dict):
                    parsed = candidate
            except ValueError:
                parsed = None
            return Response(case_id="0", raw=stdout, parsed=parsed)
        if len(responses) > 1:
            complaints.append(
                f"one request produced {len(responses)} responses "
                f"(ids {sorted(responses)}); the first in stream order is the answer "
                f"and the rest are extra output")
        return next(iter(responses.values()), None)

    def _invoke(self, requests: list[Request]) -> BatchOutcome:
        """Feed `requests` to one process and read back whatever it wrote."""
        if len(requests) > 1 and any(isinstance(r, list) for r in requests):
            # A multi-line case's answer is the whole of stdout, so it cannot share a
            # process with anything: another request's answer would be folded into it.
            # `run_all` routes them to solo chunks; this is the assertion that says so
            # rather than trusting it, because the failure mode is a case that passes
            # or fails for a reason nothing in the report mentions.
            raise AssertionError(
                f"{self.label}: a multi-line request was put in a batch of "
                f"{len(requests)}; multi-line cases must run alone. See "
                f"_has_recoverable_id and run_all.")
        stdin = b"".join(encode_request(r) for r in requests)

        self.spawns += 1
        proc = vlib.run(
            self.prefix,
            cwd=self.cwd,
            env=self.env,
            stdin_data=stdin,
            timeout=BATCH_TIMEOUT,
            log=self.log,
            label=f"{self.label} x{len(requests)}",
            # The probe's stdout is the measurement, not a log, so it must not
            # be capped. Without this, `vlib.run` kept only the first 256 KiB
            # and the rest of the batch vanished: the cut landed mid-line, so
            # one answer failed to parse and every answer after it was simply
            # absent. Those cases were then reported as "no response for this
            # id (the probe crashed or skipped it)" alongside "the probe died
            # mid-batch (exit 0)" -- the exit 0 being the tell that the process
            # was fine and the capture was not. It cost 25 cases in the `limit`
            # and `query` families of `main`, whose documents are large by
            # design, so every submission would have hit it and been blamed.
            full_capture=True,
        )
        self.log.record(proc, f"{self.label} batch of {len(requests)}")

        responses, complaints = parse_stream(proc.stdout)

        # Pair answers to requests. One request in a process is paired by position:
        # whatever it answered is the answer, whatever id that answer carries.
        # Predicting the id instead is what breaks -- `answer_id` returns "0" for a
        # raw line, but a line like `{"id":1,"id":2,"op":"hello"}` is valid JSON with
        # a repeated key, so the probe parses it last-wins and answers under id 2.
        # The frozen expectation is `{"id":2,...}` and correct; only the lookup would
        # be wrong. Position cannot be wrong here, because there is one of each.
        #
        # Several requests share a process only when every id is unique and
        # recoverable (`run_all` enforces it), so id-keying is exact there, and it is
        # the only thing that survives a crash: the ids present are how the resume
        # loop learns which requests were reached.
        if len(requests) == 1:
            ordered: list[Response | None] = [
                self._solo_answer(requests[0], proc.stdout, responses, complaints)]
        else:
            ordered = [responses.get(answer_id(r)) for r in requests]

        # A non-zero exit is only interesting if answers are missing. A probe that
        # answered everything and then exited 1 while flushing has produced usable
        # evidence, and voiding it would grade the teardown instead of the parser.
        missing = [r for r, resp in zip(requests, ordered) if resp is None]
        return BatchOutcome(
            responses=responses,
            ordered=ordered,
            complaints=complaints,
            crashed=bool(missing),
            returncode=proc.returncode,
            stderr_tail=proc.tail(lines=20, limit=2000),
            spawns=1,
        )

    # -- a batch, with resume ---------------------------------------------

    def run_batch(self, requests: list[Request]) -> BatchOutcome:
        """Run `requests`, restarting after a crash to reach the cases behind it.

        The victim of a crash is the first request with no answer. It is dropped
        and the remainder re-driven, so one fatal document costs one case. Cases
        the probe never got to are collected on the next pass rather than being
        marked failed, which is the difference between a report that says "this
        document kills the parser" and one that says the port is empty.
        """
        merged = BatchOutcome(spawns=0)
        # Answers are accumulated against their index in `requests`, not against
        # the shrinking `pending` list, so a resume cannot shift the pairing of
        # the cases it skipped past.
        answers: list[Response | None] = [None] * len(requests)
        pending = list(range(len(requests)))
        resumes = 0

        while pending:
            outcome = self._invoke([requests[i] for i in pending])
            merged.spawns += outcome.spawns
            merged.responses.update(outcome.responses)
            merged.complaints.extend(outcome.complaints)
            if outcome.stderr_tail and not merged.stderr_tail:
                merged.stderr_tail = outcome.stderr_tail
            for index, response in zip(pending, outcome.ordered):
                if response is not None:
                    answers[index] = response

            unanswered = [i for i in pending if answers[i] is None]
            if not unanswered:
                break

            merged.crashed = True
            merged.returncode = outcome.returncode
            victim = unanswered[0]
            if resumes >= MAX_RESUMES:
                self.log.write(
                    f"WARN {self.label}: gave up resuming after {resumes} "
                    f"crashes; {len(unanswered)} of {len(requests)} cases in "
                    f"this batch were not reached (first unreached: "
                    f"{answer_id(requests[victim])})")
                break
            resumes += 1
            self.log.write(
                f"WARN {self.label}: no answer for id "
                f"{answer_id(requests[victim])} (exit {outcome.returncode}); "
                f"resuming after it, {len(unanswered) - 1} cases still to reach")
            pending = unanswered[1:]

        merged.ordered = answers
        return merged

    def run_all(self, requests: list[Request]) -> BatchOutcome:
        """Run every request, in batches of `BATCH_SIZE`.

        Requests are batched by the id they will be *answered* under, which is not
        always the id they were sent with. A protocol case that omits its id is
        answered under 0 -- measured against the reference, which reports the id it
        recovered and has recovered none at that point. Two of those in one batch are
        indistinguishable, so anything whose answering id is not unique and
        recoverable runs alone, and that is also what keeps every multi-line case in a
        process of its own.

        Running alone is necessary but not sufficient. The answers still have to be
        *reported* per request, which is why `ordered` exists and why callers should
        grade against it: a single dict keyed by answering id collapses every id-0
        case onto one key, and running each in its own process does nothing to stop
        that.
        """
        merged = BatchOutcome(spawns=0)
        answers: list[Response | None] = [None] * len(requests)
        batchable: list[int] = []
        solo: list[int] = []
        for index, request in enumerate(requests):
            (batchable if _has_recoverable_id(request) else solo).append(index)

        # Batching by id is only exact while the ids in a chunk are distinct.
        # `gen._assign_wire_ids` numbers each stem 1..N so they are, but that is a
        # property of another module: assert it here rather than depend on it
        # quietly, because the failure mode is a dropped duplicate answer that
        # reads as a crashed case.
        seen: dict[str, int] = {}
        for index in batchable:
            key = answer_id(requests[index])
            if key in seen:
                raise AssertionError(
                    f"{self.label}: requests {seen[key]} and {index} are both "
                    f"answered under id {key}, so one answer would overwrite the "
                    f"other. Batchable ids must be unique; see "
                    f"gen._assign_wire_ids.")
            seen[key] = index

        chunks = [batchable[i:i + BATCH_SIZE]
                  for i in range(0, len(batchable), BATCH_SIZE)]
        chunks.extend([[index] for index in solo])

        for chunk in chunks:
            outcome = self.run_batch([requests[i] for i in chunk])
            merged.spawns += outcome.spawns
            merged.responses.update(outcome.responses)
            merged.complaints.extend(outcome.complaints)
            merged.crashed = merged.crashed or outcome.crashed
            for index, response in zip(chunk, outcome.ordered):
                answers[index] = response
            if outcome.stderr_tail and not merged.stderr_tail:
                merged.stderr_tail = outcome.stderr_tail
            if outcome.returncode and not merged.returncode:
                merged.returncode = outcome.returncode

        merged.ordered = answers
        return merged

    def run_one(self, request: Request) -> Response | None:
        """Run a single request alone. Used by the verification stage."""
        return self._invoke([request]).ordered[0]


# -- comparison ------------------------------------------------------------
#
# Byte-exact, unconditionally. There is no per-op tolerance and no field the
# comparison skips, which is only defensible because the probe removed the one
# genuinely unportable value at its source: `panicCause` maps each reachable thrown
# object to a closed set of tokens, so an expectation never carries a host engine's
# own wording. The corpus reaches exactly two of those tokens, `type` and `error`,
# and `catalog.PANIC_CAUSES` is the assertion that no third one appeared.
#
# The alternative -- comparing parsed JSON, or normalising key order -- would throw
# away most of what this task grades. jsonata decides what its results *are*: which
# singleton collapses to a scalar, which empty sequence becomes nothing at all, what
# order the keys of a constructed object come out in. Those decisions are the port's
# hardest problem, and the wire format exists to make them visible: values travel as
# tagged pairs and objects as ordered key/value lists precisely so that a comparison
# on bytes is a comparison on semantics rather than on JSON serialisation.
#
# Message *text* is the one thing not graded, and it is excluded at the source rather
# than here: the corpus asks for an error's code, position and token, which are API,
# and never its prose, which a faithful reimplementation is free to reword.


def compare(expected: bytes, actual: Response | None) -> tuple[bool, str, str]:
    """Grade one case. Returns (passed, detail, diff).

    `detail` explains the failure in the vocabulary of the thing that failed --
    a missing response, a differing error kind, differing bytes -- because
    "expected X got Y" on two 4KB JSON lines is not a diagnosis.
    """
    if actual is None:
        return False, "no response for this id (the probe crashed or skipped it)", ""

    if actual.raw == expected:
        return True, "", ""

    # A multi-line expectation is a whole stdout stream, so the first thing worth
    # saying about it is how many answers came back. "these bytes differ" on two
    # five-line blobs hides the finding that matters most here -- that one side
    # answered four times and the other five -- and the line counts name it before
    # any diff is read.
    if b"\n" in expected.rstrip(b"\n"):
        want_lines = expected.rstrip(b"\n").split(b"\n")
        got_lines = actual.raw.rstrip(b"\n").split(b"\n") if actual.raw else []
        if len(want_lines) != len(got_lines):
            return (False,
                    f"the stream drew {len(got_lines)} response line(s), expected "
                    f"{len(want_lines)}",
                    vlib.unified_diff(expected, actual.raw))
        first = next((i for i, (a, b) in enumerate(zip(want_lines, got_lines))
                      if a != b), 0)
        return (False,
                f"{len(want_lines)} response lines, differing from line {first + 1}",
                vlib.unified_diff(expected, actual.raw))

    # Both are JSON objects; say which part diverged before showing bytes.
    exp_parsed: dict | None
    try:
        exp_parsed = json.loads(expected)
    except ValueError:
        exp_parsed = None

    detail = "response bytes differ"
    if isinstance(exp_parsed, dict) and isinstance(actual.parsed, dict):
        exp_ok = exp_parsed.get("ok")
        act_ok = actual.parsed.get("ok")
        if exp_ok != act_ok:
            detail = (
                f"expected ok={exp_ok!r}, got ok={act_ok!r}"
                + (f" ({actual.kind}: {actual.message[:200]})"
                   if act_ok is False else ""))
        elif exp_ok is False:
            exp_err = exp_parsed.get("error") or {}
            exp_kind = exp_err.get("kind", "") if isinstance(exp_err, dict) else ""
            if exp_kind != actual.kind:
                detail = (f"expected error kind {exp_kind!r}, "
                          f"got {actual.kind!r}")
            else:
                detail = f"same error kind ({exp_kind}), different payload"
        else:
            detail = "both succeeded, but the results differ"

    return False, detail, vlib.unified_diff(expected, actual.raw)


def request_for(case: dict) -> Request:
    """Build the wire request for a case.

    Ordinary cases carry a ready-made `request` object holding exactly the fields
    their op takes, and this prepends the wire id rather than rebuilding the object
    field by field. That matters because the protocol treats an *unexpected* field as
    a ProtocolError: a builder that copied a fixed set of keys would either drop a
    field some op needs or add one another op rejects, and either way the case would
    fail against its own frozen expectation rather than against the port.

    The id goes first so that a case whose subject is field order -- which error a
    request with two problems reports -- sees the same document order on both sides.
    `gen` writes each `request` with sorted keys and `encode_request` preserves
    insertion order, so the bytes are fixed by these two lines together.

    Protocol cases return their raw lines instead: a `list[str]`, meaning "these lines
    go on the wire exactly as they are, in this order, in one process". They carry
    their ids inside the lines, or carry none at all, which is the subject of half of
    them.

    The protocol branch comes first and has to: an ordinary case has a `request` key
    and a protocol case has none, so building the dict below on a protocol case is a
    KeyError that reads like a generator bug and is not one.
    """
    if "raw_lines" in case:
        return list(case["raw_lines"])
    return {"id": case["wire_id"], **case["request"]}
