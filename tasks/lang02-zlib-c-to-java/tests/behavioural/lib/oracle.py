#!/usr/bin/env python3
"""Produces and stores the expected results, from the pinned C reference.

No expected value in this task is hand-written.  Every behavioural expectation is
whatever the pinned reference actually does, captured by running the same probe,
the same consumer commands and the same test drivers against a reference built
from the same tarball the agent started from.  That has a consequence worth
stating plainly: the reference's quirks are graded as behavior.  If zlib 1.3.1
emits a particular byte sequence at level 6 -- and it does, deflate is
deterministic to the bit -- the submission has to emit the same one.  For a
compatibility rewrite that is the correct standard: downstream consumers depend
on what the library does, not on what RFC 1951 permits it to do.

Deflate's determinism is what makes this task gradable at the byte level at all.
A compressor is free by specification to emit any valid stream, so "correct
output" is not a well-defined notion in general -- but a *fixed* compressor at a
fixed level with a fixed strategy and a fixed window emits exactly one stream for
one input, and that stream is what a bit-for-bit compatible rewrite must produce.
The corpus exists to pin the compressor's every decision: match lengths at the
window boundary, the lazy-match cutoff, block splitting, the static-vs-dynamic
Huffman choice.  Any of those decided differently shows up as different bytes.

Expectations are frozen when the verifier image is built, not when a submission
is graded.  A submission therefore cannot perturb the reference: by the time its
code is in the container, the answers already exist and are read-only.  The
digest recorded alongside them lets the verifier detect at runtime that it is
reading the expectations it was built with.

Payloads live in a single blob with a JSON index of offsets.  Storing them whole
rather than as hashes means a failure can be shown as a real diff -- for a task
whose failures are "these 40 bytes differ", a digest mismatch alone would be
almost useless to whoever has to fix it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import vlib
from vlib import Log

ORACLE_VERSION = "swerefactor-oracle-v1"

# The three record namespaces, matching the three executors.  Kept as constants
# because both the freezing side and the grading side have to agree on them, and
# a typo in one of two string literals is the kind of bug that looks like a
# submission failing every case.
NS_PROBE = "probe"
NS_CLI = "cli"
NS_DRIVER = "driver"


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


def keys_for(case: dict) -> list[str]:
    """Every expectation key one case is graded on.

    One place, because the freezing side and the grading side must agree
    key-for-key: an expectation stored under a name nothing looks up is a silent
    hole in the suite, and a lookup with no stored expectation fails a submission
    for the verifier's mistake.
    """
    kind = case["kind"]
    if kind == "probe":
        return [f"{NS_PROBE}/{row[0]}" for row in case.get("asserts", [])]
    if kind == "cli":
        return [f"{NS_CLI}/{case['id']}"]
    if kind == "driver":
        return [f"{NS_DRIVER}/{case['id']}"]
    raise RuntimeError(f"case kind {kind!r} has no expectation namespace")


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


def cases_of_kind(catalog: dict, kind: str) -> list[dict]:
    return [case for case in catalog["cases"] if case["kind"] == kind]


def behavioural_cases(catalog: dict) -> list[dict]:
    """The cases the behavioural score is computed over.

    `driver` is behavioural: those cases run the submission's own example and
    minigzip programs and compare their transcripts and produced files against
    the reference's.  That is a behavioral comparison, and the most end-to-end
    one in the suite -- the only cases where the program under test is the
    submission's rather than the verifier's.
    """
    return [
        case
        for case in catalog["cases"]
        if case["kind"] in (NS_PROBE, NS_CLI, NS_DRIVER)
    ]


@dataclass
class CollectContext:
    """Everything one side of the comparison needs to be run.

    The reference and the submission differ only in these paths, which is the
    point: `collect` takes one of these and cannot tell which side it is running.
    Anything that treated them differently would be comparing the verifier's
    behavior against itself.

    There is deliberately no environment here.  Each executor derives its own from
    the prefix it was handed, and one shared environment would have to clobber the
    others.

    The three instrument fields are command prefixes rather than paths.  On the
    reference side each is a one-element list holding a compiled binary; on the
    submission side each is a whole `java -p <jar> ... <Class>` invocation.  A
    submission's probe is not a file that can be exec'd, so a Path here could only
    have been made to work by having collect() know which side it was running --
    which is exactly what this dataclass exists to prevent.

    consumer_cp is the same consumer reached with the artifact on the class path
    instead of the module path.  The reference passes its one binary as both, since
    a C library has one linkage and the same program answers both sentinels; that
    is what lets the submission's two modes be graded against one frozen
    expectation.  It is stated rather than defaulted: None means "there is no
    class-path consumer", and those cases are then reported as not run instead of
    being served quietly from the module-path build.
    """

    label: str
    prefix: Path
    build_dir: Path
    probe_command: list[str] | None
    consumer: list[str] | None
    scratch: Path
    consumer_cp: list[str] | None = None


def collect(
    catalog: dict,
    ctx: CollectContext,
    corpus_dir: Path,
    log: Log,
) -> tuple[dict[str, tuple[str, bytes]], dict]:
    """Run every behavioural case, returning comparable records plus diagnostics.

    corpus_dir is the directory that *contains* blobs/.  Both the probe and the
    driver runner append blobs/NNNNN.bin to it themselves, so the descended path
    makes every payload read miss: the probe exits 71 naming the path it could not
    open, and the driver runner raises.  Loud, but it fails every case at once, so
    the note is here to keep the cause one line away from the symptom.

    The three executors are driven in increasing order of cost: the probe runs
    thousands of assertions in a handful of batched processes, the commands are
    one process each, and the drivers are one to three processes each with files
    on the side.  Ordering them this way means a submission that cannot even load
    the library reports that in seconds rather than after the whole suite.
    """
    import executor

    records: dict[str, tuple[str, bytes]] = {}
    diagnostics: dict = {"crashes": [], "missing_drivers": [], "not_run": []}

    # -- probe ------------------------------------------------------------
    rows = probe_rows_for(catalog)
    if ctx.probe_command:
        # The reference probe was linked with an rpath naming this tree's libdir,
        # so it finds its library without LD_LIBRARY_PATH -- a probe that needed
        # the variable would be hiding a missing SONAME.  The submission's probe
        # carries its own module path in the command, for the same reason: what it
        # loads is stated in the invocation rather than left to the environment.
        runner = executor.ProbeRunner(
            ctx.probe_command,
            corpus_dir,
            ctx.scratch / "probe-scratch",
            log,
            label=ctx.label,
        )
        probe_records = runner.run(rows)
        for key, record in probe_records.items():
            records[f"{NS_PROBE}/{key}"] = (record.status, record.payload)
        diagnostics["crashes"] = runner.crashes
        # Namespaced the same way the records are, because the grader looks these up
        # by the key it failed to find a record under.  Bare assertion ids here
        # would match nothing and every abandoned case would be graded as a
        # submission that produced no result.
        diagnostics["abandoned"] = [f"{NS_PROBE}/{a}" for a in runner.abandoned]
    else:
        diagnostics["not_run"].append(f"probe ({len(rows)} assertions): not built")
        log.write(f"probe[{ctx.label}]: not built; {len(rows)} assertions not run")

    # -- commands ---------------------------------------------------------
    commands = executor.CommandRunner(
        log,
        label=ctx.label,
        prefix=ctx.prefix,
        consumer=ctx.consumer,
        consumer_cp=ctx.consumer_cp,
        scratch=ctx.scratch / "cli",
    )
    cli_cases = cases_of_kind(catalog, NS_CLI)
    log.write(f"cli[{ctx.label}]: {len(cli_cases)} cases")
    for case in cli_cases:
        result, signature = commands.run_case(case)
        if result is None:
            # No consumer to run.  The case is left out of the records entirely
            # rather than recorded as an empty result: on the reference side that
            # must abort the freeze, and on the submission side the grader turns
            # a missing record into a failure with a reason.  Inventing a
            # placeholder here would let both of those pass quietly.
            diagnostics["not_run"].append(f"cli/{case['id']}: consumer not built")
            continue
        status = "timeout" if result.timed_out else f"rc:{result.returncode}"
        records[f"{NS_CLI}/{case['id']}"] = (status, signature)

    # -- drivers ----------------------------------------------------------
    drivers = executor.DriverRunner(
        log,
        label=ctx.label,
        build_dir=ctx.build_dir,
        corpus_dir=corpus_dir,
        scratch=ctx.scratch / "driver",
        prefix=ctx.prefix,
    )
    driver_cases = cases_of_kind(catalog, NS_DRIVER)
    log.write(f"driver[{ctx.label}]: {len(driver_cases)} cases")
    for case in driver_cases:
        result, signature = drivers.run_case(case)
        if result is None:
            diagnostics["not_run"].append(
                f"driver/{case['id']}: {case['argv'][0]} was not built"
            )
            continue
        status = "timeout" if result.timed_out else f"rc:{result.returncode}"
        records[f"{NS_DRIVER}/{case['id']}"] = (status, signature)
    diagnostics["missing_drivers"] = sorted(drivers.missing)

    log.write(
        f"collect[{ctx.label}]: {len(records)} records, "
        f"{len(diagnostics['crashes'])} crash(es), "
        f"{len(diagnostics['not_run'])} case(s) not run"
    )
    return records, diagnostics


def freeze(
    catalog: dict,
    corpus_meta: dict,
    ctx: CollectContext,
    corpus_dir: Path,
    out_dir: Path,
    log: Log,
) -> str:
    """Build the expectation store from the reference install.

    Every failure here is fatal at image build time, and deliberately so.  A
    reference that crashes, stalls, or cannot run a case does not define a
    behavior -- and an expectation whose value is "signal 11" would be graded as
    the correct answer, so a submission that also crashed would pass it.  The
    verifier refuses to exist rather than ship that.
    """
    log.section("freeze expectations from reference")
    records, diagnostics = collect(catalog, ctx, corpus_dir, log)
    if diagnostics["crashes"]:
        raise RuntimeError(
            f"the reference crashed on {len(diagnostics['crashes'])} case(s): "
            f"{[c.get('assert_id') for c in diagnostics['crashes'][:8]]}"
        )
    if diagnostics["not_run"]:
        raise RuntimeError(
            f"{len(diagnostics['not_run'])} reference case(s) could not run: "
            f"{diagnostics['not_run'][:6]}"
        )

    # Every behavioural case must have every one of its keys, checked here rather
    # than discovered later: a case with no frozen expectation would fail every
    # submission forever, and the report would blame the submission for it.
    missing: list[str] = []
    for case in behavioural_cases(catalog):
        for key in keys_for(case):
            if key not in records:
                missing.append(key)
    if missing:
        raise RuntimeError(
            f"{len(missing)} case key(s) produced no reference record: "
            f"{missing[:8]}"
        )
    # And nothing extra: a record nothing looks up is coverage that was paid for
    # and then dropped, which is worth failing the build over while it is still
    # cheap to fix.
    wanted = {key for case in behavioural_cases(catalog) for key in keys_for(case)}
    orphans = sorted(set(records) - wanted)
    if orphans:
        raise RuntimeError(
            f"{len(orphans)} reference record(s) match no case: {orphans[:8]}"
        )

    store = ExpectationStore(
        out_dir / "expectations.json", out_dir / "expectations.bin"
    )
    meta = {
        "catalog_digest": catalog["digest"],
        "catalog_cases": catalog["counts"]["total"],
        "catalog_behavioural": catalog["counts"]["behavioural"],
        "corpus_digest": corpus_meta["digest"],
        "corpus_payloads": corpus_meta["count"],
        "reference_version": corpus_meta.get("reference_version"),
        "by_namespace": counts_by_namespace(records),
    }
    digest = store.write(records, meta)
    log.write(
        f"expectations: {len(records)} records "
        f"({meta['by_namespace']}), digest {digest}"
    )
    return digest


def counts_by_namespace(records: dict[str, tuple[str, bytes]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for key in records:
        head = key.split("/", 1)[0]
        out[head] = out.get(head, 0) + 1
    return dict(sorted(out.items()))


def counts_by_prefix(store: ExpectationStore) -> dict[str, int]:
    """Record counts one level below the namespace, for the freeze summary."""
    out: dict[str, int] = {}
    for key in store.entries:
        head = key.split("/", 2)
        bucket = "/".join(head[:2]) if len(head) > 1 else head[0]
        out[bucket] = out.get(bucket, 0) + 1
    return out
