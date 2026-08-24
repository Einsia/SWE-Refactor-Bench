"""What a stage-3 candidate is given to interrogate one sqlparse.

A candidate is a pytest file.  It may import this module and the standard
library, and nothing else -- in particular it cannot `import sqlparse`, because
only one of the two trees under test has one to import and a test that needs it
is not a comparison.

Two ways in:

    sqlformat(...)      the command line, for anything sqlformat(1) can express
    ask(op, ...)        the probe protocol, for everything else

The second exists because the command line reaches almost none of this library.
The lexer, the token-type lattice, the grouped parse tree, the node read surface,
the keyword tables, the filter stack and the formatter's option validation are
all library entry points with no flag that addresses them, and a candidate that
wants to know whether `Identifier.get_real_name()` agrees has to ask the probe.

`ops()` is the operation table -- name, argument shape, one line of prose.  It is
derived at image build time from the cases stage 2 actually ran, so every
operation listed here is one both trees have already been graded on answering.

Nothing here can rebuild either tree, reach a build directory, or read a source
file.  Both arrive already built.  A migration is graded on what it ships.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# The verifier's own protocol client, so a candidate speaks the wire exactly the
# way the graded run did.  A second implementation here would be a second thing
# that can be wrong, and a "break" caused by this module's own framing would
# waste a round.
sys.path.insert(0, str(Path(__file__).resolve().parent / "probelib"))
import vlib  # noqa: E402
from executor import CASE_TIMEOUT, ProbeSession, ProtocolError  # noqa: E402

__all__ = [
    "TARGET_NAME", "SCRATCH",
    "Doc", "Answer", "ProbeDefect", "ProbeCrashed",
    "sql", "sql_bytes", "ask", "asks", "ask_raw", "sqlformat",
    "ops", "op_info", "tiers",
    "add_format_preset", "add_validate_case",
    "format_presets", "validate_cases", "filters", "stacks",
    "ttype_names", "node_kinds", "encoding_aliases", "encoding_canonical",
    "accessor_scope", "lexstate_scripts", "spec",
]


def _env(name: str) -> str:
    value = os.environ.pop(name, "")
    if not value:
        raise RuntimeError(
            f"{name} is not set.  This module only works inside a stage-3 "
            f"candidate run; run-candidate.sh sets it."
        )
    return value


# Popped from the environment at import, so a candidate's own subprocesses do not
# inherit the wiring and cannot reach the tree by a path this module did not
# hand them.  Containment is not the claim -- the scope rule in probe.toml is
# what forbids going around this module -- but leaving the paths lying in
# `os.environ` would make going around it the path of least resistance.
_TIER_ARGV: dict[str, list[str]] = json.loads(_env("SRB_PROBE_ARGV"))
_TIER_ENV: dict[str, dict[str, str]] = json.loads(_env("SRB_PROBE_ENV"))
_SQLFORMAT = Path(_env("SRB_SQLFORMAT"))
_DOCS_DIR = Path(_env("SRB_DOCS_DIR"))
_DOCS_JSON = Path(_env("SRB_DOCS_JSON"))
_SPEC_JSON = Path(_env("SRB_SPEC_JSON"))
_OPS_JSON = Path(_env("SRB_OPS_JSON"))

#: An opaque per-run label for the tree under test, for diagnostics.
TARGET_NAME = os.environ.get("SRB_TARGET_TOKEN", "?")
#: Scratch for anything a candidate wants to keep.  Emptied between (candidate,
#: tree) pairs.
SCRATCH = Path(os.environ.get("SRB_SCRATCH", "/tmp/srb-candidate"))

_OPS: dict[str, dict] = json.loads(_OPS_JSON.read_text(encoding="utf-8"))
_SPEC: dict = json.loads(_SPEC_JSON.read_text(encoding="utf-8"))


class ProbeDefect(Exception):
    """The probe refused the request as malformed or unanswerable.

    Raised, never returned, because a defect is not a finding.  Both halves
    answer a defect identically -- an unknown op, a missing argument, a document
    that was never registered -- so a test that asserts on one has learned
    nothing about the migration.  If you see this, the request was wrong.
    """


class ProbeCrashed(Exception):
    """The probe process died or stopped speaking the protocol.

    This one *is* catchable on purpose: a tier that segfaults, hangs, or writes
    something that is not the protocol has behaved differently from a tier that
    answered, and that difference is exactly what a stage-3 candidate is looking
    for.  Catch it, assert on it, and the next `ask` gets a fresh process.
    """


@dataclass(frozen=True)
class Doc:
    """A document registered with the probe: bytes, plus a declared encoding.

    Two attributes, and the distinction between them is the whole point of
    several operations.  `data` is the bytes on disk.  `encoding` is what the
    caller *claims* they are, or None for "undeclared, work it out" -- which is
    a different code path in the library, not a defaulted version of the same
    one.  Whether an operation reads the declaration is a property of the
    operation, so `ops()` records it and this module sends the right tag.
    """

    id: str
    data: bytes
    encoding: str | None = None

    @property
    def text(self) -> str:
        return self.data.decode(self.encoding or "utf-8", "replace")

    def __str__(self) -> str:
        enc = self.encoding or "-"
        return f"<Doc {self.id} {len(self.data)}B {enc}>"


@dataclass(frozen=True)
class Answer:
    """One answer from the probe.

    `ok` is False for a *rendered* error -- the library raised, and the probe
    turned the exception into text on purpose.  That is an answer, and the two
    trees are expected to agree on it, so an error is a perfectly good thing to
    compare.  It is not a crash; a crash raises ProbeCrashed.
    """

    op: str
    status: str
    payload: bytes

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def text(self) -> str:
        """The payload as text, with undecodable bytes replaced.

        A property and not a method, deliberately.  It began as `text(errors=...)`
        and the first thing that ever used it wrote `a.text` -- which is legal,
        binds the method object, and turns `"0.5.3" in a.text` into
        "TypeError: argument of type 'method' is not iterable".  A candidate
        reading that traceback is reading what looks like a harness defect, and
        the round it spends on it is a round it does not spend on the port.  The
        `errors` argument bought nothing that `payload` does not: an answer whose
        bytes are not UTF-8 is a finding, and `payload` is where you go to prove
        it.
        """
        return self.payload.decode("utf-8", "replace")

    def lines(self) -> list[str]:
        body = self.text
        return body.split("\n") if body else []

    def fields(self) -> list[list[str]]:
        """Every line split on tabs.  Most answers are tab-separated records."""
        return [line.split("\t") for line in self.lines()]

    def get(self, key: str) -> str | None:
        """The value of the first `key \\t value` line, if there is one.

        Many answers are keyed records -- `lossless\\t1`, `category\\tpanic` --
        and indexing by position breaks the moment an answer grows a field.
        """
        for row in self.fields():
            if row and row[0] == key:
                return "\t".join(row[1:])
        return None

    def show(self, limit: int = 600) -> str:
        """A short rendering for an assertion message.

        Sized for the feedback window: a failing candidate is reported back with
        roughly 1200 characters per tree, so an assertion that pastes a whole
        reindented document tells the reader nothing.
        """
        body = self.text
        head = f"{self.op}/{self.status}"
        if len(body) <= limit:
            return f"{head}: {body}"
        return f"{head} [{len(body)}B]: {body[:limit]}..."

    def __str__(self) -> str:
        return self.show()


class _State:
    """Live sessions, and the manifests they were started against.

    Both halves read documents.json and spec.json once, at startup: the
    reference builds its Context there and the Go half unmarshals into SpecFile.
    So registering a document or a preset after a tier is running means that
    tier is looking at a stale manifest.  Rather than making a candidate think
    about it, every mutation bumps a generation counter and any session older
    than the counter is restarted on its next use.  Cheap -- a tier starts in
    milliseconds -- and it removes an entire class of confusing result.
    """

    def __init__(self) -> None:
        self.docs: dict[str, Doc] = {}
        self.generation = 0
        self.sessions: dict[str, tuple[ProbeSession, int]] = {}
        self.counter = 0
        self.log = vlib.Log(None)
        self.presets: dict[str, dict] = dict(_SPEC.get("format_presets", {}))
        self.validate: dict[str, dict] = dict(_SPEC.get("validate_cases", {}))
        _DOCS_DIR.mkdir(parents=True, exist_ok=True)
        self._write_docs()

    def _write_docs(self) -> None:
        """Synthesize the manifest both halves expect.

        Only `id` and `encoding` are read at run time; `group`, `bytes`,
        `sha256` and `utf8` are filled in because a half-populated record is
        something a reader would stop and puzzle over, and because the Go decoder
        types every one of them.
        """
        records = []
        for doc in self.docs.values():
            try:
                doc.data.decode("utf-8")
                utf8 = True
            except UnicodeDecodeError:
                utf8 = False
            records.append({
                "id": doc.id,
                "group": "candidate",
                "bytes": len(doc.data),
                "sha256": hashlib.sha256(doc.data).hexdigest(),
                "encoding": doc.encoding or "",
                "utf8": utf8,
            })
        records.sort(key=lambda r: r["id"])
        _DOCS_JSON.write_text(
            json.dumps({"documents": records}, indent=1) + "\n",
            encoding="utf-8")

    def _write_spec(self) -> None:
        spec = dict(_SPEC)
        spec["format_presets"] = self.presets
        spec["validate_cases"] = self.validate
        _SPEC_JSON.write_text(json.dumps(spec) + "\n", encoding="utf-8")

    def register(self, doc: Doc) -> Doc:
        (_DOCS_DIR / doc.id).write_bytes(doc.data)
        self.docs[doc.id] = doc
        self.generation += 1
        self._write_docs()
        return doc

    def add_preset(self, name: str, options: dict) -> None:
        self.presets[name] = options
        self.generation += 1
        self._write_spec()

    def add_validate(self, name: str, options: dict) -> None:
        self.validate[name] = options
        self.generation += 1
        self._write_spec()

    def next_id(self) -> str:
        self.counter += 1
        return f"q{self.counter:05d}"

    def session(self, tier: str) -> ProbeSession:
        existing = self.sessions.get(tier)
        if existing is not None:
            session, generation = existing
            if session.alive and generation == self.generation:
                return session
            session.stop()
        argv = _TIER_ARGV.get(tier)
        if not argv:
            raise ProbeCrashed(f"no program is installed for tier {tier!r}")
        env = vlib.base_env(**_TIER_ENV.get(tier, {}))
        session = ProbeSession(
            argv, cwd=None, env=env, log=self.log, label=f"cand-{tier}")
        session.start()
        self.sessions[tier] = (session, self.generation)
        return session

    def drop(self, tier: str) -> None:
        existing = self.sessions.pop(tier, None)
        if existing is not None:
            existing[0].stop()


_STATE = _State()


def _escape(text: str) -> str:
    """The `s:` escaping both halves already agree on (catalog.Catalog.s)."""
    out = []
    for ch in text:
        if ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\x{ord(ch):02x}")
        else:
            out.append(ch)
    return "".join(out)


_DOC_TAGS = ("d", "e")


def _tag(op: str, index: int, tag: str, value) -> str:
    """Tag one argument the way the operation declared it wants it.

    The tags are the reason this function exists rather than the candidate
    writing wire text.  `rt` takes `e:` and `rt-raw` takes `d:`; both name a
    document, and only the operation knows which -- so a candidate that guessed
    would be sending `rt` a document whose declared encoding is ignored and
    drawing conclusions from it.  The table decides.
    """
    if tag in _DOC_TAGS:
        if not isinstance(value, Doc):
            raise ProbeDefect(
                f"{op} argument {index} is a document; pass a Doc from sql() "
                f"or sql_bytes(), not {type(value).__name__}")
        if value.id not in _STATE.docs:
            raise ProbeDefect(f"document {value.id!r} is not registered")
        return f"{tag}:{value.id}"
    if tag == "s":
        if not isinstance(value, str):
            raise ProbeDefect(
                f"{op} argument {index} is text, not {type(value).__name__}")
        return "s:" + _escape(value)
    if tag == "n":
        if not isinstance(value, str):
            raise ProbeDefect(
                f"{op} argument {index} is a name, not {type(value).__name__}")
        return "n:" + value
    if tag == "i":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ProbeDefect(
                f"{op} argument {index} is an integer, not "
                f"{type(value).__name__}")
        return f"i:{value}"
    if tag == "b":
        if not isinstance(value, bool):
            raise ProbeDefect(
                f"{op} argument {index} is a boolean, not "
                f"{type(value).__name__}")
        return "b:1" if value else "b:0"
    raise ProbeDefect(f"{op} declares an argument tag this module does not "
                      f"know how to send: {tag!r}")


def _request(op: str, args: tuple) -> tuple[str, list[str]]:
    info = _OPS.get(op)
    if info is None:
        near = ", ".join(sorted(_OPS)[:6])
        raise ProbeDefect(f"unknown op {op!r}; see ops() -- e.g. {near}")
    tags = info["args"]
    if len(args) != len(tags):
        shape = " ".join(f"{t}:" for t in tags) or "(none)"
        raise ProbeDefect(
            f"{op} takes {len(tags)} argument(s) [{shape}], got {len(args)}")
    return info["tier"], [
        _tag(op, i, tag, value) for i, (tag, value) in enumerate(zip(tags, args))
    ]


def sql(text: str, *, encoding: str | None = None, id: str | None = None) -> Doc:
    """Register a document from text.  Encoded UTF-8 unless you say otherwise.

    `encoding` is the *declaration*, and it also selects the codec the text is
    encoded with, so `sql("SELECT 'ä'", encoding="latin-1")` gives you latin-1
    bytes that announce themselves as latin-1.  To hand the library bytes whose
    declaration disagrees with them -- which is where the interesting behaviour
    is -- use sql_bytes().
    """
    data = text.encode(encoding or "utf-8")
    return sql_bytes(data, encoding=encoding, id=id)


def sql_bytes(data: bytes, *, encoding: str | None = None,
              id: str | None = None) -> Doc:
    """Register a document from bytes, with an optional declared encoding.

    `encoding=None` means undeclared, which is a distinct path through the
    library rather than a default: several operations only consult a declaration
    when there is one, and the two halves have to agree about both cases.
    """
    if not isinstance(data, (bytes, bytearray)):
        raise ProbeDefect(f"sql_bytes takes bytes, not {type(data).__name__}")
    doc_id = id or f"cand-{len(_STATE.docs):04d}"
    if "/" in doc_id or doc_id in ("", ".", ".."):
        raise ProbeDefect(f"unusable document id: {doc_id!r}")
    return _STATE.register(Doc(doc_id, bytes(data), encoding))


def ask(op: str, *args) -> Answer:
    """Ask one operation and return its answer.

    Raises ProbeDefect if the request was malformed -- wrong argument count,
    unknown op, unregistered document -- and ProbeCrashed if the process
    answering died or stopped speaking the protocol.  A *rendered* error comes
    back as an Answer with ok False, because both trees are expected to agree
    about what the library raises.
    """
    tier, tagged = _request(op, args)
    case = {"id": _STATE.next_id(), "op": op, "args": tagged}
    session = _STATE.session(tier)
    try:
        record = session.ask(case, CASE_TIMEOUT)
    except (BrokenPipeError, TimeoutError, ProtocolError, OSError) as exc:
        note = session.stderr_tail(300)
        _STATE.drop(tier)
        detail = f": {note}" if note else ""
        raise ProbeCrashed(f"{op} on tier {tier}: {exc}{detail}") from exc
    if record.status == "defect":
        raise ProbeDefect(
            f"{op}: {record.payload.decode('utf-8', 'replace')[:400]}")
    return Answer(op, record.status, record.payload)


def asks(requests) -> list[Answer]:
    """Ask several operations in order.  `[("rt", doc), ("split", doc)]`."""
    return [ask(item[0], *item[1:]) for item in requests]


def ask_raw(op: str, *tagged: str) -> Answer:
    """Send arguments that are already tagged, bypassing the shape table.

    An escape hatch, and using it means taking the tags on yourself.  It is here
    because the table describes the operations stage 2 exercised, and an
    adversary may want to send one an argument shape those cases never used --
    `fmt` with a preset name that does not exist, say.  What it will not do is
    let you reach an operation that is not registered.
    """
    info = _OPS.get(op)
    if info is None:
        raise ProbeDefect(f"unknown op {op!r}; see ops()")
    for part in tagged:
        if not isinstance(part, str):
            raise ProbeDefect("ask_raw takes pre-tagged strings")
        if "\t" in part or "\n" in part:
            raise ProbeDefect(f"argument contains a wire delimiter: {part!r}")
    case = {"id": _STATE.next_id(), "op": op, "args": list(tagged)}
    tier = info["tier"]
    session = _STATE.session(tier)
    try:
        record = session.ask(case, CASE_TIMEOUT)
    except (BrokenPipeError, TimeoutError, ProtocolError, OSError) as exc:
        note = session.stderr_tail(300)
        _STATE.drop(tier)
        detail = f": {note}" if note else ""
        raise ProbeCrashed(f"{op} on tier {tier}: {exc}{detail}") from exc
    if record.status == "defect":
        raise ProbeDefect(
            f"{op}: {record.payload.decode('utf-8', 'replace')[:400]}")
    return Answer(op, record.status, record.payload)


@dataclass(frozen=True)
class CliResult:
    """What sqlformat(1) did.  Streams are bytes, deliberately.

    The formatter's output is not always valid UTF-8 -- it is whatever the input
    and the declared encoding made it, and several of the interesting cases are
    about exactly that.  Decoding here would hide the difference.
    """

    argv: list[str]
    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    #: Properties for the same reason Answer.text is one: `if r.text:` on a bound
    #: method is always true, and `"x" in r.text` is a TypeError that reads like a
    #: harness defect.  Compare `stdout`/`stderr` when the bytes are the point.
    @property
    def text(self) -> str:
        return self.stdout.decode("utf-8", "replace")

    @property
    def err(self) -> str:
        return self.stderr.decode("utf-8", "replace")

    def __str__(self) -> str:
        head = f"sqlformat {' '.join(self.argv[1:])} -> {self.returncode}"
        if self.timed_out:
            head += " (timed out)"
        tail = self.err.strip()
        return f"{head}\n{tail[:800]}" if tail else head


def sqlformat(*argv: str, stdin: bytes = b"", doc: Doc | None = None,
              timeout: float = 60.0) -> CliResult:
    """Run the installed sqlformat(1).

    Pass `doc=` to hand it a file rather than stdin; the path is substituted for
    the literal token `{doc}` in argv, or appended if the token is absent.

    Both trees expose a `sqlformat` at the same absolute path with the same
    argv[0] basename, because the reference prints its own program name in usage
    and error text.  A difference in that name would be a difference in every
    error message, which is a break about the harness rather than the migration.
    """
    args = [str(_SQLFORMAT)]
    doc_path = str((_DOCS_DIR / doc.id).resolve()) if doc is not None else None
    used = False
    for part in argv:
        # A Doc in argv is always a mistake, and it is a mistake that costs a
        # round if it goes through: str(doc) is "<Doc cand-0010 18B ->", the
        # reference reads it as a file operand, and the candidate gets a genuine
        # difference between two programs' this-file-does-not-exist messages.
        # Caught rather than stringified, with the fix in the message.
        if isinstance(part, Doc):
            raise ProbeDefect(
                "a Doc cannot be an argv word -- pass doc=<the Doc> and, if the "
                "position matters, put the literal '{doc}' where the path goes")
        if part == "{doc}":
            if doc_path is None:
                raise ProbeDefect("argv names {doc} but no doc= was passed")
            args.append(doc_path)
            used = True
        else:
            args.append(str(part))
    if doc_path is not None and not used:
        args.append(doc_path)
    try:
        proc = subprocess.run(
            args, input=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=vlib.base_env(), cwd=str(SCRATCH), timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        return CliResult(args, -1, exc.stdout or b"", exc.stderr or b"", True)
    return CliResult(args, proc.returncode, proc.stdout, proc.stderr)


def _tagged_option(name: str, value):
    """One formatter option, in the two-element form both halves decode.

    Go's JSON decoder turns every number into float64, so the tag is what tells
    an int from a float -- and several of the library's option errors exist only
    to complain about a type, so the distinction has to survive the wire.  A
    candidate writes `{"indent_width": 2}` and this puts the tag on.
    """
    if isinstance(value, bool):
        return ["b", value]
    if isinstance(value, int):
        return ["i", value]
    if isinstance(value, float):
        return ["f", value]
    if isinstance(value, str):
        return ["s", value]
    if value is None:
        return ["n", None]
    raise ProbeDefect(
        f"option {name!r}: {type(value).__name__} is not a value the option "
        f"wire carries (bool, int, float, str, None)")


def add_format_preset(name: str, **options) -> str:
    """Name a set of formatter options, so `fmt` can be asked for them.

    `fmt` addresses its options by name because both halves resolve the name in
    the same spec file, which is what makes the comparison symmetric.  This adds
    an entry to that file, so a candidate is not limited to the presets stage 2
    happened to freeze:

        p = add_format_preset("mine", reindent=True, indent_width=7)
        a = ask("fmt", p, doc)

    Filters and stacks are deliberately *not* extensible this way.  Naming one
    means naming a Python class and a Go constructor, and the Go side resolves
    from a fixed switch, so an invented filter would fail on the submission for
    want of a case label -- a break about the harness, not the port.
    """
    if not isinstance(name, str) or not name:
        raise ProbeDefect("a preset needs a name")
    tagged = {key: _tagged_option(key, value) for key, value in options.items()}
    _STATE.add_preset(name, tagged)
    return name


def add_validate_case(name: str, **options) -> str:
    """Name a set of options for `validate` to accept or reject.

    Same mechanism as add_format_preset, different operation: `validate` reports
    what validate_options() did with the options rather than formatting anything,
    so this is how to ask whether both halves reject the same nonsense with the
    same message.
    """
    if not isinstance(name, str) or not name:
        raise ProbeDefect("a validate case needs a name")
    tagged = {key: _tagged_option(key, value) for key, value in options.items()}
    _STATE.add_validate(name, tagged)
    return name


def ops() -> dict[str, dict]:
    """Every operation: `{name: {"tier", "args", "about"}}`.

    Derived at image build time from the cases stage 2 ran, cross-checked against
    both halves' registrations, so an operation listed here is one both trees
    have already been graded on.
    """
    return json.loads(json.dumps(_OPS))


def op_info(op: str) -> dict:
    info = _OPS.get(op)
    if info is None:
        raise ProbeDefect(f"unknown op {op!r}; see ops()")
    return dict(info)


def tiers() -> list[str]:
    """The tier names, each a separate program on the submission side."""
    return sorted({info["tier"] for info in _OPS.values()})


def spec() -> dict:
    """The whole spec table, as both halves read it."""
    return json.loads(json.dumps(_SPEC))


def format_presets() -> dict[str, dict]:
    """Named formatter option sets, including any you have added."""
    return json.loads(json.dumps(_STATE.presets))


def validate_cases() -> dict[str, dict]:
    return json.loads(json.dumps(_STATE.validate))


def filters() -> dict[str, dict]:
    """The named filters `filter` can install, one at a time."""
    return json.loads(json.dumps(_SPEC.get("filters", {})))


def stacks() -> dict[str, dict]:
    """The named filter stacks `stack` can run whole."""
    return json.loads(json.dumps(_SPEC.get("stacks", {})))


def ttype_names() -> list[str]:
    """Rendered token-type names, as `ttype` resolves them."""
    return list(_SPEC.get("ttype_names", []))


def node_kinds() -> list[str]:
    """The parse-tree node class names the grouping stage can produce."""
    return list(_SPEC.get("node_kinds", []))


def encoding_aliases() -> dict[str, str]:
    """The published encoding gate: alias -> canonical, or absent for rejected."""
    return dict(_SPEC.get("encoding_aliases", {}))


def encoding_canonical() -> list[str]:
    return list(_SPEC.get("encoding_canonical", []))


def accessor_scope() -> dict[str, str]:
    """Which node kinds answer which accessor, as `api` applies them."""
    return dict(_SPEC.get("accessor_scope", {}))


def lexstate_scripts() -> dict[str, list]:
    """Named scripts of mutations against the lexer's keyword state."""
    return json.loads(json.dumps(_SPEC.get("lexstate_scripts", {})))
