"""What a stage-3 candidate is given to attack an installed zlib.

A candidate is a pytest file.  It may import this module and the standard library,
and nothing else.  Everything here addresses what the tree under test *installed*
and what its build produced: there is deliberately no way from here to reach a
source file, and no way to reach the other tree.

Two ways in:

    probe(rows)        drive the differential probe, the instrument stage 2 uses
    example()          the two test drivers the graded build produces
    minigzip(...)

The probe is the important one, and it is not a new instrument invented for this
stage.  It is the same `probe.c` / `Probe.java` pair the behavioural suite runs,
driven through the same `executor.ProbeRunner`, so a candidate exercises the
verifier's own execution path rather than a lookalike.  That matters for a reason
that is easy to miss: if stage 3 had its own runner, a divergence it found might be
a divergence between the two runners.

Why a candidate cannot see which tree it is on
----------------------------------------------
The two trees do not produce the same kind of artifact -- one installs
libz.so.1.3.1 and a header, the other installs a modular jar -- so a probe against
the C is an ELF binary and a probe against the Java is a JVM launch.  That
difference is real and this module cannot pretend otherwise; what it does is keep
the difference out of the candidate's way.  `probe()` takes rows and returns
records.  The command that produced them is assembled by run-candidate.sh, kept at
a path named by a per-run hash, and never exposed here.

A candidate that goes looking anyway -- reads the launcher, inspects the install
tree for a .jar, greps its own environment -- will find the answer.  It is not
worth doing.  Such a candidate meets every mechanical condition for a break, since
it passes on one tree and fails on the other and reproduces exactly, and it
establishes nothing about the migration; probe.toml's [scope] deny list names it,
and it is rejected on sight rather than weighed.  The question worth asking is
whether the two trees *answer differently*, and `probe()` is how you ask it.

Building an input the recorded cases do not contain
---------------------------------------------------
The probe addresses its payloads by corpus index, not by literal bytes: an op's
blob argument is an integer, and the probe reads `<corpus>/blobs/%05d.bin`.  So a
candidate composes a novel input by putting it in its own corpus:

    import srbzlib

    def test_flush_schedule():
        blob = srbzlib.payload(b"aaaa" * 400 + b"\\x00" * 17)
        rec = srbzlib.op("deflate", blob, 9, 0, 15, 8, 0, 0, 3, 7)
        assert rec.ok, rec
        assert rec.field("rc") == "Z_STREAM_END", rec.text()

`payload()` returns the index, and the corpus directory belongs to this candidate
alone: two candidates cannot see each other's payloads, and the same candidate gets
a fresh one on each tree.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

# The shipped executor and its support library, from tests/verification/lib.  Both
# are byte-identical copies of the behavioural stage's, which the stage Dockerfile
# checks against environment/probe.sha256 at build time -- a candidate driving a
# runner that had drifted from stage 2's would be measuring the drift.
import vlib
from executor import ProbeRecord, ProbeRunner

__all__ = [
    "OPS", "TARGET_TOKEN", "SCRATCH",
    "Record", "Output",
    "payload", "payload_file", "corpus_dir",
    "probe", "op", "example", "minigzip",
    "installed", "install_tree", "build_output",
]


def _env_path(name: str) -> Path:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"{name} is not set. This module only works inside a stage-3 "
            f"candidate run; run-candidate.sh sets it."
        )
    return Path(value)


#: The launcher that starts the probe for the tree under test.  A one-element
#: command prefix: run-candidate.sh decided what it wraps.
_LAUNCHER = _env_path("SRB_PROBE_LAUNCH")
#: The install prefix of the tree under test.
PREFIX = _env_path("SRB_PREFIX")
#: The build directory, which is where the two test drivers are.  They are not
#: installed -- upstream does not install them either -- so the contract says the
#: verifier runs them from the build tree.
BUILD_DIR = _env_path("SRB_BUILD_DIR")
#: An opaque per-run label for the tree under test, for diagnostics.  Not the
#: role, and opaque on purpose: a label naming which side it is would let a
#: candidate assert on the name and call that a break -- every mechanical
#: condition met, and nothing established about behaviour.  A hash keeps two
#: failure messages from one comparison distinguishable without saying which
#: side is which.
TARGET_TOKEN = os.environ.get("SRB_TARGET_TOKEN", "?")
#: Scratch for this candidate, emptied between (candidate, tree) pairs.
SCRATCH = Path(os.environ.get("SRB_SCRATCH", "/tmp/srb-candidate"))

#: The probe's operations, as `probe.c`'s dispatch declares them.  Every one takes
#: a corpus index first and integers after; the arguments each accepts are
#: documented in probe.c beside the op, which is readable in the original tree.
OPS = (
    "deflate", "inflate", "bound", "oneshot", "checksum", "statechange",
    "gzheader", "syncrecover", "inflatestate", "error", "inflateback",
    "gzfile", "abi", "alloc",
)

_CORPUS = SCRATCH / "corpus"
_BLOBS = _CORPUS / "blobs"
_next_index = 0

# One Log for the whole candidate.  ProbeRunner requires one and writes its
# progress and any crash into it; the file is in this candidate's scratch and the
# same lines go to stderr, which pytest shows when a test fails.  A candidate does
# not need to read it.
_LOG = vlib.Log(SCRATCH / "probe.log")


# --------------------------------------------------------------------------- #
# The corpus
# --------------------------------------------------------------------------- #

def corpus_dir() -> Path:
    """This candidate's corpus directory, created on first use."""
    _BLOBS.mkdir(parents=True, exist_ok=True)
    return _CORPUS


