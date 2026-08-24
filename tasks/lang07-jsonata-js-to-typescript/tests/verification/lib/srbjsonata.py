"""What a stage-3 candidate is given to attack the engine under test.

A candidate is a pytest file.  It may import this module and the standard library,
and nothing else.  There is one way in -- a probe that speaks one JSON request per
line -- and that is deliberate: the whole point of the stage is that "the original
passes this test" and "the submission fails it" are the same sentence about the
same instrument.  Both trees are reached as `node <somewhere>/dist/probe.js`.
Nothing here says which tree is which, and nothing here can tell you.

    op("eval", "1+1")             -> Record       one request, fields checked
    probe([{...}, ...])           -> {id: Record} one batch, exactly as written
    ops() / impls() / engines()   -> list[str]    the closed vocabularies
    ABSENT                                        "this field is not present"
    SCRATCH                                       a writable directory of your own

Why a candidate cannot reach a compiler
---------------------------------------
The tree under test is built once, before any candidate runs, by
`run-candidate.sh`.  This module holds the argv of the built probe and no way to
produce another one.  A candidate that wants to know what the engine does asks the
engine; there is no path from here to `tsc` and none to either tree's source.

That last clause is load-bearing on this task in a way it is not on the others in
this family.  Both trees are JavaScript by the time they run, so a candidate that
read a tree's source and `require`d it would be comparing an implementation
against itself and would pass every mechanical condition while establishing
nothing.  The submission's half of the stage runs as an unprivileged user that
cannot read either the published reference or the harness's staged trees, so this
is a property of the filesystem rather than a rule stated here -- but it is stated
here too, because a candidate that tries it should know why it failed.

Why the request encoder is imported rather than written
------------------------------------------------------
`executor.encode_request` is the same function stage 2 uses to serialise every one
of its 13,940 frozen cases, and this module drives the probe through
`executor.ProbeRunner`.  A stage-3 helper that serialised requests its own way
would be asking a differently-spelled question than the one the corpus asked, and
the first place that shows up is key order inside the request object -- which for
this protocol *is* part of the answer, because validation reports the first
offending field in the request's own order.  A candidate should be able to
reproduce a stage-2 case exactly and get the stage-2 answer.

Absent is not null
------------------
`{"op":"eval","expr":"$"}` and `{"op":"eval","expr":"$","input":null}` are
different requests and draw different answers: JSONata distinguishes an absent
input from a null one.  Python cannot express that difference with `None`, so this
module has a sentinel: pass nothing for absent, `None` for JSON null, and
`ABSENT` where you need to say "absent" explicitly.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import executor
import vlib

__all__ = [
    "OPS", "IMPLS", "ENGINES", "OPTION_KEYS", "KINDS", "CLOCKS", "PROTOCOL",
    "REPEAT_MIN", "REPEAT_MAX", "ABSENT", "SCRATCH", "TARGET_NAME",
    "Record", "CandidateError",
    "op", "probe", "batch_of", "ops", "impls", "engines", "tag",
    "func", "assignment",
]

PROTOCOL = "jsonata-probe/1"

# The six ops and the fields each accepts.  `!` marks required.  This mirrors
# State A's own `OP_FIELDS` table, and the mirroring is the point: the table is the
# validator on both sides, so a candidate that wants to know whether a field is
# legal can read it here instead of guessing and getting a `P0006` it did not mean.
OPS = ("hello", "ast", "eval", "evalcb", "assign", "register")

_FIELDS: dict[str, dict[str, str]] = {
    "hello": {},
    "ast": {"expr": "string!", "recover": "boolean"},
    "eval": {
        "expr": "string!", "input": "any", "bindings": "object",
        "options": "object", "clock": "string", "repeat": "integer",
    },
    "evalcb": {
        "expr": "string!", "input": "any", "bindings": "object",
        "options": "object", "clock": "string", "repeat": "integer",
    },
    "assign": {
        "expr": "string!", "input": "any", "options": "object",
        "clock": "string", "repeat": "integer", "assigns": "array!",
    },
    "register": {
        "expr": "string!", "input": "any", "options": "object",
        "clock": "string", "repeat": "integer", "funcs": "array!",
    },
}

# The named fixtures.  These are names on a wire, not values: a regex engine is a
# constructor and a registered function is a closure, and neither can be written
# down in JSON, so the protocol carries a name and the probe looks it up.  A name
# the probe does not know is `P0009`, never a silent default.
IMPLS = (
    "asyncDouble", "concat2", "counter", "describe", "double", "focusInput",
    "focusLookup", "hostDate", "hostMap", "makeAdder", "nothing", "throwing",
    "throwingCode",
)
ENGINES = ("broken", "caseless", "native", "nomatch")

# `options` keys.  Four are JSONata's own; `engine` is this protocol's stand-in
# for `RegexEngine`.
OPTION_KEYS = ("recover", "timeout", "stack", "sequence", "engine")

# How an error is classified.  A JSONata error carries a `code`; a `Panic` carries
# a `cause` and no code.  A `ProtocolError` is this protocol complaining about the
# request rather than the engine complaining about the expression.
KINDS = ("JsonataError", "ProtocolError", "Panic")

# `clock` pins `$now`/`$millis` so a frozen expectation stays true.  `"live"` opts
# out, and a case that opts out is asserting on something that moves.
CLOCKS = ("pinned", "live")

REPEAT_MIN, REPEAT_MAX = 1, 8

class CandidateError(RuntimeError):
    """The candidate asked for something that does not exist.

    Raised instead of letting the request through.  A helper that passed an
    unknown op name, fixture name or option key to the probe would let the probe
    answer "unknown op" or `P0009` -- which are real, graded behaviours -- and the
    candidate would then be measuring its own typo identically on both trees.
    Where that answer is what the candidate wants, it can still have it: build the
    request dict by hand and pass it to `probe()`, which is the documented way to
    ask a malformed question on purpose.
    """


class _Absent:
    """The type of `ABSENT`.  One instance, and it is not `None`."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return "ABSENT"

    def __bool__(self) -> bool:
        return False


