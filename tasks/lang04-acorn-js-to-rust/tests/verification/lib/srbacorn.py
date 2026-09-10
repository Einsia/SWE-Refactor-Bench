"""What a stage-3 candidate is allowed to import, besides the standard library.

A candidate is a pytest file.  It is run twice -- once against a tree built from
State A's JavaScript, once against the submission's Rust -- and it is not told
which run is which.  Both trees are presented through the two entry points
instruction.md specifies as the deliverable:

    $SRB_PREFIX/bin/acorn-probe    the NDJSON line protocol
    $SRB_PREFIX/bin/acorn          the command-line interface

This module is the only thing that knows how to talk to them.  It exists for two
reasons.  The first is that six models would otherwise each write the same
sixty lines of subprocess framing, and a bug in one of those copies reads as a
defect in the submission.  The second is the important one: the protocol is
specified byte for byte, so an assertion about it has to be able to see bytes.
`json.loads` on a response silently discards exactly the things the contract
pins -- key order, `1e+21` versus `1e21`, a key that is absent versus one that is
`null`, `-0`.  A candidate that only ever compares parsed dictionaries cannot
express most of what this task promises, so `ask()` returns both forms and the
raw line is the one that decides.

What this module deliberately does not offer
--------------------------------------------
Anything that would let a candidate learn which tree it is on.  There is no
accessor for the role, no path that contains it, and the token in
`SRB_TARGET_TOKEN` is opaque and changes per run.  Reading the installed prefix
to find out whether `bin/acorn-probe` is an ELF or a shell script would answer
the question, and probe.toml's scope rejects a candidate that does -- it meets
every mechanical condition for a break while establishing nothing about the
migration.  Ask the artifact a question about JavaScript instead; that is the
only kind of answer that means anything.
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

__all__ = [
    "Output", "Response", "prefix", "probe_path", "acorn_path", "scratch",
    "ask", "ask_one", "ask_raw", "parse", "parse_or_error", "tokenize",
    "loose_parse", "walk_full", "run_acorn", "OPS",
]

#: Every op the protocol defines, so a candidate can be sure it is spelling one
#: correctly rather than discovering `{"ok":false,"error":{"kind":
#: "ProtocolError"}}` and reading it as a finding.  An unknown op is answered by
#: both sides the same way, which makes it a bad candidate rather than a good one.
OPS = (
    "version", "default_options", "token_types", "keyword_types",
    "parse", "parse_collect", "parse_expression_at", "tokenize", "loose_parse",
    "walk_full", "walk_full_ancestor", "walk_simple", "walk_recursive",
    "find_node_at", "find_node_around", "find_node_after", "find_node_before",
    "get_line_info", "is_identifier_start", "is_identifier_char", "is_new_line",
    "line_break_test", "nonascii_whitespace_test",
)

DEFAULT_TIMEOUT = 60.0


class ProbeError(RuntimeError):
    """The probe process itself misbehaved: died, hung, or lost a line.

    Distinct from a response with ``ok: false``, which is a normal answer and
    frequently the answer a candidate is looking for.
    """


@dataclass
class Output:
    """A finished subprocess.  Bytes, because the contract is about bytes."""

    argv: list[str]
    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def __str__(self) -> str:  # shown when an assert fails
        return (f"argv={self.argv}\nreturncode={self.returncode}"
                f"{' (TIMED OUT)' if self.timed_out else ''}\n"
                f"stdout={self.stdout[:2000]!r}\nstderr={self.stderr[:2000]!r}")


@dataclass
class Response:
    """One response line, in both forms.

    ``raw`` is the line as it came off the wire, without its newline.  It is the
    authority: the protocol fixes key order and number formatting, and those
    survive in ``raw`` and nowhere else.

    ``value`` is ``json.loads(raw)`` for the cases where structure is what you
    are asserting about.  ``result`` and ``error`` are conveniences over it.

    ``has_result`` distinguishes ``{"id":1,"ok":true}`` -- the correct answer
    when a ``find_node_*`` matches nothing, because JavaScript's ``undefined``
    is omitted by ``JSON.stringify`` -- from a result that is present and
    ``null``.  Reading ``.result`` alone cannot tell those apart, and the
    difference is one instruction.md calls out.
    """

    raw: bytes
    value: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return bool(self.value.get("ok"))

    @property
    def id(self) -> Any:
        return self.value.get("id")

    @property
    def has_result(self) -> bool:
        return "result" in self.value

    @property
    def result(self) -> Any:
        return self.value.get("result")

    @property
    def error(self) -> dict[str, Any]:
        err = self.value.get("error")
        return err if isinstance(err, dict) else {}

    def __str__(self) -> str:
        return self.raw.decode("utf-8", "replace")


# --------------------------------------------------------------------------- #
# Where the tree under test is
# --------------------------------------------------------------------------- #
# Set by run-candidate.sh.  A missing one is a harness fault and says so, rather
# than defaulting to something that would make every candidate fail identically
# on both trees and look like a clean submission.

def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ProbeError(
            f"{name} is not set: srbacorn is being imported outside the stage-3 "
            f"runner, so there is no installed tree to talk to"
        )
    return value


def prefix() -> Path:
    """The install prefix `make install PREFIX=` produced."""
    return Path(_env("SRB_PREFIX"))


def probe_path() -> Path:
    """`bin/acorn-probe`, the line protocol."""
    return Path(_env("SRB_PROBE"))


def acorn_path() -> Path:
    """`bin/acorn`, the CLI."""
    return Path(_env("SRB_ACORN"))


def scratch() -> Path:
    """A directory of your own, emptied before each (candidate, tree) run.

    Write input files here.  Anything written elsewhere may still be there when
    the same candidate runs against the other tree, which is the one way a
    deterministic-looking candidate can produce two different answers.
    """
    path = Path(os.environ.get("SRB_SCRATCH") or "/tmp/srb-candidate")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _clean_env() -> dict[str, str]:
    """The environment the probe runs in.

    Built from a fixed dict rather than inherited: `SRB_*` in the child's
    environment would let a submission's probe notice it is being graded, and
    the locale and timezone decide number and date formatting in some standard
    libraries.
    """
    keep = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": os.environ.get("HOME", "/root"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TZ": "UTC",
    }
    # The reference side is a node script and needs its own module root; the Rust
    # side ignores this.  It is the one variable that differs between the trees,
    # and it carries no information about which is which -- it is set for both.
    for name in ("ACORN_REFERENCE_REPO", "NODE_PATH"):
        if name in os.environ:
            keep[name] = os.environ[name]
    return keep


# --------------------------------------------------------------------------- #
# The line protocol
# --------------------------------------------------------------------------- #

def _encode(request: dict[str, Any]) -> bytes:
    # Compact separators so the request is one line, and ensure_ascii=False
    # because the protocol's own encoding does not escape non-ASCII -- a source
    # string containing U+2028 has to arrive as U+2028.
    line = json.dumps(request, ensure_ascii=False, separators=(",", ":"))
    if "\n" in line or "\r" in line:
        raise ProbeError(f"request encodes to more than one line: {line[:200]!r}")
    return line.encode("utf-8")


def ask_raw(requests: Sequence[dict[str, Any]] | Iterable[dict[str, Any]],
            *, timeout: float = DEFAULT_TIMEOUT,
            strict: bool = True) -> list[bytes]:
    """Send request lines to the probe; return the raw response lines.

    One process per call, fed every line and then given EOF.  That is the shape
    the protocol specifies -- responses in request order, one per line, each
    flushed -- and it is also the shape that makes a candidate reproducible: a
    long-lived process would carry state between assertions, and a port whose
    tenth answer depends on its first is a defect this could hide rather than
    find.

    ``strict`` checks the framing: exit status 0, and one response per request.
    Turn it off to assert *about* the framing -- that a malformed line is
    answered rather than fatal, say, or that a request after a failing one is
    still answered.  With it off you get whatever the process produced.
    """
    lines = [_encode(r) for r in requests]
    payload = b"".join(line + b"\n" for line in lines)
    argv = [str(probe_path())]
    try:
        proc = subprocess.run(argv, input=payload, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=timeout,
                              env=_clean_env(), cwd=str(scratch()))
    except subprocess.TimeoutExpired as exc:
        raise ProbeError(
            f"the probe did not finish within {timeout}s for {len(lines)} "
            f"request(s); first was {lines[0][:200]!r}"
            if lines else f"the probe hung on an empty request set"
        ) from exc
    out = proc.stdout.split(b"\n")
    if out and out[-1] == b"":
        out.pop()
    if strict:
        if proc.returncode != 0:
            raise ProbeError(
                f"the probe exited {proc.returncode}; the protocol says it exits "
                f"0 on EOF however malformed a request is.\n"
                f"stderr={proc.stderr[:2000]!r}\nstdout={proc.stdout[:2000]!r}"
            )
        if len(out) != len(lines):
            raise ProbeError(
                f"sent {len(lines)} request(s) and got {len(out)} response "
                f"line(s); the protocol says one response per request, in order."
                f"\nstdout={proc.stdout[:2000]!r}\nstderr={proc.stderr[:2000]!r}"
            )
    return out


def ask(requests: Sequence[dict[str, Any]] | Iterable[dict[str, Any]],
        *, timeout: float = DEFAULT_TIMEOUT,
        strict: bool = True) -> list[Response]:
    """`ask_raw`, with each line also parsed.  See `Response`."""
    responses = []
    for raw in ask_raw(requests, timeout=timeout, strict=strict):
        try:
            value = json.loads(raw)
        except ValueError:
            if strict:
                raise ProbeError(f"response is not JSON: {raw[:400]!r}")
            value = {}
        if not isinstance(value, dict):
            if strict:
                raise ProbeError(f"response is not a JSON object: {raw[:400]!r}")
            value = {}
        responses.append(Response(raw=raw, value=value))
    return responses


def ask_one(op: str, *, id: int = 1, timeout: float = DEFAULT_TIMEOUT,
            **fields: Any) -> Response:
    """One request, one response.

        r = srbacorn.ask_one("parse", source="let x = 1",
                             options={"ecmaVersion": 2020})
        assert r.ok, r
        assert r.result["body"][0]["type"] == "VariableDeclaration"

    `id` is settable because a candidate asserting that ids are echoed needs to
    send something other than 1.
    """
    request: dict[str, Any] = {"id": id, "op": op}
    request.update(fields)
    return ask([request], timeout=timeout)[0]


# --------------------------------------------------------------------------- #
# Shorthands for the ops a candidate reaches for most
# --------------------------------------------------------------------------- #
# Each raises on an unexpected outcome so a typo in a test fails as an error
# rather than as a finding.  Use `ask_one` when the failure *is* the assertion.

def parse(source: str, **options: Any) -> Any:
    """`acorn.parse(source, options)`.  Raises if it did not parse."""
    r = ask_one("parse", source=source, options=options or {})
    if not r.ok:
        raise ProbeError(f"parse failed on {source!r}: {r.error}")
    return r.result


def parse_or_error(source: str, **options: Any) -> Response:
    """`parse`, without the opinion.  For input you expect to be rejected."""
    return ask_one("parse", source=source, options=options or {})


def tokenize(source: str, **options: Any) -> Any:
    r = ask_one("tokenize", source=source, options=options or {})
    if not r.ok:
        raise ProbeError(f"tokenize failed on {source!r}: {r.error}")
    return r.result


def loose_parse(source: str, **options: Any) -> Any:
    """`acornLoose.parse`.  This one is supposed to never fail, on any input."""
    r = ask_one("loose_parse", source=source, options=options or {})
    if not r.ok:
        raise ProbeError(
            f"loose_parse reported an error, which acorn-loose does not do: "
            f"{r.error}"
        )
    return r.result


def walk_full(source: str, **options: Any) -> Any:
    r = ask_one("walk_full", source=source, options=options or {})
    if not r.ok:
        raise ProbeError(f"walk_full failed on {source!r}: {r.error}")
    return r.result


# --------------------------------------------------------------------------- #
# The CLI
# --------------------------------------------------------------------------- #

def run_acorn(*args: str, stdin: bytes | str | None = None,
              timeout: float = DEFAULT_TIMEOUT) -> Output:
    """Run `bin/acorn` with these arguments.

    A timeout returns an `Output` with `timed_out` set rather than raising, so
    "it hung where 8.14.0 did not" is something a candidate can assert.
    """
    if isinstance(stdin, str):
        stdin = stdin.encode("utf-8")
    argv = [str(acorn_path()), *[str(a) for a in args]]
    try:
        proc = subprocess.run(argv, input=stdin or b"", stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=timeout,
                              env=_clean_env(), cwd=str(scratch()))
    except subprocess.TimeoutExpired as exc:
        return Output(argv=argv, returncode=-1,
                      stdout=exc.stdout or b"", stderr=exc.stderr or b"",
                      timed_out=True)
    return Output(argv=argv, returncode=proc.returncode,
                  stdout=proc.stdout, stderr=proc.stderr)