def payload(data: bytes) -> int:
    """Register bytes as a corpus blob and return the index the ops take.

    Indices are handed out in order from 0 for each (candidate, tree) pair, so the
    same candidate registering the same payloads gets the same indices on both
    trees.  That is what makes a comparison meaningful -- and it is why you should
    register payloads at module level or at the top of the test rather than
    conditionally: a payload registered inside an `if` that only fires on one tree
    would shift every later index by one.
    """
    global _next_index
    corpus_dir()
    index = _next_index
    _next_index += 1
    (_BLOBS / f"{index:05d}.bin").write_bytes(data)
    return index


def payload_file(path: Path | str) -> int:
    """Register a file's bytes as a corpus blob. Same rules as payload()."""
    return payload(Path(path).read_bytes())


# --------------------------------------------------------------------------- #
# The probe
# --------------------------------------------------------------------------- #

@dataclass
class Record:
    """One probe record: the answer to one op on one tree.

    `payload` is bytes, deliberately.  A probe record carries compressed output,
    and several of the interesting cases are about bytes that are not text.
    """

    case_id: str
    status: str
    payload: bytes

    @property
    def ok(self) -> bool:
        """Whether the probe completed the op.

        `status` is "ok" when it did, "badop" for an op the probe does not have,
        and "crash:..." when the probe died on this case -- which is itself a
        finding worth asserting, since a segfault or an uncaught exception on
        input the other tree survives is exactly what this stage looks for.
        """
        return self.status == "ok"

    def text(self, errors: str = "replace") -> str:
        return self.payload.decode("utf-8", errors)

    def fields(self) -> dict[str, str]:
        """The record's `key=value` lines, parsed.

        Every op emits its answer as lines of `key=value`; ops that emit bytes
        emit them as a hex value on such a line.  Later keys win, which matches
        how a reader of the raw text would take it.
        """
        out: dict[str, str] = {}
        for line in self.text().splitlines():
            key, sep, value = line.partition("=")
            if sep:
                out[key.strip()] = value.strip()
        return out

    def field(self, name: str, default: str | None = None) -> str | None:
        return self.fields().get(name, default)

    def __str__(self) -> str:
        head = f"{self.case_id} [{self.status}] on {TARGET_TOKEN}"
        body = self.text()
        if len(body) > 4000:
            body = body[:4000] + f"\n... ({len(self.payload)} bytes total)"
        return f"{head}\n{body}" if body else head


def _runner() -> ProbeRunner:
    """A ProbeRunner over the launcher, for this candidate's corpus.

    Constructed per call rather than cached: the runner holds no state between
    batches beyond its crash list, and a candidate that mutated one would
    otherwise carry the mutation into its next call.
    """
    corpus_dir()
    return ProbeRunner(
        [str(_LAUNCHER)],
        _CORPUS,
        SCRATCH / "probe-scratch",
        _LOG,
        label=TARGET_TOKEN,
    )


def probe(rows: list[list[str | int]]) -> dict[str, Record]:
    """Drive the probe over these rows, returning records keyed by case id.

    A row is `[case_id, op, *args]`.  Integers are fine; everything is
    stringified on the way out, since the protocol is tab-separated text.

        recs = srbzlib.probe([
            ["c1", "deflate", blob, 6],
            ["c2", "deflate", blob, 9],
        ])
        assert recs["c1"].payload != recs["c2"].payload

    Case ids must be unique within a call.  A row whose op the probe does not know
    comes back with status "badop" rather than raising, and a probe that dies takes
    only its victim case with it: the records it produced before the crash are
    returned, the victim's status is "crash:...", and the remaining rows are
    re-driven.  That recovery is the shipped runner's, not this module's.
    """
    if not rows:
        return {}
    prepared = [[str(part) for part in row] for row in rows]
    ids = [row[0] for row in prepared]
    if len(set(ids)) != len(ids):
        raise ValueError("probe(): duplicate case ids in one call")
    records = _runner().run(prepared)
    return {
        case_id: Record(rec.case_id, rec.status, rec.payload)
        for case_id, rec in records.items()
    }