#: "This field is not in the request at all", as distinct from `None`, which is
#: JSON `null`.  The distinction is not decoration: `{"op":"eval","expr":"$"}` and
#: the same request with `"input":null` evaluate differently, because JSONata
#: treats an absent input and a null one as different things.  Every optional
#: field in `op()` defaults to `ABSENT`, so passing nothing omits the key.
ABSENT = _Absent()

#: An opaque per-run label for the tree under test.  It is a hash, not
#: "original"/"submission": `assert TARGET_NAME == "original"` would pass on one
#: tree, fail on the other and reproduce every time -- meeting every mechanical
#: condition for a break while establishing nothing about the migration.  It is
#: here so that two failure messages from one comparison stay distinguishable.
TARGET_NAME = os.environ.get("SRB_TARGET_TOKEN", "?")

#: Yours to write in.  Emptied between (candidate, tree) pairs, so a file left
#: here by a candidate's run against one tree is not present for its run against
#: the other.  Writing anywhere else -- above all inside the tree -- changes the
#: answer a later candidate in the same round gets.
SCRATCH = Path(os.environ.get("SRB_SCRATCH", "/tmp/srb-candidate"))


def _argv() -> tuple[list[str], Path]:
    """The probe's argv prefix and working directory, from run-candidate.sh."""
    raw = os.environ.get("SRB_PROBE_ARGV")
    cwd = os.environ.get("SRB_PROBE_CWD")
    if not raw or not cwd:
        raise RuntimeError(
            "SRB_PROBE_ARGV/SRB_PROBE_CWD are not set. This module only works "
            "inside a stage-3 candidate run; run-candidate.sh sets them after "
            "building the tree under test."
        )
    argv = json.loads(raw)
    if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
        raise RuntimeError(f"SRB_PROBE_ARGV is not a list of strings: {raw!r}")
    return argv, Path(cwd)


