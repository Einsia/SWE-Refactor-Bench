#!/usr/bin/env python3
"""Produces and stores the expected results, from the pinned C reference.

No expected output in this task is hand-written.  Every behavioural expectation is
whatever the pinned reference implementation actually does, captured by running
the same probe and the same CLI cases against a reference install built from the
same tarball the agent started from.  That choice has a consequence worth being
explicit about: the reference's quirks are graded as behavior.  If cmark 0.31.1
emits a stray blank line or resolves an ambiguous nesting oddly, the submission
has to do the same.  For a compatibility rewrite that is the correct standard --
downstream consumers depend on what the library does, not on what a specification
says it should do.

Expectations are frozen when the verifier image is built, not when a submission
is graded.  A submission therefore cannot perturb the reference: by the time its
code is in the container, the answers already exist and are read-only.  The
digest recorded alongside them lets the verifier detect at runtime that it is
reading the expectations it was built with.

Payloads live in a single blob with a JSON index of offsets.  Storing them whole
rather than as hashes means a failure can be shown as a real diff.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import vlib
from vlib import Log

ORACLE_VERSION = "swerefactor-oracle-v1"


@dataclass
class Expectation:
    key: str
    status: str
    digest: str
    offset: int
    length: int


class ExpectationStore:
    """A blob of payloads plus an index, written once and read many times."""

    def __init__(self, index_path: Path, blob_path: Path) -> None:
        self.index_path = index_path
        self.blob_path = blob_path
        self.entries: dict[str, Expectation] = {}
        self.meta: dict = {}
        self._blob: bytes | None = None

    # -- writing ---------------------------------------------------------

    def write(self, records: dict[str, tuple[str, bytes]], meta: dict) -> str:
        """Serialize records as {key: (status, payload)} and return the digest."""
        offsets: dict[str, Expectation] = {}
        chunks: list[bytes] = []
        cursor = 0
        for key in sorted(records):
            status, payload = records[key]
            offsets[key] = Expectation(
                key=key,
                status=status,
                digest=vlib.sha256_bytes(payload),
                offset=cursor,
                length=len(payload),
            )
            chunks.append(payload)
            cursor += len(payload)
        blob = b"".join(chunks)
        self.blob_path.write_bytes(blob)

        index = {
            "version": ORACLE_VERSION,
            "meta": meta,
            "blob_sha256": vlib.sha256_bytes(blob),
            "blob_bytes": len(blob),
            "count": len(offsets),
            "entries": {
                key: [e.status, e.digest, e.offset, e.length]
                for key, e in sorted(offsets.items())
            },
        }
        digest = digest_of(index)
        index["digest"] = digest
        vlib.write_json(self.index_path, index)
        self.entries = offsets
        self.meta = meta
        return digest

    # -- reading ---------------------------------------------------------

    def load(self) -> str:
        index = vlib.read_json(self.index_path)
        if index.get("version") != ORACLE_VERSION:
            raise RuntimeError(f"oracle version mismatch: {index.get('version')}")
        self.meta = index.get("meta", {})
        self.entries = {
            key: Expectation(
                key=key, status=row[0], digest=row[1], offset=row[2], length=row[3]
            )
            for key, row in index["entries"].items()
        }
        recorded = index.get("digest")
        stated = dict(index)
        stated.pop("digest", None)
        if digest_of(stated) != recorded:
            raise RuntimeError("oracle index digest mismatch: expectations altered")
        actual_blob = vlib.sha256_file(self.blob_path)
        if actual_blob != index["blob_sha256"]:
            raise RuntimeError("oracle blob digest mismatch: expectations altered")
        return recorded

    def payload(self, key: str) -> bytes:
        entry = self.entries[key]
        if self._blob is None:
            self._blob = self.blob_path.read_bytes()
        return self._blob[entry.offset : entry.offset + entry.length]

    def get(self, key: str) -> Expectation | None:
        return self.entries.get(key)


def digest_of(index: dict) -> str:
    """A stable digest over the index contents, independent of dict order."""
    payload = json.dumps(
        {k: v for k, v in index.items() if k != "digest"},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return vlib.sha256_bytes(payload)


def probe_rows_for(catalog: dict) -> list[list[str]]:
    """Every probe assertion row in the catalog, flattened and deduplicated."""
    rows: list[list[str]] = []
    seen: set[str] = set()
    for case in catalog["cases"]:
        if case["kind"] != "probe":
            continue
        for row in case["asserts"]:
            key = row[0]
            if key in seen:
                raise RuntimeError(f"duplicate assertion id: {key}")
            seen.add(key)
            rows.append(row)
    return rows


def cli_cases_for(catalog: dict) -> list[dict]:
    return [case for case in catalog["cases"] if case["kind"] == "cli"]


def collect(
    catalog: dict,
    probe_binary: Path,
    cli_binary: Path,
    corpus_dir: Path,
    log: Log,
    *,
    label: str,
    env: dict | None = None,
) -> tuple[dict[str, tuple[str, bytes]], list[dict]]:
    """Run every probe assertion and CLI case, returning comparable records."""
    import executor

    records: dict[str, tuple[str, bytes]] = {}

    rows = probe_rows_for(catalog)
    runner = executor.ProbeRunner(
        probe_binary, corpus_dir, log, label=label, env=env
    )
    probe_records = runner.run(rows)
    for key, record in probe_records.items():
        records[f"probe/{key}"] = (record.status, record.payload)

    cli = executor.CliRunner(cli_binary, corpus_dir, log, label=label, env=env)
    cli_cases = cli_cases_for(catalog)
    log.write(f"cli[{label}]: {len(cli_cases)} cases")
    for case in cli_cases:
        result = cli.run_case(case)
        status = "timeout" if result.timed_out else f"rc:{result.returncode}"
        records[f"cli/{case['id']}"] = (status, cli.signature(case, result))

    return records, runner.crashes


def freeze(
    catalog: dict,
    corpus_meta: dict,
    probe_binary: Path,
    cli_binary: Path,
    corpus_dir: Path,
    out_dir: Path,
    log: Log,
) -> str:
    """Build the expectation store from the reference install."""
    log.section("freeze expectations from reference")
    records, crashes = collect(
        catalog, probe_binary, cli_binary, corpus_dir, log, label="reference"
    )
    if crashes:
        # The reference crashing means the case is not a well-defined behavior and
        # must not be graded.  Fail loudly at image build time rather than
        # shipping a case whose expectation is a signal number.
        raise RuntimeError(
            f"reference crashed on {len(crashes)} case(s): "
            f"{[c.get('case_id') for c in crashes[:8]]}"
        )
    store = ExpectationStore(out_dir / "expectations.json", out_dir / "expectations.bin")
    meta = {
        "catalog_digest": catalog["digest"],
        "catalog_cases": catalog["counts"]["total"],
        "corpus_digest": corpus_meta["digest"],
        "corpus_documents": corpus_meta["count"],
        "reference_version": corpus_meta.get("reference_version"),
    }
    digest = store.write(records, meta)
    log.write(f"expectations: {len(records)} records, digest {digest}")
    return digest


def counts_by_prefix(store: ExpectationStore) -> dict[str, int]:
    out: dict[str, int] = {}
    for key in store.entries:
        head = key.split("/", 2)
        bucket = "/".join(head[:2]) if len(head) > 1 else head[0]
        out[bucket] = out.get(bucket, 0) + 1
    return out
