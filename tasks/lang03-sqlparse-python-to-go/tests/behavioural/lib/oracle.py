#!/usr/bin/env python3
"""Produces and stores the expected answers, from the pinned Python reference.

Nothing here is hand-written.  Every behavioural expectation is whatever sqlparse
0.5.3 actually does, captured by driving the same probe protocol and the same CLI
cases against a reference install unpacked from the same tarball the agent
started from.  That has a consequence worth stating plainly: the reference's
quirks are the graded behavior.  Where sqlparse indents oddly, mis-splits a
statement or raises on something arguably valid, the port has to do the same.
For a compatibility rewrite that is the right standard -- downstream code depends
on what the library does, not on what the SQL standard says.

Expectations are frozen when the verifier image is built, not when a submission
is graded.  By the time a submission's code exists in a container, the answers
are already on disk and read-only, so the submission cannot perturb the
reference.  The digest stored alongside them lets the verifier confirm at run
time that it is reading the answers it was built with.

Payloads are kept whole in one blob with a JSON index of offsets, rather than
reduced to hashes, so a failure can be reported as a real diff instead of "digest
mismatch".

One status is special.  The probe answers `ok` or `err`, and `err` is a graded
answer -- "this input raises" is behavior a port must reproduce.  `defect` means
the probe itself is broken, and it must never appear.  If the reference produces
one at freeze time the image build fails; the alternative is shipping a case
whose expected answer is a bug in the harness.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import vlib
from vlib import Log

ORACLE_VERSION = "swerefactor-lang03-oracle-v1"

# Statuses that mean the harness failed rather than the implementation answered.
BROKEN_STATUSES = ("defect", "timeout")


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
        self.blob_path.parent.mkdir(parents=True, exist_ok=True)
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
        """Load and self-verify.

        Both digests are re-checked on every load.  The store is baked into the
        verifier image, so a mismatch is not a submission failure -- it means the
        image or the mount is wrong, and the caller turns it into "verifier
        corrupted" rather than a score of zero.
        """
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
        actual = vlib.sha256_file(self.blob_path)
        if actual != index["blob_sha256"]:
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
def probe_cases_for(catalog: dict) -> list[dict]:
    """Every probe case, in catalog order, with ids checked unique.

    One case is one wire request here, unlike lang01's grouped assertions.  The
    uniqueness check is cheap and catches a generator that emitted a case twice --
    which would otherwise silently halve one op's weight.
    """
    out: list[dict] = []
    seen: set[str] = set()
    for case in catalog["cases"]:
        if case.get("kind") != "probe":
            continue
        if case["id"] in seen:
            raise RuntimeError(f"duplicate probe case id: {case['id']}")
        seen.add(case["id"])
        out.append(case)
    return out


def cli_cases_for(catalog: dict) -> list[dict]:
    return [case for case in catalog["cases"] if case.get("kind") == "cli"]


def cases_by_tier(cases: list[dict]) -> dict[str, list[dict]]:
    """Group probe cases by the tier whose binary can answer them."""
    grouped: dict[str, list[dict]] = {}
    for case in cases:
        grouped.setdefault(case.get("tier", ""), []).append(case)
    return grouped


def collect(
    catalog: dict,
    probe_driver,
    cli_runner,
    log: Log,
    *,
    label: str,
) -> tuple[dict[str, tuple[str, bytes]], list[dict]]:
    """Answer every probe and CLI case, returning records keyed for the store.

    The probe driver is an object with `.run(cases) -> dict[id, Record]`, not a
    binary path.  That is what lets one function serve both halves: the reference
    driver is a single Python process that answers every tier, while the
    submission driver runs four tier binaries and routes each case to the one
    whose compile closure contains its op.  Neither the oracle nor the grader
    needs to know which it is holding.
    """
    records: dict[str, tuple[str, bytes]] = {}

    probe_cases = probe_cases_for(catalog)
    log.write(f"probe[{label}]: {len(probe_cases)} cases")
    answers = probe_driver.run(probe_cases)
    for case_id, record in answers.items():
        records[f"probe/{case_id}"] = (record.status, record.payload)

    cli_cases = cli_cases_for(catalog)
    log.write(f"cli[{label}]: {len(cli_cases)} cases")
    for case in cli_cases:
        result = cli_runner.run_case(case)
        status = "timeout" if result.timed_out else f"rc:{result.returncode}"
        records[f"cli/{case['id']}"] = (status, cli_runner.signature(case, result))

    return records, list(getattr(probe_driver, "crashes", []))


def freeze(
    catalog: dict,
    docs_meta: dict,
    probe_driver,
    cli_runner,
    out_dir: Path,
    log: Log,
    *,
    reference_version: str,
    extra_meta: dict | None = None,
) -> tuple[str, dict]:
    """Build the expectation store from the reference install.

    Three conditions abort the image build rather than being written down:
    a case the reference crashed on, a case it answered `defect`, and a case
    that timed out.  All three mean the case has no well-defined expected
    answer, and a case whose expectation is a harness bug would be graded
    against every submission forever.
    """
    log.section(f"freeze expectations from reference (sqlparse {reference_version})")
    records, crashes = collect(catalog, probe_driver, cli_runner, log, label="reference")

    if crashes:
        raise RuntimeError(
            f"reference crashed on {len(crashes)} case(s): "
            f"{[c.get('case_id') for c in crashes[:8]]}"
        )
    broken = sorted(
        key for key, (status, _) in records.items()
        if status in BROKEN_STATUSES or status.startswith("crash:")
    )
    if broken:
        raise RuntimeError(
            f"reference produced a non-answer on {len(broken)} case(s): "
            f"{broken[:8]}"
        )

    expected_total = len(probe_cases_for(catalog)) + len(cli_cases_for(catalog))
    if len(records) != expected_total:
        missing = expected_total - len(records)
        raise RuntimeError(
            f"expectation count mismatch: catalog declares {expected_total} "
            f"runnable cases, store has {len(records)} ({missing} unanswered)"
        )

    store = ExpectationStore(
        out_dir / "expectations.json", out_dir / "expectations.bin"
    )
    tier_counts = {
        tier: len(group)
        for tier, group in sorted(cases_by_tier(probe_cases_for(catalog)).items())
    }
    meta = {
        "catalog_digest": catalog["digest"],
        "catalog_cases": catalog["counts"]["total"],
        "documents_digest": docs_meta["digest"],
        "documents_count": docs_meta.get("count"),
        # Recorded rather than derived: the expectations are only meaningful
        # relative to the version that produced them, and a verifier image built
        # against a different sqlparse would otherwise be undetectable.
        "reference_version": reference_version,
        "probe_cases": len(probe_cases_for(catalog)),
        "cli_cases": len(cli_cases_for(catalog)),
        "tier_counts": tier_counts,
    }
    if extra_meta:
        meta.update(extra_meta)
    digest = store.write(records, meta)
    log.write(
        f"expectations: {len(records)} records "
        f"({meta['probe_cases']} probe, {meta['cli_cases']} cli), digest {digest}"
    )
    log.write("  per tier: " + ", ".join(f"{k}={v}" for k, v in tier_counts.items()))
    return digest, meta


def status_breakdown(records: dict[str, tuple[str, bytes]]) -> dict[str, int]:
    """How many records carry each status -- a freeze-time sanity read.

    A frozen set with no `err` records at all would mean the error-path cases are
    not reaching their error paths, which is a catalog bug that would otherwise
    show up only as a suspiciously high submission score.
    """
    out: dict[str, int] = {}
    for status, _ in records.values():
        out[status] = out.get(status, 0) + 1
    return dict(sorted(out.items()))


def counts_by_prefix(store: ExpectationStore) -> dict[str, int]:
    """How many expectations each key namespace holds.

    Keys are exactly `probe/<id>` and `cli/<id>`, so the bucket is the first
    component.  Splitting further would put every CLI case in a bucket of its
    own and make the count a list.
    """
    out: dict[str, int] = {}
    for key in store.entries:
        bucket = key.split("/", 1)[0]
        out[bucket] = out.get(bucket, 0) + 1
    return dict(sorted(out.items()))