#: The ten value tags.  Every value the probe reports travels as a two-element
#: `[tag, payload]` array, because four of the things JSONata can evaluate to have
#: no JSON spelling: `undefined` (which is not `null`), the three non-finite
#: numbers and `-0` (which `JSON.stringify` renders as `0`), and the sequence flag
#: on an array.  `tag(value)` reads the first element; the payload's shape depends
#: on it, and `["o", ...]` is a list of `[key, value]` pairs rather than an object
#: so that key order stays observable and a key named `u` stays distinguishable
#: from a tag.
TAGS = ("u", "z", "b", "d", "s", "a", "q", "o", "f", "x")


def tag(value: Any) -> str:
    """The tag of an encoded value, or "" if it is not one.

    `tag(rec.value) == "q"` says "a sequence-flagged array", which is a different
    answer from `"a"` and is exactly the kind of difference a port loses.
    """
    if isinstance(value, list) and value and isinstance(value[0], str):
        return value[0]
    return ""


@dataclass
class Record:
    """One response.

    `raw` is the response line as bytes, which is what byte-exact comparison uses.
    Everything else is a view onto it, for writing an assertion that says what it
    means: `.results[0]` rather than a slice of a JSON blob.
    """

    id: int
    raw: bytes
    parsed: dict | None

    # -- the success half --------------------------------------------------
    @property
    def ok(self) -> bool:
        return isinstance(self.parsed, dict) and self.parsed.get("ok") is True

    @property
    def op(self) -> str:
        """The echoed op name, or "".

        This protocol echoes `op` in every response, success or failure.  A port
        that drops it, or that echoes the op it decided to run rather than the one
        it was asked for, differs here.
        """
        if isinstance(self.parsed, dict):
            return str(self.parsed.get("op", ""))
        return ""

    @property
    def result(self) -> Any:
        """The `result` payload, or None on an error response."""
        if isinstance(self.parsed, dict):
            return self.parsed.get("result")
        return None

    @property
    def results(self) -> list:
        """`result.results` -- one encoded value per `repeat`, in order.

        The four evaluating ops always answer with a list, even at the default
        `repeat` of 1.  Asking for more than one is how a candidate observes state
        that leaks between evaluations of a single compiled expression.
        """
        res = self.result
        if isinstance(res, dict) and isinstance(res.get("results"), list):
            return res["results"]
        return []

    @property
    def value(self) -> Any:
        """The first repeat's encoded value, or None.

        A convenience for the common case.  `.results` is the honest shape and a
        candidate comparing repeats must use it.
        """
        vals = self.results
        return vals[0] if vals else None

    @property
    def registered(self) -> list:
        """`result.registered` on a `register` response -- names, in order."""
        res = self.result
        if isinstance(res, dict) and isinstance(res.get("registered"), list):
            return res["registered"]
        return []

    @property
    def ast(self) -> Any:
        """`result.ast` on an `ast` response, or None."""
        res = self.result
        return res.get("ast") if isinstance(res, dict) else None

    @property
    def errors(self) -> Any:
        """`result.errors` on an `ast` response.

        `null` when the parse was clean, a list when `recover` was set and the
        parser recovered.  `null` and `[]` are different answers.
        """
        res = self.result
        return res.get("errors") if isinstance(res, dict) else None

    # -- the error half ----------------------------------------------------
    @property
    def error(self) -> dict | None:
        if isinstance(self.parsed, dict):
            err = self.parsed.get("error")
            if isinstance(err, dict):
                return err
        return None

    @property
    def kind(self) -> str:
        """`JsonataError`, `ProtocolError`, `Panic`, or "" for a success.

        The field is `error.kind`.  Named `kind` here and not `category`: the
        behavioural suite uses that second word for a case-level verdict, and the
        two have been conflated before at the cost of two wrong diagnoses.
        """
        err = self.error
        return str(err.get("kind", "")) if err else ""

    @property
    def code(self) -> str:
        """`error.code` -- a JSONata code like `T1006`, or a `P000x`.

        A `Panic` has no code and reports "" here; it carries `.cause` instead.
        """
        err = self.error
        return str(err.get("code", "")) if err else ""

    @property
    def message(self) -> str:
        err = self.error
        return str(err.get("message", "")) if err else ""

    @property
    def position(self) -> Any:
        """`error.position`, or None when the error carries none."""
        err = self.error
        return err.get("position") if err else None

    @property
    def token(self) -> Any:
        """`error.token`, or None when the error carries none."""
        err = self.error
        return err.get("token") if err else None

    @property
    def cause(self) -> Any:
        """`error.cause` -- present on a `Panic` and nowhere else."""
        err = self.error
        return err.get("cause") if err else None

    @property
    def keys(self) -> list[str]:
        """The response's own top-level keys, in the order they were written.

        Order is part of the answer here, and `.raw` is where it is decided; this
        is the readable way to assert on it without a byte comparison.
        """
        return list(self.parsed.keys()) if isinstance(self.parsed, dict) else []

    # -- raw ---------------------------------------------------------------
    def text(self, errors: str = "replace") -> str:
        """The response line as text.  For an assertion message."""
        return self.raw.decode("utf-8", errors)

    def __str__(self) -> str:
        return f"<Record id={self.id} ok={self.ok} {self.text()[:200]}>"


