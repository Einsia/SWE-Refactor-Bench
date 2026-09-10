#!/usr/bin/env python3
"""Checks the frozen manifest against the suite's published floors.

catalog.py already refuses to build a catalog below these floors, but that check
runs on the catalog in memory.  This one runs on the manifest that shipped, in
the image that will do the grading, and is the last thing between a shrunken
suite and a graded submission.  It is cheap and it is on the build's critical
path on purpose.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

# The floors published in instruction.md and in the task's documentation.
FLOORS = {
    "cases": 800,
    "behavioural": 600,
}
KIND_FLOORS = {
    "guard": 20,
    "struct": 40,
    "probe": 400,
    "cli": 100,
}
EXPECTED_VERSION = "0.31.1"
REQUIRED_DIGESTS = ("catalog", "corpus", "oracle")


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
    if counts.get("expectations", 0) < counts.get("behavioural", 0):
        problems.append(
            f"only {counts.get('expectations')} expectations were frozen for "
            f"{counts.get('behavioural')} behavioural cases; some case would be "
            f"graded against nothing"
        )

    digests = manifest.get("digests", {})
    for name in REQUIRED_DIGESTS:
        value = digests.get(name, "")
        if len(value) != 64:
            problems.append(f"digests.{name} is not a sha256: {value!r}")

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
                "by_kind": by_kind,
                "documents": counts.get("documents"),
                "expectations": counts.get("expectations"),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
