"""The one import a stage-3 candidate may make beyond the standard library.

A candidate is a pytest file, and the only thing it can reach is a built
`yaml-probe` speaking the NDJSON protocol instruction.md specifies.  It does not
know, and cannot be told, whether that binary came from the Go original or from
the Zig submission: `run-candidate.sh` hands over one executable at an
unguessable path and nothing else.  So the only way to distinguish the two trees
is to ask the library a question it answers differently, which is the whole point
of the stage.

    import srbyaml

    def test_literal_chomp():
        r = srbyaml.node("a: |-\n  x\n  y\n")
        assert r.ok, r
        assert r.node["content"][0]["content"][1]["value"] == "x\ny"

What this is not
----------------
It is not a YAML library.  There is no PyYAML in this image and there is no
second opinion available here: every answer comes from the binary under test, and
the reason a candidate is run twice is that "what should this be?" is answered by
the *other* tree rather than by anything in this file.

Why it duplicates stage 2's read loop
-------------------------------------
`tests/behavioural/lib/probe.py` holds the same discipline -- write one line, block
for one line, never let `readline` block uninterruptibly on a live-but-silent
child -- and this is a second implementation of it rather than a shared one,
because a Docker build context does not reach outside its own directory and
tests/verification is its own context.  The duplication is real, so it is checked
rather than trusted: `tests/check-task.py` compares the timeout constants and the
select-based framing in the two files and fails if they drift.  A candidate that
saw a different framing discipline from the one stage 2 graded against would be
measuring this file instead of the submission.
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import time
from dataclasses import dataclass, field

# Same value stage 2's probe.py enforces per request, for the same reason: a
# probe that consumes a request and answers nothing must be reported as a probe
# that did not answer, not as a candidate that timed out.  pytest-timeout is set
# above this in run-candidate.sh so the specific message wins over the generic
# one.
PER_REQUEST_TIMEOUT = 120.0
# The whole conversation for one process.  Stage 2 allows longer because it feeds
# thousands of frozen cases through one session; a candidate asks a handful.
SESSION_TIMEOUT = 600.0
# The largest legitimate single response in the frozen set is 616 KB (a 94 KB
# document under `stream`).  Two orders of magnitude above that is room for a
# submission that is wrong about sizes and still trying.
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
MAX_STDERR_BYTES = 64 * 1024

#: Every operation the protocol defines, `hello` included.  Kept here so a typo
#: in a candidate becomes an error in the candidate rather than a ProtocolError
#: that reads like a finding about the submission.
OPERATIONS = ("hello", "node", "stream", "emit", "emit_indent",
              "emit_stream", "roundtrip")


class ProbeDied(RuntimeError):
    """The probe exited, or stopped answering, mid-conversation.

    Raised rather than returned.  A probe that dies is a fact about the tree
    under test, and a candidate that asserts on it -- `with pytest.raises(
    srbyaml.ProbeDied)` -- is asserting on something the protocol forbids, which
    is in scope.
    """


def _binary() -> str:
    path = os.environ.get("SRB_PROBE", "")
    if not path:
        raise RuntimeError(
            "SRB_PROBE is not set: this module only works under "
            "run-candidate.sh, which builds a tree and hands over its probe")
    return path


@dataclass
class Response:
    """One response line, as bytes and as parsed JSON.

    `raw` is kept because the protocol is compared byte for byte: key order and
    the `\\uXXXX` escaping rule are part of the contract, and an assertion about
    them has to see the bytes.  `data` is the same line through `json.loads`, for
    the assertions that are about values.

    A line that is not JSON leaves `data` empty and sets `unparsable`.  That is
    itself a finding -- the protocol says every response is a JSON object -- so it
    is reported rather than raised.
    """

    raw: bytes
    request: dict
    data: dict = field(default_factory=dict)
    unparsable: str = ""

    @property
    def ok(self) -> bool:
        """True only for a well-formed success response."""
        return self.data.get("ok") is True

    @property
    def id(self):
        return self.data.get("id")

    @property
    def result(self) -> dict:
        value = self.data.get("result")
        return value if isinstance(value, dict) else {}

    @property
    def error(self) -> dict:
        value = self.data.get("error")
        return value if isinstance(value, dict) else {}

    @property
    def kind(self) -> str:
        """`YamlError`, `ProtocolError`, `Panic`, or "" on a success."""
        return str(self.error.get("kind") or "")

    @property
    def message(self) -> str:
        return str(self.error.get("message") or "")

    # -- the per-operation result keys, as attributes ------------------------
    # Named after the protocol's own keys so a candidate reads like the spec it
    # is asserting against.  Each returns the falsy default rather than raising,
    # because `assert r.out == "..."` on an error response should fail with the
    # response in the message, not with a KeyError three frames down.

    @property
    def node(self) -> dict:
        """`node` result: the parsed node tree."""
        value = self.result.get("node")
        return value if isinstance(value, dict) else {}

    @property
    def docs(self) -> list:
        """`stream` / `emit_stream` result: the documents read."""
        value = self.result.get("docs")
        return value if isinstance(value, list) else []

    @property
    def out(self) -> str:
        """`emit` / `emit_indent` / `emit_stream` result: the emitted YAML."""
        return str(self.result.get("out") or "")

    @property
    def stream_error(self):
        """`stream` / `emit_stream`: the error beside the documents, or None."""
        return self.result.get("error")

    def __str__(self) -> str:
        head = f"{self.request.get('op')} id={self.request.get('id')}"
        body = self.raw.decode("utf-8", "replace")
        if len(body) > 4000:
            body = body[:4000] + f"... [{len(self.raw)} bytes]"
        note = f" [not JSON: {self.unparsable}]" if self.unparsable else ""
        src = json.dumps(self.request.get("source", ""))
        if len(src) > 600:
            src = src[:600] + "..."
        return f"<{head}{note}\n  source: {src}\n  response: {body}>"

    __repr__ = __str__


class Probe:
    """A live yaml-probe process, fed one request at a time.

    Usable directly when a candidate needs to control the process boundary --
    several requests down one session, or a session it can watch exit:

        with srbyaml.Probe() as p:
            first = p.ask("node", "a: 1")
            second = p.ask("node", "b: 2")
        # exits the `with` by closing stdin; p.exit_code must be 0

    Most candidates want the module-level `node`, `emit`, `stream` and friends,
    which share one process for the whole file.
    """

    def __init__(self) -> None:
        # Deliberately takes no arguments.  There is exactly one binary a
        # candidate is allowed to run -- the one `run-candidate.sh` built and
        # named in SRB_PROBE -- and a constructor parameter would be arbitrary
        # command execution: `Probe("/bin/sh")` with `send_raw` for a command line
        # runs anything inside the staged tree, which is how a candidate reads the
        # tree it was not given.  `Probe("/bin/sh")` is a TypeError.
        self.binary = _binary()
        self.proc: subprocess.Popen | None = None
        self.sent = 0
        self.started = 0.0
        self.tail = b""
        self.stderr_seen = b""
        self.exit_code: int | None = None

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> "Probe":
        if self.proc is not None:
            raise RuntimeError("this Probe has already been started")
        self.proc = subprocess.Popen(
            [self.binary],
            cwd=os.environ.get("SRB_SCRATCH") or os.getcwd(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.sent = 0
        self.started = time.monotonic()
        self.tail = b""
        return self

    def __enter__(self) -> "Probe":
        return self.start() if self.proc is None else self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> int | None:
        """Close stdin and wait.  The protocol says EOF means exit 0.

        Returns the exit status, which a candidate may assert on: "exits 0 when
        stdin closes" is in the protocol, so a probe that exits 1, or hangs and
        has to be killed, is a divergence.
        """
        if self.proc is None:
            return self.exit_code
        proc, self.proc = self.proc, None
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
            self.exit_code = -9
            self._collect(proc)
            return self.exit_code
        self.exit_code = proc.returncode
        self._collect(proc)
        return self.exit_code

    def _collect(self, proc: subprocess.Popen) -> None:
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream and not stream.closed:
                    rest = stream.read() or b""
                    if stream is proc.stderr:
                        self.stderr_seen += rest[:MAX_STDERR_BYTES]
                    stream.close()
            except OSError:
                pass

    @property
    def stderr(self) -> str:
        """Whatever the probe wrote to stderr, decoded.

        Diagnostics on stderr are allowed; the protocol only reserves stdout.
        Available so a candidate can quote a crash, not so it can assert on the
        wording -- that is out of scope.
        """
        return self.stderr_seen.decode("utf-8", "replace")

    # -- the conversation ---------------------------------------------------

    def send(self, request: dict) -> Response:
        """One request object, one response.  The lowest-level entry point.

        Takes the dict the protocol describes, so a candidate can send a request
        the helpers cannot build -- an unknown `op`, a missing `source`, an extra
        `note` key -- all of which the protocol has answers for.
        """
        line = json.dumps(request, ensure_ascii=False,
                          separators=(",", ":")).encode("utf-8")
        return self._exchange(line, request)

    def send_raw(self, line: bytes | str) -> Response:
        """Send bytes verbatim, with no JSON encoding.

        The way to ask what a malformed line does: the protocol says a line that
        is not valid JSON is answered with a ProtocolError carrying id 0.
        """
        if isinstance(line, str):
            line = line.encode("utf-8")
        if b"\n" in line:
            raise ValueError("a request is one line; embedded newlines would be "
                             "two requests")
        return self._exchange(line, {"op": "<raw>", "id": None})

    def ask(self, op: str, source: str = "", *, id: int = 1,
            indent: int | None = None, **extra) -> Response:
        """The usual call: an operation, a YAML source, one response."""
        if op not in OPERATIONS and not extra.pop("allow_unknown_op", False):
            raise ValueError(
                f"{op!r} is not a protocol operation; the six graded ones plus "
                f"hello are {', '.join(OPERATIONS)}. To ask what an unknown op "
                f"does, use send({{'id': 1, 'op': 'whatever'}}).")
        request: dict = {"id": id, "op": op, "source": source}
        if indent is not None:
            request["indent"] = indent
        request.update(extra)
        return self.send(request)

    # -- the seven operations, by name ---------------------------------------
    # Methods rather than only module-level functions, so that `Probe()` and the
    # shared session have the same surface: a candidate that opened its own
    # session to watch the process exit should not have to drop back to `ask` for
    # every request it makes on the way there.  The module-level `node`, `emit`
    # and friends are these, on the shared session.

    def hello(self, **kw) -> Response:
        """The handshake.  Result is `protocol`, `library` and `upstream`."""
        return self.ask("hello", "", **kw)

    def node(self, source: str, **kw) -> Response:
        """Parse one document into a node tree.  Result key `node`."""
        return self.ask("node", source, **kw)

    def stream(self, source: str, **kw) -> Response:
        """Decode every document.  Result keys `docs` and `error`."""
        return self.ask("stream", source, **kw)

    def emit(self, source: str, **kw) -> Response:
        """Parse one document and encode it again.  Result key `out`."""
        return self.ask("emit", source, **kw)

    def emit_indent(self, source: str, indent: int, **kw) -> Response:
        """`emit` with SetIndent(indent) called first.

        `indent` is positional and required because the protocol distinguishes an
        absent indent from zero, and both are answers a candidate may want: send
        them with `ask("emit_indent", src)` and `emit_indent(src, 0)`.
        """
        return self.ask("emit_indent", source, indent=indent, **kw)

    def emit_stream(self, source: str, **kw) -> Response:
        """Decode every document, encode all of them through one encoder."""
        return self.ask("emit_stream", source, **kw)

    def roundtrip(self, source: str, **kw) -> Response:
        """Parse, emit, parse that, emit again.  Result keys `first`, `second`,
        `stable`, or one of `reparse_error` / `reemit_error`."""
        return self.ask("roundtrip", source, **kw)

    def _exchange(self, line: bytes, request: dict) -> Response:
        if self.proc is None:
            self.start()
        assert self.proc is not None
        if self.proc.poll() is not None:
            raise ProbeDied(
                f"the probe is no longer running (exit {self.proc.returncode}) "
                f"after {self.sent} request(s){self._note()}")
        if time.monotonic() - self.started > SESSION_TIMEOUT:
            raise ProbeDied(
                f"this session passed {SESSION_TIMEOUT:.0f}s after {self.sent} "
                f"request(s); start a new Probe for a longer conversation")
        deadline = time.monotonic() + PER_REQUEST_TIMEOUT
        self._write_all(line + b"\n", deadline)
        self.sent += 1
        raw = self._read_line(deadline)
        try:
            data = json.loads(raw.decode("utf-8"))
            unparsable = "" if isinstance(data, dict) else "not a JSON object"
            if not isinstance(data, dict):
                data = {}
        except (ValueError, UnicodeDecodeError) as exc:
            data, unparsable = {}, str(exc)
        return Response(raw=raw, request=dict(request), data=data,
                        unparsable=unparsable)

    # -- framing ------------------------------------------------------------
    # Both halves are on a deadline and neither uses a blocking file object.  A
    # request carrying a 90 KB source is larger than a pipe buffer, so a probe
    # that does not read blocks the write rather than the read; and a blocking
    # `readline` on a live-but-silent child cannot be interrupted, which is
    # exactly the failure the protocol's flush rule exists to forbid.

    def _write_all(self, payload: bytes, deadline: float) -> None:
        assert self.proc is not None and self.proc.stdin is not None
        fd = self.proc.stdin.fileno()
        view = memoryview(payload)
        while view:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProbeDied(
                    f"the probe stopped reading its stdin during request "
                    f"{self.sent + 1}: {len(payload) - len(view)} of "
                    f"{len(payload)} bytes accepted in "
                    f"{PER_REQUEST_TIMEOUT:.0f}s{self._note()}")
            try:
                ready = select.select([], [fd], [], min(remaining, 1.0))[1]
            except OSError as exc:
                raise ProbeDied(f"the probe's stdin is unusable: {exc}") from exc
            if not ready:
                if self.proc.poll() is not None:
                    raise ProbeDied(
                        f"the probe exited {self.proc.returncode} while request "
                        f"{self.sent + 1} was being written{self._note()}")
                continue
            try:
                written = os.write(fd, view[: 1 << 20])
            except BrokenPipeError:
                raise ProbeDied(
                    f"the probe closed its stdin after {self.sent} request(s); "
                    f"the protocol says it reads until EOF{self._note()}"
                ) from None
            except OSError as exc:
                raise ProbeDied(
                    f"writing request {self.sent + 1} failed: {exc}") from exc
            view = view[written:]

    def _read_line(self, deadline: float) -> bytes:
        assert self.proc is not None and self.proc.stdout is not None
        out_fd = self.proc.stdout.fileno()
        err_fd = self.proc.stderr.fileno() if self.proc.stderr else None
        while True:
            nl = self.tail.find(b"\n")
            if nl >= 0:
                line, self.tail = self.tail[:nl], self.tail[nl + 1:]
                return line
            if len(self.tail) > MAX_RESPONSE_BYTES:
                raise ProbeDied(
                    f"the response to request {self.sent} passed "
                    f"{MAX_RESPONSE_BYTES} bytes with no newline")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                alive = self.proc.poll() is None
                raise ProbeDied(
                    f"the probe did not answer request {self.sent} within "
                    f"{PER_REQUEST_TIMEOUT:.0f}s "
                    f"({'still running' if alive else 'exited'}, "
                    f"{len(self.tail)} bytes of a partial response). Every "
                    f"request gets one response line, flushed before the next "
                    f"request is read{self._note()}")
            watch = [out_fd] + ([err_fd] if err_fd is not None else [])
            try:
                ready = select.select(watch, [], [], min(remaining, 1.0))[0]
            except OSError as exc:
                raise ProbeDied(f"the probe's stdout is unusable: {exc}") from exc
            if err_fd is not None and err_fd in ready:
                self._drain_stderr(err_fd)
            if out_fd not in ready:
                continue
            chunk = os.read(out_fd, 1 << 20)
            if not chunk:
                code = self.proc.poll()
                if self.tail:
                    raise ProbeDied(
                        f"the probe exited ({code!r}) mid-response to request "
                        f"{self.sent}: {len(self.tail)} bytes with no "
                        f"terminating newline{self._note()}")
                raise ProbeDied(
                    f"the probe produced no response to request {self.sent} "
                    f"(exit {code!r}); it must answer every request and exit "
                    f"only on EOF{self._note()}")
            self.tail += chunk

    def _drain_stderr(self, err_fd: int) -> None:
        """Keep the stderr pipe from filling.

        A probe blocked writing diagnostics into a full 64 KB pipe looks exactly
        like one that hung, and would be reported as the wrong failure.
        """
        try:
            chunk = os.read(err_fd, 1 << 16)
        except OSError:
            return
        if chunk and len(self.stderr_seen) < MAX_STDERR_BYTES:
            self.stderr_seen += chunk[: MAX_STDERR_BYTES - len(self.stderr_seen)]

    def _note(self) -> str:
        if not self.stderr_seen:
            return ""
        text = " ".join(self.stderr.split())
        return "; stderr: " + text[-400:]


# --------------------------------------------------------------------------- #
# The shared session, and the helpers most candidates want
# --------------------------------------------------------------------------- #
# One process for the whole test file, restarted when it dies.  Sharing matters
# for more than speed: stage 2 grades thousands of cases down a single session, so
# a candidate that used a fresh process per request would be exercising a code
# path the graded suite never takes.
#
# A request that kills the probe still raises ProbeDied from that call -- the
# crash is not swallowed.  What the restart buys is that the *next* request gets a
# live process instead of inheriting a corpse, which is the same rule
# probe.py's Runner follows.

MAX_RESTARTS = 25
_shared: Probe | None = None
_restarts = 0


def probe() -> Probe:
    """The shared session, started or restarted as needed."""
    global _shared, _restarts
    if _shared is not None and _shared.proc is not None \
            and _shared.proc.poll() is None:
        return _shared
    if _shared is not None:
        if _restarts >= MAX_RESTARTS:
            raise ProbeDied(
                f"the probe has died {_restarts} times in this candidate; it is "
                f"not going to recover")
        _restarts += 1
        _shared.close()
    _shared = Probe().start()
    return _shared


def reset() -> None:
    """Close the shared session, so the next call starts a fresh process.

    Worth calling when a candidate's next assertion is about process state --
    "the first request after startup", or the exit status on EOF.
    """
    global _shared
    if _shared is not None:
        _shared.close()
        _shared = None


def restarts() -> int:
    """How many times the shared probe had to be restarted.

    A candidate can assert on this: a probe that dies once per document has a
    defect whatever its answers look like.
    """
    return _restarts


def ask(op: str, source: str = "", **kw) -> Response:
    """Any operation, on the shared session."""
    return probe().ask(op, source, **kw)


# The seven operations on the shared session.  Each one is the identically-named
# Probe method, so `srbyaml.node(src)` and `srbyaml.probe().node(src)` are the same
# call and a candidate can write whichever reads better.  They delegate rather than
# duplicating, so a change to an operation's signature cannot leave two spellings
# of it disagreeing.

def hello(**kw) -> Response:
    """The handshake.  Result is `protocol`, `library` and `upstream`."""
    return probe().hello(**kw)


def node(source: str, **kw) -> Response:
    """Parse one document into a node tree.  Result key `node`."""
    return probe().node(source, **kw)


def stream(source: str, **kw) -> Response:
    """Decode every document.  Result keys `docs` and `error`."""
    return probe().stream(source, **kw)


def emit(source: str, **kw) -> Response:
    """Parse one document and encode it again.  Result key `out`."""
    return probe().emit(source, **kw)


def emit_indent(source: str, indent: int, **kw) -> Response:
    """`emit` with SetIndent(indent) called first.

    `indent` is positional and required here because the protocol distinguishes
    an absent indent from zero, and both of those are answers a candidate may want
    -- send them with `ask("emit_indent", src)` and `emit_indent(src, 0)`.
    """
    return probe().emit_indent(source, indent, **kw)


def emit_stream(source: str, **kw) -> Response:
    """Decode every document, encode all of them through one encoder."""
    return probe().emit_stream(source, **kw)


def roundtrip(source: str, **kw) -> Response:
    """Parse, emit, parse that, emit again.  Result keys `first`, `second`,
    `stable`, or one of `reparse_error` / `reemit_error`."""
    return probe().roundtrip(source, **kw)


# --------------------------------------------------------------------------- #
# Reading a node tree
# --------------------------------------------------------------------------- #
# The protocol's node shape is a nest of dicts with single-letter keys, and an
# assertion written against it directly is unreadable in a report.  These are
# accessors, not a model: they return what the response said or a default, so a
# failing assertion prints the response rather than a KeyError.

#: The Style bitmask, by the name instruction.md gives it.
TAGGED, DOUBLE_QUOTED, SINGLE_QUOTED, LITERAL, FOLDED, FLOW = 1, 2, 4, 8, 16, 32


def kids(n: dict) -> list:
    """`content`, or [] when the node has no children."""
    value = (n or {}).get("content")
    return value if isinstance(value, list) else []


def walk(n: dict):
    """Every node in the tree, depth first, the node itself included."""
    if not isinstance(n, dict):
        return
    yield n
    for child in kids(n):
        yield from walk(child)


def doc_root(response: Response) -> dict:
    """The document node's single child: what most `node` assertions are about.

    A `node` response is a document node wrapping the content, so nearly every
    candidate starts with `["content"][0]`.  Returns {} when there is nothing
    there, so an assertion about a value fails on the value.
    """
    children = kids(response.node)
    return children[0] if children and isinstance(children[0], dict) else {}


def at(n: dict, *path) -> dict:
    """Index into a node tree by alternating content indices.

    `at(root, 0, 1)` is `root["content"][0]["content"][1]`.  A path that runs off
    the end returns {} rather than raising: the assertion that follows should fail
    about the value it wanted, and print the tree.
    """
    cur = n if isinstance(n, dict) else {}
    for step in path:
        children = kids(cur)
        if not isinstance(step, int) or step >= len(children) or step < -len(children):
            return {}
        nxt = children[step]
        cur = nxt if isinstance(nxt, dict) else {}
    return cur


def mapping(n: dict) -> dict:
    """A `map` node's children as a Python dict of scalar key -> node.

    Only for mappings whose keys are scalars, which is almost all of them; a
    complex key is skipped rather than stringified, and `pairs()` is there for a
    candidate that needs to see it.
    """
    out = {}
    children = kids(n)
    for i in range(0, len(children) - 1, 2):
        key, value = children[i], children[i + 1]
        if isinstance(key, dict) and key.get("kind") == "scalar":
            out[str(key.get("value", ""))] = value
    return out


def pairs(n: dict) -> list:
    """A `map` node's children as [(key_node, value_node), ...], keys of any kind."""
    children = kids(n)
    return [(children[i], children[i + 1])
            for i in range(0, len(children) - 1, 2)]


def summarize(n: dict) -> str:
    """A one-line-per-node rendering of a tree, for a failure message.

        doc l1 c1
          map !!map l1 c1
            scalar !!str 'a' l1 c1

    Assert on values, not on this: it is a debugging aid, and its format is not
    part of anything.
    """
    lines = []

    def render(node_: dict, depth: int) -> None:
        if not isinstance(node_, dict):
            return
        bits = [str(node_.get("kind", "?"))]
        for key in ("tag", "anchor", "alias"):
            if node_.get(key):
                bits.append(f"{key}={node_[key]!r}")
        if node_.get("style"):
            bits.append(f"style={node_['style']}")
        if "value" in node_:
            bits.append(repr(node_["value"]))
        for key in ("head", "line", "foot"):
            if node_.get(key):
                bits.append(f"{key}={node_[key]!r}")
        bits.append(f"l{node_.get('l')} c{node_.get('c')}")
        lines.append("  " * depth + " ".join(bits))
        for child in kids(node_):
            render(child, depth + 1)

    render(n, 0)
    return "\n".join(lines)