def ops() -> list[str]:
    """The six declared op names."""
    return list(OPS)


def impls() -> list[str]:
    """The thirteen registrable implementations, by name."""
    return list(IMPLS)


def engines() -> list[str]:
    """The four named regex engines."""
    return list(ENGINES)


def func(name: str, impl: str, signature: str | None | _Absent = ABSENT) -> dict:
    """One entry for `register`'s `funcs` array.

    `signature` may be a string, `None` (an explicit JSON null, which is a legal
    value the protocol accepts and not the same request as omitting it), or left
    off entirely.
    """
    if impl not in IMPLS:
        raise CandidateError(
            f"no implementation named {impl!r}. Known: {', '.join(IMPLS)}. "
            f"To send an unknown name on purpose -- the answer is `P0009` -- "
            f"build the dict yourself and use probe()."
        )
    entry: dict[str, Any] = {"name": name, "impl": impl}
    if not isinstance(signature, _Absent):
        entry["signature"] = signature
    return entry


def assignment(name: str, value: Any) -> dict:
    """One entry for `assign`'s `assigns` array.

    `value` is any JSON value, `None` included: `{"name":"k","value":null}` binds
    `$k` to null, which is a different binding from not binding it.
    """
    return {"name": name, "value": value}


def _check_options(options: Any) -> None:
    """Refuse an options object the protocol would reject for a spelling reason."""
    if not isinstance(options, dict):
        raise CandidateError(
            f"options must be a dict, got {type(options).__name__}. To send a "
            f"wrong type on purpose -- `P0007` -- use probe()."
        )
    for key in options:
        if key not in OPTION_KEYS:
            raise CandidateError(
                f"unknown option key {key!r}. Known: {', '.join(OPTION_KEYS)}. "
                f"To send an unknown key on purpose -- `P0006` -- use probe()."
            )
    engine = options.get("engine")
    if isinstance(engine, str) and engine not in ENGINES:
        raise CandidateError(
            f"no regex engine named {engine!r}. Known: {', '.join(ENGINES)}. "
            f"To send an unknown name on purpose -- `P0009` -- use probe()."
        )


def batch_of(requests: list[dict]) -> list[dict]:
    """Validate a list of request dicts without running them.

    Exposed because a candidate that is about to spend a `probe()` call on twelve
    requests would rather learn about a bad op name now.
    """
    seen: dict[int, int] = {}
    for i, request in enumerate(requests):
        if not isinstance(request, dict):
            raise CandidateError(f"request {i} is not a dict: {request!r}")
        if "id" not in request:
            raise CandidateError(
                f"request {i} has no id. `probe()` returns its answers keyed by "
                f"id, so a request without one has nowhere to go -- the reference "
                f"answers it under 0, and two of those in one batch would land on "
                f"the same key. Asserting on that behaviour is legitimate; do it "
                f"with a single-request batch.")
        wire_id = request["id"]
        if isinstance(wire_id, int) and not isinstance(wire_id, bool):
            if wire_id in seen:
                raise CandidateError(
                    f"requests {seen[wire_id]} and {i} share id {wire_id}. "
                    f"`probe()` keys its result by id, so one answer would "
                    f"overwrite the other and the loss would read as the probe "
                    f"having answered nothing. Give each request a distinct id, "
                    f"or send them in separate batches.")
            seen[wire_id] = i
    return list(requests)


