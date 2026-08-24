#!/usr/bin/env python3
"""Case execution and differential comparison.

Two executors:

* `ProbeRunner` drives the compiled probe over the protocol on stdin.  Cases run
  in batches for speed, but each record is self-delimiting and flushed as it is
  produced, so a crash is attributed to exactly one case rather than voiding the
  batch -- the surviving records still count, and the batch is re-driven from
  the case after the crash.

* `CliRunner` executes the installed `cmark` once per case and compares stdout,
  stderr and exit status.

Neither holds an expected value of its own.  Expected output is whatever the
pinned C reference produced under identical inputs, which is why quirks are
graded as behavior rather than as defects to be corrected.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import vlib
from vlib import Log, Result

BATCH_SIZE = 250
PROBE_TIMEOUT = 300.0
CLI_TIMEOUT = 60.0

# A pathological input that is quadratic instead of linear will hit this; that
# is a real failure for a library fed untrusted input, so the timeout is graded
# rather than retried.
SINGLE_CASE_TIMEOUT = 30.0


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
    def __init__(
        self,
        binary: Path,
        corpus_dir: Path,
        log: Log,
        *,
        label: str,
        env: dict | None = None,
    ) -> None:
        self.binary = binary
        self.corpus_dir = corpus_dir
        self.log = log
        self.label = label
        self.env = env or vlib.base_env()
        self.crashes: list[dict] = []

    def _invoke(self, lines: list[str], timeout: float) -> Result:
        payload = ("\n".join(lines) + "\n").encode("utf-8")
        return vlib.run(
            [str(self.binary), str(self.corpus_dir)],
            env=self.env,
            timeout=timeout,
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
            batch = pending[:BATCH_SIZE]
            pending = pending[BATCH_SIZE:]
            batch_index += 1
            self._drive(batch, out, batch_index)
        self.log.write(
            f"probe[{self.label}]: {len(out)} records, {len(self.crashes)} crash(es)"
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
            result = self._invoke(lines, PROBE_TIMEOUT)
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
            signal_note = (
                "timeout"
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
                return


class CliRunner:
    def __init__(
        self,
        binary: Path,
        corpus_dir: Path,
        log: Log,
        *,
        label: str,
        env: dict | None = None,
        workdir: Path | None = None,
    ) -> None:
        self.binary = binary
        self.corpus_dir = corpus_dir
        self.log = log
        self.label = label
        self.env = env or vlib.base_env()
        self.workdir = workdir

    def _doc_path(self, index: int) -> Path:
        return self.corpus_dir / f"{index:05d}.md"

    def run_case(self, case: dict) -> Result:
        argv = [str(self.binary)]
        files = (case.get("params") or {}).get("files") or []
        for token in case.get("argv", []):
            if token.startswith("@FILE"):
                slot = int(token[5:])
                argv.append(str(self._doc_path(files[slot])))
            else:
                argv.append(token)
        stdin_doc = case.get("stdin_doc")
        stdin_data = b""
        if stdin_doc is not None:
            stdin_data = self._doc_path(stdin_doc).read_bytes()
        return vlib.run(
            argv,
            cwd=self.workdir,
            env=self.env,
            timeout=CLI_TIMEOUT,
            stdin_data=stdin_data,
            # These bytes are the signature being compared; a cut at 256 KiB
            # could make two different outputs look identical.
            full_capture=True,
        )

    def signature(self, case: dict, result: Result) -> bytes:
        """Everything the CLI contract covers, in a comparable form.

        The binary's own path differs between reference and submission, so it is
        normalized out of diagnostics; corpus paths are normalized too, since
        they are scratch locations rather than part of the behavior.
        """
        def normalize(data: bytes) -> bytes:
            out = data.replace(str(self.binary).encode(), b"<cmark>")
            out = out.replace(str(self.corpus_dir).encode(), b"<corpus>")
            return out

        parts = [
            b"rc=" + str(result.returncode).encode(),
            b"timeout=" + (b"1" if result.timed_out else b"0"),
            b"--stdout--",
            normalize(result.stdout),
            b"--stderr--",
            normalize(result.stderr),
        ]
        return b"\n".join(parts)