def op(name: str, *args: str | int, case_id: str = "case") -> Record:
    """Run one op and return its record. The single-case shorthand.

        rec = srbzlib.op("checksum", blob, 0)
        assert rec.field("adler32") == "0x..."

    Raises if the probe produced no record at all, which means it died before
    writing its header -- a candidate should see that as an error in its own row
    rather than as a silent empty answer.
    """
    got = probe([[case_id, name, *args]])
    if case_id not in got:
        raise AssertionError(
            f"the probe produced no record for {case_id!r} ({name}) on "
            f"{TARGET_TOKEN}; it died before emitting a header"
        )
    return got[case_id]


# --------------------------------------------------------------------------- #
# The drivers
# --------------------------------------------------------------------------- #

@dataclass
class Output:
    """The result of running a driver."""

    argv: list[str]
    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    files: dict[str, bytes] | None = None

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def __str__(self) -> str:
        head = f"{' '.join(self.argv)} -> {self.returncode}"
        if self.timed_out:
            head += " (timed out)"
        tail = self.stderr.decode("utf-8", "replace").strip()
        return f"{head}\n{tail[:2000]}" if tail else head


def _driver(name: str) -> Path:
    """One of the two test drivers, from the build directory.

    The contract requires each to be an executable file the harness can exec
    directly -- for a JVM that means a wrapper script -- so this execs a path.  A
    driver that is missing or not executable raises here rather than producing a
    confusing permission error inside the comparison.
    """
    path = BUILD_DIR / name
    if not path.is_file():
        raise AssertionError(
            f"the build produced no {name} in {BUILD_DIR} on {TARGET_TOKEN}; "
            f"it holds: {sorted(p.name for p in BUILD_DIR.iterdir())[:40]}"
        )
    if not os.access(path, os.X_OK):
        raise AssertionError(f"{name} exists but is not executable on {TARGET_TOKEN}")
    return path


def _run(argv: list[str], *, stdin: bytes = b"", timeout: float = 120.0,
         cwd: Path | None = None) -> Output:
    try:
        proc = subprocess.run(
            argv, input=stdin, capture_output=True, timeout=timeout,
            cwd=str(cwd) if cwd else None, env=vlib.base_env(),
        )
    except subprocess.TimeoutExpired as exc:
        return Output(argv, 124, exc.stdout or b"", exc.stderr or b"",
                      timed_out=True)
    return Output(argv, proc.returncode, proc.stdout, proc.stderr)


def _fresh(name: str) -> Path:
    work = SCRATCH / "drivers" / name
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    return work


def example(*args: str, timeout: float = 120.0) -> Output:
    """Run the `example` driver in a clean directory.

    Upstream's test/example.c: a self-checking exercise of compress, uncompress,
    deflate, inflate, dictionaries, sync and flush.  It prints a fixed transcript
    and exits 0, or prints the failure and aborts -- so both the transcript and the
    exit status are worth asserting.

    It writes foo.gz in its working directory and reads it back.  `.files` holds
    whatever it left behind, which is how you compare the gzip container it
    produced rather than only what it said about it.
    """
    work = _fresh("example")
    out = _run([str(_driver("example")), *args], timeout=timeout, cwd=work)
    out.files = {p.name: p.read_bytes() for p in sorted(work.iterdir())
                 if p.is_file()}
    return out


def minigzip(*args: str, stdin: bytes = b"", timeout: float = 120.0) -> Output:
    """Run the `minigzip` driver as a filter, in a clean directory.

    Upstream's test/minigzip.c: a gzip-compatible filter over the gz* API.  With
    no arguments it compresses stdin to stdout; `-d` decompresses.  The bytes it
    writes are a second, independent check on the gzip wrapper, and they are
    comparable against the system gzip -- which is in this image and is not either
    tree's code.
    """
    work = _fresh("minigzip")
    out = _run([str(_driver("minigzip")), *args], stdin=stdin, timeout=timeout,
               cwd=work)
    out.files = {p.name: p.read_bytes() for p in sorted(work.iterdir())
                 if p.is_file()}
    return out


# --------------------------------------------------------------------------- #
# The installed tree
# --------------------------------------------------------------------------- #

def installed(relpath: str) -> Path:
    """A path under the install prefix. Existence is not checked."""
    return PREFIX / relpath


def install_tree() -> list[str]:
    """Every path the install produced, relative to the prefix, sorted.

    For a failure message, and for the one legitimate question about the install
    layout: whether the entries the contract requires are there.  Asking which
    *kind* of artifact they are, in order to learn which tree this is, is the deny
    list's business -- see this module's docstring.
    """
    if not PREFIX.is_dir():
        return []
    out: list[str] = []
    for path in sorted(PREFIX.rglob("*")):
        rel = str(path.relative_to(PREFIX))
        out.append(rel + ("/" if path.is_dir() and not path.is_symlink() else ""))
    return out


def build_output() -> list[str]:
    """The build directory's top level, for a failure message about a driver."""
    if not BUILD_DIR.is_dir():
        return []
    return sorted(p.name for p in BUILD_DIR.iterdir())