class _QuietLog(vlib.Log):
    """`vlib.Log` with the stderr half removed.

    The base class writes every command to stderr as well as to its file, which is
    right for a stage-2 module whose stderr *is* the verifier log.  Here stderr is
    what pytest shows the adversary when an assertion fails, and a line per probe
    invocation would bury the assertion under the harness.  The file keeps
    everything.
    """

    def write(self, message: str) -> None:
        if self._fh:
            self._fh.write(message + "\n")
            self._fh.flush()


def _run(requests: list[dict | str]) -> list[executor.Response | None]:
    """One answer slot per request, in order. None where nothing came back.

    Positional rather than keyed by answering id: a request the probe could not
    read an id from is answered under 0, and so is every other one like it, so a
    dict cannot hold two of them.  `batch_of` keeps a candidate out of that shape
    for the requests it can check, but the pairing should not depend on the check.
    """
    argv, cwd = _argv()
    log = _QuietLog(SCRATCH / "probe.log")
    try:
        runner = executor.ProbeRunner(argv, cwd=cwd, log=log,
                                      env=vlib.base_env(), label="candidate")
        return runner.run_all(requests).ordered
    finally:
        log.close()


def probe(requests: list[dict]) -> dict[int, Record]:
    """Run a batch.  Returns the responses keyed by the id you sent.

    Keyed by the id in the request, not by the id the response echoed -- those
    differ when the probe could not read the one you sent, and pairing on the
    echo cannot hold two answers that both come back under 0.  Answers are
    matched to requests by position internally, so the key is a label for you
    rather than the thing that did the matching.

    The requests go on the wire exactly as given, in the key order you wrote them:
    this is the call to use for a request that is malformed on purpose -- an
    unknown op, a missing field, an extra one, two bad fields in either order.
    `op()` is the checked convenience on top of it.

    A response that never arrived is simply absent from the returned dict.  That
    is not smoothed over with a placeholder, because "the probe answered nothing
    for id 3" and "the probe answered id 3 with an error" are different findings
    and a candidate should not have to squint at a sentinel to tell them apart.

    One line per request, one line per response, one process for the batch.  A
    batch is therefore also how a candidate asks whether anything survives between
    requests: an expression compiled for request 1 is gone by request 2, but a
    `counter` fixture registered in request 1 and a global the engine keeps are
    not necessarily.  Where the subject is state, put the requests in one batch --
    and where it is not, remember that a batch shares state and give a case that
    must start clean its own.
    """
    batch_of(requests)
    responses = _run(list(requests))
    out: dict[int, Record] = {}
    for request, response in zip(requests, responses):
        if response is None:
            continue
        wire_id = request.get("id")
        key = wire_id if isinstance(wire_id, int) else int(
            executor.answer_id(request))
        out[key] = Record(id=key, raw=response.raw, parsed=response.parsed)
    return out


