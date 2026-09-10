#!/usr/bin/env python3
"""Case execution and differential comparison.

Three executors:

* `ProbeRunner` drives the probe over the protocol on stdin.  Cases run in
  batches for speed, but each record is self-delimiting and flushed as it is
  produced, so a crash is attributed to exactly one case rather than voiding the
  batch -- the surviving records still count, and the batch is re-driven from
  the case after the crash.

* `CommandRunner` executes one process per case: the verifier's own consumer,
  built against the artifact under test, and compares stdout, stderr and exit
  status.

* `DriverRunner` executes the two test drivers the graded build produces from
  upstream's test/example.c and test/minigzip.c.  These are the submission's own
  programs rather than the verifier's, which makes them the most end-to-end cases
  here.

None of them holds an expected value of its own.  Expected output is whatever the
pinned C reference produced under identical inputs, which is why quirks are
graded as behavior rather than as defects to be corrected.

The two sides are reached differently, so every runner holds a **command prefix
list** rather than a binary path.  The reference side is a binary -- probe.c and
consumer.c compiled against the pinned C install -- and the submission side is a
class that only means something inside a JVM, reached as `java -p <jar>
--add-modules org.zlib -cp <classes> Probe`.  Both go through the same code: the
reference's prefix happens to be a one-element list.

That is also what makes the two linkage modes gradable.  A jar on the module path
and the same jar on the class path are different programs to the JVM -- a missing
module-info breaks the first and leaves the second working, and a package that is
not exported does the reverse -- so `@consumer` and `@consumer-cp` resolve to two
different prefixes and are compared against the same frozen C output.

Nothing here sets PKG_CONFIG_*.  State B installs no .pc file -- it is on the
contract's forbidden install list, because a Java artifact has no compile flags
to publish -- so that environment would describe a file that cannot exist.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

import vlib
from vlib import Log, Result

BATCH_SIZE = 250
PROBE_TIMEOUT = 300.0
CLI_TIMEOUT = 60.0
# The drivers are whole programs run on one payload each; the largest corpus
# document is under 200 KB, so a driver that needs longer than this is stuck
# rather than busy.
DRIVER_TIMEOUT = 120.0

# A pathological input that is quadratic instead of linear will hit this; that
# is a real failure for a library fed untrusted input, so the timeout is graded
# rather than retried.
#
# Enforced as an *idle* bound rather than a per-case wall clock: the probe emits
# one self-delimiting record per case and flushes it, so "no record for 30s" is
# the same observation as "this case has not finished in 30s" without the runner
# having to know when the case started.  PROBE_TIMEOUT still bounds the batch as
# a whole; this bounds the stall inside it, and the difference is a factor of ten
# per hang.  lang02/high paid for the version where only the batch was bounded: a
# Java deflate that stalls on chunked feeding hit nine hangs at the full 300s,
# 2700s of a 3005s module against a 2400s budget, so the runner killed the module
# and all 183 checks were lost -- including the 88 that had passed.
SINGLE_CASE_TIMEOUT = 30.0


# Held back from the module's budget so there is time to write the result file.
# The behavioural runner's contract says a module that means to survive its deadline
# has to reserve a slice and check the clock -- ignoring it is not an error, it just
# means the module reports nothing if it overruns, and nothing is scored 0.0.
BATCH_RESERVE_SEC = 20.0


def seconds_left() -> float | None:
    """Seconds until the behavioural runner's deadline, or None if it set none.

    ``SRB_MODULE_DEADLINE`` is an absolute unix time, which is why it is preferred
    here over ``SRB_MODULE_TIMEOUT_SEC``: this code runs well after the module
    started and has no record of when that was.

    None rather than infinity when the variable is missing or unparseable, so the
    caller decides what no deadline means.  A module run by hand (``driver.py
    --module streaming``) has no deadline and must not be cut short by one.
    """
    raw = os.environ.get("SRB_MODULE_DEADLINE")
    if not raw:
        return None
    try:
        return float(raw) - vlib.now()
    except (TypeError, ValueError):
        return None


@dataclass
class ProbeRecord:
    case_id: str
    status: str
    payload: bytes


def parse_records(stream: bytes) -> tuple[list[ProbeRecord], bool]:
    """Parse the probe's output.

    Returns the records and whether the stream ended mid-record (which is what a
    crash looks like from the outside).
    """
    records: list[ProbeRecord] = []
    pos = 0
    truncated = False
    while pos < len(stream):
        newline = stream.find(b"\n", pos)
        if newline < 0:
            truncated = True
            break
        header = stream[pos:newline]
        if not header.startswith(b"#CASE\t"):
            # Anything not a header means the child wrote something unexpected
            # to stdout; skip the line and keep going so one stray write does
            # not void the run.
            pos = newline + 1
            continue
        parts = header.split(b"\t")
        if len(parts) != 4:
            pos = newline + 1
            continue
        try:
            length = int(parts[3])
        except ValueError:
            pos = newline + 1
            continue
        start = newline + 1
        end = start + length
        if end > len(stream):
            truncated = True
            break
        payload = stream[start:end]
        records.append(
            ProbeRecord(
                case_id=parts[1].decode("utf-8", "replace"),
                status=parts[2].decode("utf-8", "replace"),
                payload=payload,
            )
        )
        pos = end + 1  # skip the record's trailing newline
    return records, truncated


class ProbeRunner:
    """Drives one probe -- the C binary or a JVM -- over the stdin protocol.

    `command` is the argv prefix that starts the probe; the corpus and scratch
    directories are appended to it.  For the reference that is `[probe-binary]`,
    and for a submission it is the whole `java -p ... Probe` invocation.  Taking a
    prefix rather than a path is what lets one runner serve both sides, and it is
    also what makes the two linkage modes runnable without a second class.
    """

    def __init__(
        self,
        command: list[str],
        corpus_dir: Path,
        scratch_dir: Path,
        log: Log,
        *,
        label: str,
        env: dict | None = None,
    ) -> None:
        if not command:
            raise SystemExit("ProbeRunner: empty command")
        self.command = [str(part) for part in command]
        self.corpus_dir = corpus_dir
        # The gzfile cases go through gzopen, so the probe needs somewhere to
        # write.  It is given a directory of its own per label, so the reference
        # and the submission cannot see each other's files.
        self.scratch_dir = scratch_dir
        self.scratch_dir.mkdir(parents=True, exist_ok=True)
        self.log = log
        self.label = label
        self.env = env or vlib.base_env()
        self.crashes: list[dict] = []
        #: Assertion ids the deadline stopped this runner from attempting.  Kept
        #: apart from `crashes`: a crash is an observation about the submission,
        #: and this is an observation about the clock.
        self.abandoned: list[str] = []

    def _invoke(self, lines: list[str], timeout: float,
                idle_timeout: float | None = None) -> Result:
        # Read at call time, not captured as a default argument: a default would
        # snapshot SINGLE_CASE_TIMEOUT at import, so the constant at the top of
        # this file and the bound actually enforced could disagree.  That is the
        # same shape of defect as the one this bound exists to fix -- a value
        # declared in one place and not the one in force.
        if idle_timeout is None:
            idle_timeout = SINGLE_CASE_TIMEOUT
        payload = ("\n".join(lines) + "\n").encode("utf-8")
        return vlib.run_streaming(
            self.command + [str(self.corpus_dir), str(self.scratch_dir)],
            env=self.env,
            timeout=timeout,
            idle_timeout=idle_timeout,
            stdin_data=payload,
            label=f"probe-{self.label}",
            # Truncation here is indistinguishable from a crash: parse_records
            # would report truncated=True and _drive would invent a victim.
            full_capture=True,
        )

    def run(self, rows: list[list[str]]) -> dict[str, ProbeRecord]:
        """Execute assertion rows, returning payloads keyed by assertion id."""
        out: dict[str, ProbeRecord] = {}
        pending = list(rows)
        self.log.write(
            f"probe[{self.label}]: {len(pending)} assertions in batches of {BATCH_SIZE}"
        )
        batch_index = 0
        while pending:
            # The module's deadline, checked between batches.  Past it the runner
            # SIGKILLs this process and the module reports nothing at all -- so the
            # last batch that can be afforded is worth more than an attempt at the
            # next one, and the rows not attempted are named rather than left to
            # look like results the submission failed to produce.
            left = seconds_left()
            if left is not None and left <= BATCH_RESERVE_SEC:
                self.abandoned.extend(row[0] for row in pending)
                self.log.write(
                    f"probe[{self.label}]: {left:.0f}s of the module budget left, "
                    f"under the {BATCH_RESERVE_SEC:g}s reserved to publish in; "
                    f"{len(pending)} assertion(s) not attempted"
                )
                break
            batch = pending[:BATCH_SIZE]
            pending = pending[BATCH_SIZE:]
            batch_index += 1
            self._drive(batch, out, batch_index)
        self.log.write(
            f"probe[{self.label}]: {len(out)} records, {len(self.crashes)} crash(es)"
            + (f", {len(self.abandoned)} not attempted" if self.abandoned else "")
        )
        return out

    def _drive(
        self, batch: list[list[str]], out: dict[str, ProbeRecord], batch_index: int
    ) -> None:
        """Run a batch, recovering from a crash by resuming after the victim."""
        remaining = list(batch)
        attempt = 0
        while remaining:
            attempt += 1
            lines = ["\t".join(row) for row in remaining]
            # The batch window is also capped by what is left of the module budget.
            # Without this the restart loop can outlive the module: it allows up to
            # len(batch)+4 attempts, so 250 cases at the full PROBE_TIMEOUT is 21
            # hours against a budget measured in tens of minutes, and the module is
            # killed mid-batch having written nothing.
            left = seconds_left()
            window = PROBE_TIMEOUT
            if left is not None:
                window = min(window, max(1.0, left - BATCH_RESERVE_SEC))
            result = self._invoke(lines, window)
            records, truncated = parse_records(result.stdout)
            for record in records:
                out[record.case_id] = record
            produced = len(records)
            if result.ok and not truncated and produced >= len(remaining):
                return
            # The child died or stalled.  Everything it emitted is kept; the
            # case it was working on when it stopped is the victim.
            victim_row = remaining[produced] if produced < len(remaining) else None
            victim_id = victim_row[0] if victim_row else "<unknown>"
            self.crashes.append(
                {
                    "label": self.label,
                    "batch": batch_index,
                    "attempt": attempt,
                    "assert_id": victim_id,
                    "returncode": result.returncode,
                    "timed_out": result.timed_out,
                    "produced": produced,
                    "expected": len(remaining),
                    "stderr": result.stderr.decode("utf-8", "replace")[:2000],
                }
            )
            # An idle stall and a batch overrun are both timeouts, and naming which
            # one matters when reading the crash list: a stall says one case stopped
            # making progress and the cases after it were never tried, while an
            # overrun says the batch as a whole was too slow.  The first is charged
            # to `victim_id`; the second is not really attributable to it at all.
            signal_note = (
                ("stalled" if result.idle_timeout_hit else "timeout")
                if result.timed_out
                else f"rc={result.returncode}"
            )
            self.log.write(
                f"probe[{self.label}]: aborted at '{victim_id}' ({signal_note}); "
                f"{produced}/{len(remaining)} records kept"
            )
            if victim_row is None:
                return
            # Record the victim as a hard failure and continue after it.
            out[victim_id] = ProbeRecord(
                case_id=victim_id,
                status=f"crash:{signal_note}",
                payload=b"",
            )
            remaining = remaining[produced + 1 :]
            if attempt > len(batch) + 4:
                self.log.write(f"probe[{self.label}]: too many restarts; abandoning batch")
                self.abandoned.extend(row[0] for row in remaining)
                return
            left = seconds_left()
            if left is not None and left <= BATCH_RESERVE_SEC and remaining:
                # Same reserve as between batches, checked here too: a batch that
                # keeps stalling restarts inside this loop and would otherwise spend
                # the whole budget without ever returning to the caller's check.
                self.log.write(
                    f"probe[{self.label}]: {left:.0f}s left of the module budget; "
                    f"{len(remaining)} assertion(s) in this batch not attempted")
                self.abandoned.extend(row[0] for row in remaining)
                return


def normalizer(paths: dict[str, Path]):
    """Replace scratch and install paths with stable placeholders.

    Reference and submission run out of different directories, so a message that
    quotes a path differs for a reason that is not a behavior difference.  Only
    these known locations are rewritten -- nothing else is touched, because
    blanket normalization is how a real difference gets edited away.  The
    substitutions are applied longest-first so a prefix never shadows a path
    nested inside it.
    """
    subs = sorted(
        ((str(v).encode(), f"<{k}>".encode()) for k, v in paths.items()),
        key=lambda pair: len(pair[0]),
        reverse=True,
    )

    def apply(data: bytes) -> bytes:
        for needle, placeholder in subs:
            if needle:
                data = data.replace(needle, placeholder)
        return data

    return apply


def signature(result: Result, normalize) -> bytes:
    """Everything a process contract covers, in a comparable form."""
    return b"\n".join(
        [
            b"rc=" + str(result.returncode).encode(),
            b"timeout=" + (b"1" if result.timed_out else b"0"),
            b"--stdout--",
            normalize(result.stdout),
            b"--stderr--",
            normalize(result.stderr),
        ]
    )


class CommandRunner:
    """Runs one process per case: the verifier's consumer, built against the
    artifact under test.

    The consumer is built twice, once against the reference install and once
    against the submission's, so the program being run differs between the two
    sides by construction.  What is compared is what it printed, which is a
    statement about the library it was linked against rather than about the
    compiler.

    Two sentinels, not one.  `@consumer` runs it with the artifact on the module
    path and `@consumer-cp` with the artifact on the class path, because those are
    two different linkages and they fail apart: a jar whose module-info is missing,
    misnamed, or does not export the package still works on the class path, and a
    class path run cannot see a module descriptor at all.  A task that graded only
    one of them would accept an artifact that half of its downstreams could not
    consume.  Both are compared against the same frozen C output -- the C reference
    has one linkage and answers both sentinels -- so the two modes are held to the
    same behavior rather than to each other.
    """

    def __init__(
        self,
        log: Log,
        *,
        label: str,
        prefix: Path,
        consumer: list[str] | None,
        consumer_cp: list[str] | None = None,
        scratch: Path,
        env: dict | None = None,
    ) -> None:
        self.log = log
        self.label = label
        # Absolute, always: children run with their own cwd, so a relative path
        # would be checked here against one directory and executed against
        # another.  That disagreement does not fail loudly -- it fails as a
        # missing file inside the child.
        self.prefix = prefix.resolve()
        # Command prefixes rather than paths, for the reason in the module
        # docstring.  The reference side holds one compiled binary and passes it as
        # both, because a C library has one linkage and the same program answers
        # both sentinels.
        #
        # No fallback from consumer_cp to consumer.  It would read as a
        # convenience, and it is the one substitution that must not happen: on the
        # submission side the two are separate javac invocations against the same
        # jar, and a class-path compile that failed has to report its cases as not
        # run.  Filling them in from the module-path build would grade a program
        # the class-path linkage never produced, which is precisely the failure
        # those cases exist to find.
        self.consumer = [str(part) for part in consumer] if consumer else None
        self.consumer_cp = (
            [str(part) for part in consumer_cp] if consumer_cp else None
        )
        self.scratch = scratch.resolve()
        self.scratch.mkdir(parents=True, exist_ok=True)
        self.env = env or vlib.base_env()
        self.normalize = normalizer(
            {"prefix": self.prefix, "scratch": self.scratch}
        )

    def resolve(self, case: dict) -> list[str] | None:
        argv = list(case.get("argv") or [])
        if not argv:
            return None
        prefix = None
        if argv[0] == "@consumer":
            prefix = self.consumer
        elif argv[0] == "@consumer-cp":
            prefix = self.consumer_cp
        if prefix is not None:
            # One scratch directory per case id, shared by both linkage modes:
            # the two runs of a given op are separated by the sentinel being part
            # of the id, so they never collide, and a mode that wrote into the
            # other's directory would be visible as a diff rather than silent.
            case_scratch = self.scratch / case["id"].replace("/", "_")
            case_scratch.mkdir(parents=True, exist_ok=True)
            return prefix + [str(case_scratch)] + argv[1:]
        if argv[0].startswith("@"):
            # An unresolved sentinel is a catalog defect, not a submission
            # failure, and silently execing it would look like a missing file.
            raise SystemExit(f"CommandRunner: unknown sentinel {argv[0]!r}")
        return argv

    def run_case(self, case: dict) -> tuple[Result | None, bytes]:
        argv = self.resolve(case)
        if argv is None:
            return None, b""
        result = vlib.run(
            argv,
            cwd=self.scratch,
            env=self.env,
            timeout=CLI_TIMEOUT,
            # These bytes are the signature being compared; a cut at 256 KiB
            # could make two different outputs look identical.
            full_capture=True,
        )
        return result, signature(result, self.normalize)


class DriverRunner:
    """Runs the two test drivers the graded build produces.

    Unlike every other executor here, the program being run is the submission's
    own: test/example.c and test/minigzip.c, rewritten and built by the graded
    CMake.  That makes these the most end-to-end cases in the catalog, and also
    the ones with the fewest degrees of freedom -- the drivers take no verifier
    protocol, they just run.

    The drivers are not installed, so they are found in the build directory.
    Each case gets its own working directory because `example` writes foo.gz into
    the current directory and minigzip's file mode writes next to its input.

    The contract requires each driver to be an executable file the harness can
    exec directly -- in practice a wrapper that starts a JVM -- so this runner
    execs a path.  It sets no LD_LIBRARY_PATH: there is no shared object to find,
    and a search path for a file State B does not produce describes nothing.  The
    wrapper has to locate its own jar, which is part of what the driver contract
    is asking for: a driver that only runs because the harness pointed it at
    something is a driver a developer could not run.
    """

    def __init__(
        self,
        log: Log,
        *,
        label: str,
        build_dir: Path,
        corpus_dir: Path,
        scratch: Path,
        prefix: Path,
        env: dict | None = None,
    ) -> None:
        self.log = log
        self.label = label
        # Absolute for the same reason as in CommandRunner: resolve() below
        # tests for an executable that the child then has to find from its own
        # working directory.
        self.build_dir = build_dir.resolve()
        self.corpus_dir = corpus_dir.resolve()
        self.scratch = scratch.resolve()
        self.scratch.mkdir(parents=True, exist_ok=True)
        self.prefix = prefix.resolve()
        # base_env() exports JAVA_HOME and a PATH that includes /usr/bin, where
        # the image's `java` alternative lives, so a wrapper can find a JVM by
        # either route.  Nothing beyond that is provided: the wrapper is the
        # submission's, and a driver that needs the harness to tell it where its
        # own jar is would not run for anyone else.
        self.env = env or vlib.base_env()
        self.normalize = normalizer(
            {"prefix": self.prefix, "build": self.build_dir,
             "corpus": self.corpus_dir, "scratch": self.scratch}
        )
        self.missing: set[str] = set()

    def _blob(self, index: int) -> bytes:
        return (self.corpus_dir / "blobs" / f"{index:05d}.bin").read_bytes()

    def resolve(self, argv: list[str]) -> list[str] | None:
        """Map the @driver sentinel to a built, executable driver.

        The executable bit is checked separately from existence.  The contract's
        `form` clause is that the harness execs the path, so a driver delivered as
        a non-executable file -- a plain `.sh`, or a jar someone renamed -- has
        missed the requirement in a way that would otherwise surface as an opaque
        permission error inside every driver case.
        """
        name = argv[0]
        if not name.startswith("@"):
            return argv
        driver = self.build_dir / name[1:]
        if not driver.is_file():
            self.missing.add(name[1:])
            return None
        if not os.access(driver, os.X_OK):
            self.missing.add(f"{name[1:]} (present but not executable)")
            return None
        return [str(driver)] + argv[1:]

    def _case_dir(self, case: dict) -> Path:
        out = self.scratch / case["id"].replace("/", "_")
        shutil.rmtree(out, ignore_errors=True)
        out.mkdir(parents=True, exist_ok=True)
        return out

    def _exec(self, argv: list[str], cwd: Path, stdin_data: bytes) -> Result:
        return vlib.run(
            argv,
            cwd=cwd,
            env=self.env,
            timeout=DRIVER_TIMEOUT,
            stdin_data=stdin_data,
            full_capture=True,
        )

    def run_case(self, case: dict) -> tuple[Result | None, bytes]:
        argv = self.resolve(list(case.get("argv") or []))
        if argv is None:
            return None, b""
        params = case.get("params") or {}
        mode = params.get("mode", "stdout")
        work = self._case_dir(case)
        handler = getattr(self, f"_mode_{mode.replace('-', '_')}", None)
        if handler is None:
            raise SystemExit(f"unknown driver mode: {mode}")
        return handler(argv, work, params)

    # -- modes ------------------------------------------------------------

    def _mode_example(self, argv, work: Path, params) -> tuple[Result, bytes]:
        """Run the self-test driver and compare its whole transcript.

        The driver writes foo.gz in its working directory and reads it back, so
        the file it produced is folded into the signature too: the transcript
        alone would not notice a gzip container that decodes correctly through
        this library but differs on the wire.
        """
        result = self._exec(argv, work, b"")
        parts = [signature(result, self.normalize)]
        produced = sorted(p for p in work.iterdir() if p.is_file())
        parts.append(b"--files--")
        for path in produced:
            data = path.read_bytes()
            parts.append(
                f"{path.name} {len(data)} ".encode() + vlib.sha256_bytes(data).encode()
            )
            if len(data) <= 4096:
                parts.append(data.hex().encode())
        return result, b"\n".join(parts)

    def _mode_stdout(self, argv, work: Path, params) -> tuple[Result, bytes]:
        """Compress a payload to stdout; the gzip stream is compared exactly."""
        data = self._blob(int(params["doc"]))
        result = self._exec(argv, work, data)
        return result, signature(result, self.normalize)

    def _mode_gzin(self, argv, work: Path, params) -> tuple[Result, bytes]:
        """Decompress a stream the system gzip produced.

        The input comes from outside the library under test, so no amount of
        internal self-consistency helps here.  -n keeps gzip's output a function
        of its input alone, so the case stays reproducible.
        """
        data = self._blob(int(params["doc"]))
        level = int(params.get("gzip_level", 6))
        plain = work / "plain.bin"
        plain.write_bytes(data)
        made = vlib.run(
            ["gzip", "-n", f"-{level}", "-c", str(plain)],
            cwd=work,
            env=vlib.base_env(),
            timeout=CLI_TIMEOUT,
            full_capture=True,
        )
        if not made.ok:
            raise SystemExit(f"system gzip failed while staging: {made.tail()}")
        result = self._exec(argv, work, made.stdout)
        parts = [
            signature(result, self.normalize),
            b"--matches-input--",
            b"1" if result.stdout == data else b"0",
        ]
        return result, b"\n".join(parts)

    def _mode_roundtrip(self, argv, work: Path, params) -> tuple[Result, bytes]:
        """Compress then decompress, and require the original bytes back."""
        data = self._blob(int(params["doc"]))
        forward = self._exec(argv, work, data)
        # The decompressing half drops any level or strategy flag: those select
        # how to compress and are not part of reading a stream back.
        back_argv = [argv[0], "-d"]
        backward = self._exec(back_argv, work, forward.stdout)
        parts = [
            b"--forward--",
            signature(forward, self.normalize),
            b"--backward--",
            signature(backward, self.normalize),
            b"--recovered--",
            b"1" if backward.stdout == data else b"0",
            f"len={len(backward.stdout)} of {len(data)}".encode(),
        ]
        return backward, b"\n".join(parts)

    def _mode_file(self, argv, work: Path, params) -> tuple[Result, bytes]:
        """The named-file path: gzopen plus the .gz suffix logic.

        minigzip compresses `data` into `data.gz` and removes the original, then
        the reverse direction restores it.  The archive's bytes are part of the
        signature, and the system gzip is asked to validate it as well, so the
        file path is held to the same wire-format standard as the stream path.
        """
        data = self._blob(int(params["doc"]))
        target = work / "data"
        target.write_bytes(data)
        forward = self._exec(argv + [str(target)], work, b"")
        archive = work / "data.gz"
        parts = [b"--compress--", signature(forward, self.normalize)]
        parts.append(b"--original-removed--")
        parts.append(b"1" if not target.exists() else b"0")
        if archive.is_file():
            blob = archive.read_bytes()
            parts.append(b"--archive--")
            parts.append(
                f"{len(blob)} ".encode() + vlib.sha256_bytes(blob).encode()
            )
            validated = vlib.run(
                ["gzip", "-t", str(archive)],
                cwd=work,
                env=vlib.base_env(),
                timeout=CLI_TIMEOUT,
            )
            parts.append(b"--gzip-validates--")
            parts.append(str(validated.returncode).encode())
            backward = self._exec([argv[0], "-d", str(archive)], work, b"")
            parts.append(b"--decompress--")
            parts.append(signature(backward, self.normalize))
            restored = target.read_bytes() if target.is_file() else None
            parts.append(b"--restored--")
            parts.append(b"1" if restored == data else b"0")
            return backward, b"\n".join(parts)
        parts.append(b"--archive--")
        parts.append(b"MISSING")
        return forward, b"\n".join(parts)
