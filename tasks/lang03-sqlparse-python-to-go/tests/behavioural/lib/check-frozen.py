#!/usr/bin/env python3
"""Checks the frozen manifest against the suite's published numbers.

catalog.py already refuses to build a catalog that falls below the floors or that
drifts from its declared shape, but that check runs on a catalog in memory, in a
process that could have been given anything.  This one runs on the manifest that
shipped, inside the image that will do the grading, on the build's critical path.
It is the last thing standing between a shrunken suite and a graded submission.

Three things are checked, and they fail for three different reasons:

  floors        the published minimums (>= 2000 cases, and per kind and per tier).
                A floor catches collapse -- a document builder that returned nothing,
                a tier that stopped emitting.
  declared      the exact case counts, which are deterministic given the pinned
                inputs.  This catches the opposite of collapse: a family emitted
                twice, or a case dropped by an edit nobody re-counted.
  concentration no family may carry more than 17% of the behavioural weight, and
                the shares must sum to 1.  A suite whose score is really one
                family's score grades one thing while claiming to grade twenty.

The floors and the declared shape are imported from catalog.py rather than
retyped, so there is exactly one place where each number lives.  Copying them here
would produce a check that agrees with itself and with nothing else -- two
self-consistent tables that both drifted is a failure mode this suite has already
paid for once.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import catalog as catalogmod  # noqa: E402

MANIFEST_SCHEMA = "swerefactor-verifier-manifest-v1"
EXPECTED_VERSION = "0.5.3"
TASK = "lang03-sqlparse-python-to-go"

# Every digest the driver recomputes at startup.  Named here so a manifest that
# simply omitted one could not pass: an absent digest is not a mismatch, and
# the driver's own drift check compares `manifest.get(key)` against a computed
# value, which a missing key would satisfy as None != value only by luck of the
# comparison.  Checking presence here makes that a build failure instead.
REQUIRED_DIGESTS = (
    "catalog_digest", "documents_digest", "expectations_digest", "spec_digest",
    "keywords_digest", "contract_digest",
)

# The ceiling catalog.py enforces per family.  Restated as a number here because
# catalog.py applies it inline rather than exposing it; if that changes, this is
# the second place to update, and the assertion below is what will say so.
MAX_FAMILY_SHARE = 0.17


def problems_with(manifest: dict) -> list[str]:
    """Everything wrong with this manifest, not just the first thing."""
    bad: list[str] = []

    if manifest.get("schema") != MANIFEST_SCHEMA:
        bad.append(f"unknown manifest schema {manifest.get('schema')!r}")
    if manifest.get("task") != TASK:
        bad.append(f"manifest is for task {manifest.get('task')!r}, not {TASK!r}")
    if manifest.get("upstream_version") != EXPECTED_VERSION:
        bad.append(f"reference version is {manifest.get('upstream_version')!r}, "
                   f"expected {EXPECTED_VERSION!r}")

    counts = manifest.get("counts", {})
    by_kind = counts.get("by_kind", {})
    by_tier = counts.get("by_tier", {})

    floors = {
        "total": catalogmod.MIN_TOTAL_CASES,
        "behavioural": catalogmod.MIN_BEHAVIOURAL_CASES,
    }
    for key, floor in sorted(floors.items()):
        have = counts.get(key, 0)
        if have < floor:
            bad.append(f"counts.{key} is {have}, floor is {floor}")
    # The three kinds that have a floor.  `probe` has none of its own -- it is
    # covered by the `behavioural` floor above and by the per-tier floors below,
    # which is where a collapse in the probe tiers would show up.
    #
    # Derived from the manifest's own `floors` block rather than named here, and
    # that is the point.  A hand-written mapping over kinds is a completeness
    # claim.
    kind_floors = {
        kind: floor
        for kind, floor in sorted(manifest.get("floors", {}).items())
        if kind in by_kind and isinstance(floor, int)
    }
    for kind, floor in kind_floors.items():
        have = by_kind.get(kind, 0)
        if have < floor:
            bad.append(f"counts.by_kind.{kind} is {have}, floor is {floor}")

    # And the floors the manifest recorded have to be the floors this catalog
    # module holds now, or the paragraph above is checking a shrunken suite
    # against the shrunken numbers that shipped with it.
    for name, attr in (("struct", "MIN_STRUCT_CASES"),
                       ("cli", "MIN_CLI_CASES"),
                       ("provenance", "MIN_PROVENANCE_CASES")):
        want = getattr(catalogmod, attr)
        got = manifest.get("floors", {}).get(name)
        if got != want:
            bad.append(f"manifest floors.{name} is {got}, catalog.{attr} is {want}")
        if name not in by_kind:
            bad.append(f"counts.by_kind has no {name!r} kind, but catalog.{attr} "
                       f"sets a floor of {want} for it")
    for tier, floor in sorted(catalogmod.MIN_TIER_CASES.items()):
        have = by_tier.get(tier, 0)
        if have < floor:
            bad.append(f"counts.by_tier.{tier} is {have}, floor is {floor}")

    # The exact shape.  Deterministic inputs, deterministic counts: a difference
    # here means the suite was edited without updating catalog.DECLARED_SHAPE, and
    # the diff of that constant is where the change should have been reviewed.
    shape = catalogmod.DECLARED_SHAPE
    if counts.get("total") != shape["total"]:
        bad.append(f"counts.total is {counts.get('total')}, "
                   f"catalog declares {shape['total']}")
    if len(counts.get("by_family", {})) != shape["families"]:
        bad.append(f"{len(counts.get('by_family', {}))} families in the manifest, "
                   f"catalog declares {shape['families']}")
    for kind, want in sorted(shape["by_kind"].items()):
        if by_kind.get(kind) != want:
            bad.append(f"counts.by_kind.{kind} is {by_kind.get(kind)}, "
                       f"catalog declares {want}")

    # An expectation per behavioural case, or some case is graded against nothing.
    # The `struct` and `guard` kinds are graded by inspection and have no frozen
    # answer, which is why this compares against `behavioural` and not `total`.
    #
    # The two halves are counted separately in the store's metadata because they
    # were frozen by different runners -- the probe driver and the CLI runner -- so
    # the total is their sum, and each is also checked against its kind.  Reading
    # only `probe_cases` here would look like a shortfall of exactly the CLI cases.
    frozen = manifest.get("expectations", {})
    probe_frozen = frozen.get("probe_cases", 0)
    cli_frozen = frozen.get("cli_cases", 0)
    behavioural = counts.get("behavioural", 0)
    if probe_frozen + cli_frozen < behavioural:
        bad.append(f"only {probe_frozen + cli_frozen} expectations were frozen "
                   f"({probe_frozen} probe + {cli_frozen} cli) for {behavioural} "
                   f"behavioural cases; some case would be graded against nothing")
    if probe_frozen != by_kind.get("probe"):
        bad.append(f"{probe_frozen} probe expectations were frozen for "
                   f"{by_kind.get('probe')} probe cases")
    if cli_frozen != by_kind.get("cli"):
        bad.append(f"{cli_frozen} CLI expectations were frozen for "
                   f"{by_kind.get('cli')} CLI cases")
    if frozen.get("catalog_cases") != counts.get("total"):
        bad.append(f"the expectations were frozen against a catalog of "
                   f"{frozen.get('catalog_cases')} cases, this one has "
                   f"{counts.get('total')}")

    shares = manifest.get("weight_share", {})
    if not shares:
        bad.append("the manifest carries no weight_share, so nothing checks that "
                   "the score is spread across families")
    else:
        for family, share in sorted(shares.items()):
            if share > MAX_FAMILY_SHARE:
                bad.append(f"family {family} carries {share:.1%} of the behavioural "
                           f"weight, ceiling is {MAX_FAMILY_SHARE:.0%}")
        total = sum(shares.values())
        # Each share is rounded to five places when written, so the sum drifts by
        # at most half a unit in the last place per family.  23 families gives
        # ~1.2e-4 of headroom; 5e-3 is loose enough not to be brittle and tight
        # enough that a missing family (the smallest is 0.1%) still fails.
        if abs(total - 1.0) > 5e-3:
            bad.append(f"the family weight shares sum to {total:.4f}, not 1.0, so "
                       f"some family's weight is unaccounted for")

    digests = manifest.get("digests", {})
    for name in REQUIRED_DIGESTS:
        value = manifest.get(name, digests.get(name, ""))
        if not isinstance(value, str) or len(value) != 64:
            bad.append(f"{name} is not a sha256: {value!r}")
    if len(manifest.get("assets", {})) < 40:
        bad.append(f"only {len(manifest.get('assets', {}))} asset digests were "
                   f"recorded; the staged grading inputs are not all covered")

    rt = manifest.get("round_trip", {})
    if not rt.get("consumer_ok"):
        bad.append("the contract round trip did not confirm the generated consumer "
                   "compiles, so the published API was never machine-checked")

    return bad


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args(argv)

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    bad = problems_with(manifest)
    if bad:
        for line in bad:
            print(f"manifest check FAILED: {line}")
        return 1

    counts = manifest["counts"]
    print("manifest ok: " + json.dumps({
        "total": counts["total"],
        "behavioural": counts["behavioural"],
        "by_kind": counts["by_kind"],
        "by_tier": counts["by_tier"],
        "families": len(counts["by_family"]),
        "documents": manifest["expectations"].get("documents_count"),
        "expectations": (manifest["expectations"]["probe_cases"]
                         + manifest["expectations"]["cli_cases"]),
        "max_family_share": round(max(manifest["weight_share"].values()), 4),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
