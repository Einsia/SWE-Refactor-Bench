#!/usr/bin/env python3
"""Checks the frozen manifest against the suite's published floors and the
release facts the structural cases grade against.

catalog.py already refuses to build a catalog below these floors, but that check
runs on the catalog in memory.  This one runs on the manifest that shipped, in
the image that will do the grading, and is the last thing between a shrunken
suite and a graded submission.  It is cheap and it is on the build's critical
path on purpose.

The contract block is the part specific to this task, and it is where this file
differs most from the C-to-C form.  There, the release contract was an ELF fact
-- 88 exported symbols, 47 of them bound to one of 14 GNU version nodes, behind
the soname libz.so.1 -- read off the reference the image was built from.  A jar
has no equivalent: State B's release surface is 11 public types and 115 members
in module org.zlib, published as one artifact at a fixed path, and none of that
can be read off a shared object.  So the numbers asserted here come from the
contract itself, cross-checked by freeze.py against the reference for the two
facts the two can share (the version string and the compile-flag word).

They are asserted exactly rather than as floors: a member count that drifted is a
different API, not a bigger one.  If it ever drifts without this file being
updated, every structural case would be graded against a surface nobody
published, and the failure would look like a submission's fault.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

# The floors published in instruction.md and in the task's documentation.  They
# sit below what the suite currently produces on purpose: this is the promise,
# not the measurement.
FLOORS = {
    "cases": 800,
    "behavioural": 650,
    "families": 40,
    "assertions": 800,
    "payloads": 60,
}
KIND_FLOORS = {
    "probe": 600,
    "cli": 20,
    "driver": 40,
    "struct": 50,
    "guard": 30,
}
EXPECTED_VERSION = "1.3.1"
REQUIRED_DIGESTS = ("catalog", "corpus", "oracle")

# The release surface, exact.  Read out of the contract by freeze.py after
# Expectations.load has asserted the contract against itself, so a mismatch here
# means the contract changed rather than that it was self-inconsistent.
#
# state_a_symbols is 88 and stays 88 after the migration: it is the count of C
# entry points State A published, and symbol_map's job is to say where each one
# went.  It is a property of the *source* release, so unlike every other number
# in this block it must not change when State B's API does.
EXPECTED_CONTRACT = {
    "types": 11,
    "members": 115,
    "module": "org.zlib",
    "jar": "share/java/zlib-1.3.1.jar",
    "class_major_version": 61,
    "state_a_symbols": 88,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args(argv)

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    problems: list[str] = []

    if manifest.get("schema") != "swerefactor-verifier-manifest-v1":
        problems.append(f"unknown manifest schema {manifest.get('schema')!r}")
    if manifest.get("upstream_version") != EXPECTED_VERSION:
        problems.append(
            f"reference version is {manifest.get('upstream_version')!r}, "
            f"expected {EXPECTED_VERSION!r}"
        )

    counts = manifest.get("counts", {})
    for key, floor in sorted(FLOORS.items()):
        have = counts.get(key, 0)
        if have < floor:
            problems.append(f"counts.{key} is {have}, floor is {floor}")
    by_kind = counts.get("by_kind", {})
    for kind, floor in sorted(KIND_FLOORS.items()):
        have = by_kind.get(kind, 0)
        if have < floor:
            problems.append(f"counts.by_kind.{kind} is {have}, floor is {floor}")

    # Every behavioural case is graded against at least one frozen expectation, and
    # most against several.  Fewer expectations than cases means some case would
    # be compared against nothing -- which the grader reports as a failure, so the
    # submission would pay for it.
    if counts.get("expectations", 0) < counts.get("behavioural", 0):
        problems.append(
            f"only {counts.get('expectations')} expectations were frozen for "
            f"{counts.get('behavioural')} behavioural cases; some case would be "
            f"graded against nothing"
        )
    namespaces = counts.get("expectations_by_namespace", {})
    for namespace in ("probe", "cli", "driver"):
        if not namespaces.get(namespace):
            problems.append(
                f"no expectations were frozen in the {namespace!r} namespace; "
                f"that whole executor would be graded against nothing"
            )

    digests = manifest.get("digests", {})
    for name in REQUIRED_DIGESTS:
        value = digests.get(name, "")
        if len(value) != 64:
            problems.append(f"digests.{name} is not a sha256: {value!r}")

    contract = manifest.get("contract", {})
    for key, want in sorted(EXPECTED_CONTRACT.items()):
        got = contract.get(key)
        if got != want:
            problems.append(
                f"contract.{key} is {got!r}, expected {want!r}: the contract this "
                f"image was built from does not describe the release the "
                f"structural cases grade against"
            )

    # The reference is a freeze-time instrument and nothing under this key
    # survives into the grading image, so its paths are not checked for
    # existence.  What is checked is that it was the shared build: the driver
    # expectations came out of that build directory, and a static reference would
    # have produced them from different binaries.
    reference = manifest.get("reference", {})
    if reference.get("config") != "shared":
        problems.append(
            f"reference.config is {reference.get('config')!r}, expected 'shared': "
            f"the driver expectations were frozen from the shared build tree"
        )
    if reference.get("compile_flags") != "0xa9":
        problems.append(
            f"reference.compile_flags is {reference.get('compile_flags')!r}, "
            f"expected '0xa9': compile-flags-unchanged grades against this word"
        )

    if problems:
        for line in problems:
            print(f"manifest check FAILED: {line}")
        return 1
    print(
        "manifest ok: "
        + json.dumps(
            {
                "cases": counts["cases"],
                "behavioural": counts["behavioural"],
                "assertions": counts["assertions"],
                "executions": counts.get("executions"),
                "families": counts.get("families"),
                "by_kind": by_kind,
                "payloads": counts.get("payloads"),
                "expectations": counts.get("expectations"),
                "contract": {k: contract.get(k) for k in sorted(EXPECTED_CONTRACT)},
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
