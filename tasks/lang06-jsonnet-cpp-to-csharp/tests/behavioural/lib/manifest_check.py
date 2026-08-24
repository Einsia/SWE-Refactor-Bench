#!/usr/bin/env python3
"""Build-time self-checks for the verifier image.

Two things get checked here that nothing else can check:

  asminspect  The metadata reader runs, emits the document shape asmmeta.py
              parses, and finds a real assembly in a real publish directory.  A
              reader that silently reported an empty assembly list would make all
              three assembly checks pass vacuously -- every submission would clear
              them by default.  This says nothing about whether those checks are
              *right*; it is the narrower claim that the tool they depend on runs
              in this image and finds an assembly that is really there.

  frozen      The expectations blob the image was built to produce is complete and
              large enough to be worth grading against.  The floor is asserted
              against the artifact that actually shipped, not against the code
              that generated it.

Kept as a file rather than inlined into the Dockerfile with `python3 -c`: a
compound statement cannot follow a semicolon on a `-c` line, so any inline version
of this either cannot loop or has to be written in a contorted way that nobody
will read.  It also means these checks can be run against a built image by hand.
"""
import argparse
import json
import sys

# Every submission is graded on at least this many cases.  A benchmark task that
# quietly shrank its own case list would still look green, so the floor is asserted
# on the shipped blob.
MIN_CASES = 2000

# The document shape asmmeta.py reads.  Named here so a rename in Program.cs
# fails the image build rather than disarming a gate at grading time.
REQUIRED_REPORT_KEYS = ("assemblies", "nativeFiles", "frameworkFiles",
                        "unreadable")
REQUIRED_ASSEMBLY_KEYS = ("name", "path", "isExecutable", "ilOnly",
                          "hasNativeResources", "nativeResourceNames",
                          "pinvokes", "moduleRefs", "attributes", "memberRefs")


def check_asminspect(path: str, expect_assembly: str) -> list[str]:
    errs: list[str] = []
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)

    if doc.get("tool") != "AsmInspect/1.0":
        errs.append(f"unexpected tool banner: {doc.get('tool')!r}")

    for key in REQUIRED_REPORT_KEYS:
        if not isinstance(doc.get(key), list):
            errs.append(f"report key {key!r} is not a list: "
                        f"{type(doc.get(key)).__name__}")

    asms = doc.get("assemblies") or []
    if not asms:
        errs.append("the reader found no managed assemblies at all; every "
                    "assembly gate would pass vacuously")
    else:
        for key in REQUIRED_ASSEMBLY_KEYS:
            if key not in asms[0]:
                errs.append(f"assembly records have no {key!r} field")
        names = [a.get("name") for a in asms]
        if expect_assembly not in names:
            errs.append(f"expected to find {expect_assembly!r} among "
                        f"{names!r}")

    if doc.get("unreadable"):
        errs.append(f"the reader could not read: {doc['unreadable']!r}")

    if not errs:
        print(f"asminspect probe: assemblies={len(asms)} "
              f"native={len(doc['nativeFiles'])} "
              f"framework={len(doc['frameworkFiles'])}, "
              f"found {expect_assembly!r}, nothing unreadable")
    return errs


def check_frozen(path: str) -> list[str]:
    errs: list[str] = []
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)

    n = doc.get("case_count")
    # freeze.py writes `cases` as an object keyed by case id, so that grading can
    # look a case up by name rather than by position.
    cases = doc.get("cases")
    if not isinstance(n, int):
        errs.append(f"case_count is {n!r}, not an int")
    elif n < MIN_CASES:
        errs.append(f"only {n} cases frozen; the floor is {MIN_CASES}")
    if not isinstance(cases, dict):
        errs.append(f"cases is {type(cases).__name__}, expected an object keyed "
                    f"by case id")
    elif isinstance(n, int) and len(cases) != n:
        errs.append(f"case_count says {n} but the blob holds {len(cases)}")

    for key in ("freeze_format", "schema_version", "upstream_version",
                "case_digest", "family_counts", "family_weights",
                "upstream_files", "upstream_hashes"):
        if key not in doc:
            errs.append(f"the expectations blob has no {key!r}")

    fams = doc.get("family_counts") or {}
    weights = doc.get("family_weights") or {}
    if fams and weights and set(fams) - set(weights):
        errs.append(f"families with no weight: {sorted(set(fams) - set(weights))}")

    # A family with no cases is a coverage claim the case list does not back.
    empty = sorted(k for k, v in fams.items() if not v)
    if empty:
        errs.append(f"families with no cases: {empty}")

    if not errs:
        print(f"frozen: {n} cases in {len(fams)} families, "
              f"digest {doc['case_digest'][:16]}, "
              f"upstream {doc['upstream_version']}, "
              f"format {doc['freeze_format']}")
    return errs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--asminspect-report",
                    help="JSON emitted by AsmInspect over a publish directory")
    ap.add_argument("--expect-assembly", default="AsmInspect",
                    help="an assembly name that must appear in that report")
    ap.add_argument("--frozen", help="expectations.json to check")
    args = ap.parse_args()

    if not args.asminspect_report and not args.frozen:
        ap.error("nothing to check: pass --asminspect-report or --frozen")

    errs: list[str] = []
    if args.asminspect_report:
        errs += check_asminspect(args.asminspect_report, args.expect_assembly)
    if args.frozen:
        errs += check_frozen(args.frozen)

    if errs:
        print(f"\nimage check FAILED: {len(errs)} problems", file=sys.stderr)
        for e in errs:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print("image check PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
