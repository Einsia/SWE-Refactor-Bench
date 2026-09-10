#!/usr/bin/env python3
"""Checks the frozen manifest against the suite's published floors.

catalog.py already refuses to build a catalog below these floors, and freeze.py
refuses to write a manifest below them.  Both of those run on data in memory, in
the process that produced it.  This one runs on the manifest file that shipped, in
the image that will do the grading, and it is the last thing standing between a
suite that quietly shrank and a submission graded against it.

The floors are imported from catalog.py rather than retyped here.  A second copy
of a number is a second thing to keep in step, and the failure mode is silent:
lowering the floor in one file and not the other leaves a check that passes while
measuring less than it says.  What this file adds is not the numbers -- it is
asking them of the artifact.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import catalog

MANIFEST_SCHEMA = "swerefactor-verifier-manifest-v1"
EXPECTED_TASK = "lang04-acorn-js-to-rust"
EXPECTED_VERSION = "8.14.0"
EXPECTED_TOOLCHAIN = "1.90.0"

# Every digest driver.py's `Assets` looks up, from the inventory that also tells
# freeze.py what to digest.  Checked as a set in both directions: an extra entry is
# dead weight nothing verifies, and a missing one is an input that ships unchecked.
#
# Imported rather than retyped, for the reason the module docstring gives about the
# floors, and demonstrated by this tuple: it was a hand-written list of six, and
# when `excluded` became a seventh frozen input the check fired -- correctly, and
# saying the manifest was the thing at fault.  A stale copy of a set does not report
# that it is stale; it reports that the world is wrong.
REQUIRED_DIGESTS = tuple(sorted(catalog.FROZEN_INPUTS))

# Counted from the catalog rather than declared: `upstream_tests` is how many of
# acorn's own tests the reference had to pass before its answers were frozen, and
# `baseline_files` is State A's regular-file count.  Both are properties of the
# pinned release, so they are equalities and not floors.
UPSTREAM_TESTS = 6763
BASELINE_FILES = 116


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--assets", type=Path, default=None,
                        help="the asset root the manifest's paths are relative to; "
                             "defaults to the directory the manifest is in")
    args = parser.parse_args(argv)

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    counts = manifest.get("counts") or {}
    problems: list[str] = []

    def want(key: str, got, expected) -> None:
        if got != expected:
            problems.append(f"{key} is {got!r}, expected {expected!r}")

    want("schema", manifest.get("schema"), MANIFEST_SCHEMA)
    want("task", manifest.get("task"), EXPECTED_TASK)
    want("upstream_version", manifest.get("upstream_version"), EXPECTED_VERSION)
    want("rust_toolchain", manifest.get("rust_toolchain"), EXPECTED_TOOLCHAIN)
    want("counts.upstream_tests", counts.get("upstream_tests"), UPSTREAM_TESTS)
    want("counts.baseline_files", counts.get("baseline_files"), BASELINE_FILES)

    # The seed is the anti-memorisation term: the generated half of the corpus is a
    # function of it, and it exists in this image only.  Its presence is checked,
    # not its value -- a rebuild with a different seed is a legitimate thing to do
    # and must not be blocked by this file.
    if not isinstance((manifest.get("corpus") or {}).get("seed"), int):
        problems.append("corpus.seed is absent or not an integer")

    # Two populations vary between honest rebuilds and three do not, so they are
    # held to different standards.
    #
    # `corpus_cases` is a function of the seed and `fixture_cases` of which
    # libraries the download actually yielded; both get a floor, because pinning
    # them to a number would make a legitimate reseed or a mirror hiccup look
    # like tampering.
    #
    # The other three are `len()` of fixed tuples in catalog.py -- see
    # freeze.py's `cli_cases`/`struct_cases`/`gate_cases` keys.  Nothing about a
    # rebuild can move them, so a floor is the wrong instrument: it cannot tell
    # this tree's freeze from some other tree's.  It was checked with one.  An
    # earlier layout of this task had twenty structural cases; the restructure
    # left seventeen, and a manifest still claiming twenty printed "manifest ok"
    # because twenty clears a floor of fifteen.  Equality is what makes the
    # manifest evidence that the frozen artefacts came from the tree they are
    # about to grade.
    floors = {
        "corpus_cases": catalog.MIN_CORPUS_CASES,
        "fixture_cases": catalog.MIN_FIXTURE_CASES,
    }
    for key, floor in sorted(floors.items()):
        have = counts.get(key)
        if not isinstance(have, int):
            problems.append(f"counts.{key} is absent")
        elif have < floor:
            problems.append(f"counts.{key} is {have}, catalog floor is {floor}")

    for key, expect in sorted({
        "cli_cases": len(catalog.CLI_CASES),
        "struct_cases": len(catalog.STRUCT_CASES),
        "gate_cases": len(catalog.GUARD_CASES),
    }.items()):
        have = counts.get(key)
        if not isinstance(have, int):
            problems.append(f"counts.{key} is absent")
        elif have != expect:
            problems.append(
                f"counts.{key} is {have} but this tree's catalog holds {expect}; "
                f"the manifest was frozen from a different tree"
            )

    # The graded total must be the three populations it is made of, less the cases
    # nothing will ask, and nothing else.  A total that drifted from its parts is
    # the one arithmetic error that would make every per-case score wrong while
    # every count looked plausible.
    #
    # `unportable_cases` subtracts because a case whose frozen answer is a crash
    # inside the reference is skipped by every module that holds it, and a total
    # counting cases nobody is asked is wrong by exactly the amount it flatters the
    # suite's size.  Checked for presence, not for a value: freeze derives it by
    # measuring the expectations and asserts the shapes it found are the ones the
    # rule is written for, so a number here would be a fourth copy of a claim that
    # is already proved where it is computed.
    parts = [counts.get(k) for k in ("corpus_cases", "fixture_cases", "cli_cases")]
    unportable = counts.get("unportable_cases")
    if not isinstance(unportable, int) or unportable < 0:
        problems.append(
            f"counts.unportable_cases is {unportable!r}, not a count; the manifest "
            f"does not say how many cases have no portable answer, so the graded "
            f"total cannot be checked against its parts"
        )
    elif all(isinstance(p, int) for p in parts):
        want("counts.graded_total", counts.get("graded_total"),
             sum(parts) - unportable)

    # Every family the catalog budgets must have cases, and every family with
    # cases must be budgeted.  A family present in one and not the other is a
    # weight nobody can earn or a case graded at a weight nobody declared.
    families = counts.get("families")
    if not isinstance(families, dict):
        problems.append("counts.families is absent or not a table")
    else:
        budgeted = set(catalog.FAMILY_BUDGETS)
        present = {name for name, n in families.items() if n}
        for name in sorted(budgeted - present):
            problems.append(f"family {name!r} is budgeted but froze no cases")
        for name in sorted(present - budgeted):
            problems.append(f"family {name!r} froze cases but has no budget")

    digests = manifest.get("digests") or {}
    for name in REQUIRED_DIGESTS:
        value = digests.get(name, "")
        if not isinstance(value, str) or len(value) != 64:
            problems.append(f"digests.{name} is not a sha256: {value!r}")
    for name in sorted(set(digests) - set(REQUIRED_DIGESTS)):
        problems.append(f"digests.{name} ships unverified: nothing looks it up")

    # And that each one is on disk, beside the manifest that claims it.  A digest
    # for an absent file is the failure this check is least able to see from the
    # manifest alone: every string is 64 characters and every key is present, and
    # the first thing to notice would be the grading run.  driver.Assets does
    # compare the bytes, which is strictly more, but it does it at grading time --
    # so this is the same question asked while a build can still fail for it.
    root = args.assets or args.manifest.parent
    for name in REQUIRED_DIGESTS:
        path = root / catalog.FROZEN_INPUTS[name]
        if not path.is_file():
            problems.append(
                f"digests.{name} is recorded but {path} is not a file; the freeze "
                f"wrote a digest of something it did not leave behind"
            )

    if problems:
        for line in problems:
            print(f"manifest check FAILED: {line}")
        return 1
    print(
        "manifest ok: "
        + json.dumps(
            {
                "graded_total": counts["graded_total"],
                "corpus_cases": counts["corpus_cases"],
                "corpus_families": counts.get("corpus_families"),
                "unportable_cases": counts["unportable_cases"],
                "fixture_cases": counts["fixture_cases"],
                "cli_cases": counts["cli_cases"],
                "struct_cases": counts["struct_cases"],
                "gate_cases": counts["gate_cases"],
                "seed": manifest["corpus"]["seed"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