def op(name: str,
       expr: str | _Absent = ABSENT,
       *,
       input: Any = ABSENT,
       bindings: Any = ABSENT,
       options: Any = ABSENT,
       clock: Any = ABSENT,
       repeat: Any = ABSENT,
       recover: Any = ABSENT,
       assigns: Any = ABSENT,
       funcs: Any = ABSENT,
       id: int = 1) -> Record:
    """Run one request and return its response.

    Sends exactly the fields the named op takes, in the protocol's own order, and
    omits every argument left at `ABSENT`.  Anything the op does not take raises
    rather than reaching the probe, so a candidate cannot spend a round on its own
    typo -- an extra field is a `P0006` on both trees and finds nothing.

    `input=None` sends JSON `null`; leaving `input` off sends no `input` at all.
    Those are different requests and JSONata answers them differently.

        op("hello")
        op("ast", "a.b", recover=True)
        op("eval", "$sum(v)", input={"v": [1, 2]})
        op("eval", "$", input=None)                       # null, not absent
        op("eval", "$random()", options={"engine": "native"}, repeat=4)
        op("assign", "$k", assigns=[assignment("k", 7)])
        op("register", "$d(4)", funcs=[func("d", "double")])
    """
    if name not in _FIELDS:
        raise CandidateError(
            f"unknown op {name!r}. The declared ops are: {', '.join(OPS)}. To "
            f"ask the probe about an undeclared op on purpose -- which is a "
            f"graded behaviour, `P0005` -- build the request yourself and pass "
            f"it to probe([...]).")
    fields = _FIELDS[name]
    supplied = {
        "expr": expr, "input": input, "bindings": bindings, "options": options,
        "clock": clock, "repeat": repeat, "recover": recover,
        "assigns": assigns, "funcs": funcs,
    }

    for field, value in supplied.items():
        if isinstance(value, _Absent):
            continue
        if field not in fields:
            takes = ", ".join(fields) or "no fields at all"
            raise CandidateError(
                f"op {name!r} takes {takes}, not {field!r}. An unexpected field "
                f"is a ProtocolError rather than something ignored, so this "
                f"request would fail identically on both trees. To send it on "
                f"purpose, use probe([...]).")
    for field, spec in fields.items():
        if spec.endswith("!") and isinstance(supplied[field], _Absent):
            raise CandidateError(
                f"op {name!r} requires {field!r}. Omitting it is a `P0008` on "
                f"both trees; to assert on that, use probe([...]).")

    if not isinstance(options, _Absent):
        _check_options(options)
    if not isinstance(repeat, _Absent):
        if not isinstance(repeat, int) or isinstance(repeat, bool) \
                or not REPEAT_MIN <= repeat <= REPEAT_MAX:
            raise CandidateError(
                f"repeat must be an integer in {REPEAT_MIN}..{REPEAT_MAX}, got "
                f"{repeat!r}. Out of range is a `P0007` on both trees; to assert "
                f"on that, use probe([...]).")
    if not isinstance(clock, _Absent) and clock not in CLOCKS:
        raise CandidateError(
            f"clock must be one of {', '.join(CLOCKS)}, got {clock!r}. Anything "
            f"else is a `P0007` on both trees; to assert on that, use probe().")
    if not isinstance(funcs, _Absent):
        if not isinstance(funcs, list):
            raise CandidateError(
                f"funcs must be a list of {{name, impl, signature?}} dicts, got "
                f"{type(funcs).__name__}. Build one entry with func().")
        for entry in funcs:
            if isinstance(entry, dict) and entry.get("impl") not in IMPLS:
                raise CandidateError(
                    f"no implementation named {entry.get('impl')!r}. Known: "
                    f"{', '.join(IMPLS)}. To send an unknown name on purpose -- "
                    f"`P0009` -- use probe([...]).")
    if not isinstance(assigns, _Absent) and not isinstance(assigns, list):
        raise CandidateError(
            f"assigns must be a list of {{name, value}} dicts, got "
            f"{type(assigns).__name__}. Build one entry with assignment().")

    # Written in the table's order, because the protocol reports the *first*
    # offending field in the request's own order and a candidate reproducing a
    # stage-2 case should get the stage-2 answer.
    request: dict[str, Any] = {"id": id, "op": name}
    for field in fields:
        if not isinstance(supplied[field], _Absent):
            request[field] = supplied[field]

    answers = probe([request])
    if id not in answers:
        raise AssertionError(
            f"the probe returned no response for id {id} (op {name!r}). It "
            f"crashed or wrote nothing; see {SCRATCH / 'probe.log'}. If the "
            f"expression is meant to kill the process -- an unrecoverable stack "
            f"overflow, say -- that is a finding, but assert it with probe([...]) "
            f"and an empty result, since op() cannot return what does not exist.")
    return answers[id]

