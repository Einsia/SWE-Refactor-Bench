#!/usr/bin/env python3
"""The matched-pair check: every catalog op is answerable by both halves.

The probe is a differential pair.  The Python half runs against pinned upstream
sqlparse and produces the expected answer; the Go half runs against the
submission.  A case only means something if both halves can answer it:

  * an op the Python half lacks has no expected answer, so every submission
    fails it no matter what it does;
  * an op the Go half lacks answers `defect`, which halts the run as "verifier
    corrupted" -- so one missing registration turns a whole trial into a
    non-result rather than a score.

Both failures are silent at authoring time and expensive at grading time, and
both are reachable by ordinary editing: a half that answers with fields the
other cannot produce, and a registration in a tier the contract does not list.
This runs at freeze time, where a mismatch is a build failure.

Checked here, mechanically, in three directions:

  1. every op referenced by a catalog case is registered in probe.py;
  2. every such op is registered in exactly the tier binary the case names;
  3. the contract's per-tier op lists agree with both of the above.

The Go side is read from source rather than by running the binaries, because the
binaries need a submission to link against and this check must hold before any
submission exists.  Registration is a literal `r.Register("name", fn)` in each
tier's main.go, which is generated, so the text is reliable; a tier that ever
registers dynamically will fail this check and should.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def python_ops(path: Path) -> set[str]:
    """Op names the Python half registers, read from its @op decorators."""
    return set(re.findall(r'^@op\("([^"]+)"\)', path.read_text(), re.M))


def go_ops(probe_dir: Path) -> dict[str, set[str]]:
    """Op names each tier binary registers, per tier."""
    out: dict[str, set[str]] = {}
    for main in sorted(probe_dir.glob("*/main.go")):
        tier = main.parent.name
        out[tier] = set(re.findall(r'r\.Register\("([^"]+)"', main.read_text()))
    return out


def check(catalog: dict, contract: dict, probe_dir: Path) -> list[str]:
    problems: list[str] = []
    py = python_ops(probe_dir / "probe.py")
    go = go_ops(probe_dir)
    tiers = contract["go_contract"]["probe_tiers"]

    cases = [c for c in catalog["cases"] if c.get("kind") == "probe"]
    used: dict[str, set[str]] = {}
    for c in cases:
        used.setdefault(c["op"], set()).add(c["tier"])

    for op in sorted(used):
        if op not in py:
            problems.append(f"{op}: catalog uses it, probe.py does not register "
                            f"it -- no expected answer exists, so every "
                            f"submission fails these cases")
        for tier in sorted(used[op]):
            if tier not in go:
                problems.append(f"{op}: catalog routes it to tier {tier!r}, "
                                f"which has no main.go")
            elif op not in go[tier]:
                problems.append(f"{op}: catalog routes it to tier {tier}, whose "
                                f"binary does not register it -- these cases "
                                f"would answer `defect` and halt the run")

    # The contract's tier lists are what a port reads to know what it is being
    # asked.  They must match what is actually registered, in both directions.
    for tier, spec in sorted(tiers.items()):
        declared = set(spec["ops"])
        registered = go.get(tier, set())
        for op in sorted(declared - registered):
            problems.append(f"{tier}: contract declares op {op}, the binary "
                            f"does not register it")
        for op in sorted(registered - declared):
            problems.append(f"{tier}: binary registers op {op}, the contract "
                            f"does not declare it")
        for op in sorted(declared - py):
            problems.append(f"{tier}: contract declares op {op}, probe.py does "
                            f"not register it")

    # An op registered on both sides but never used by any case is dead weight
    # that will rot; an op used by cases the catalog never routes cannot exist.
    for op in sorted(py - set(used)):
        problems.append(f"{op}: probe.py registers it, no catalog case uses it")
    return problems


def main() -> int:
    catalog_path = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if catalog_path is None:
        sys.path.insert(0, str(HERE))
        import catalog as catmod
        work = HERE.parent.parent.parent / "_work/lang03"
        catalog = catmod.build_catalog(
            json.loads((work / "documentbuild/documents.json").read_text()),
            json.loads((work / "keywords.json").read_text()))
    else:
        catalog = json.loads(catalog_path.read_text())
    contract = json.loads((HERE / "source-contract.json").read_text())

    problems = check(catalog, contract, HERE / "probe")
    n_ops = len({c["op"] for c in catalog["cases"] if c.get("kind") == "probe"})
    if problems:
        print(f"pair check FAILED: {len(problems)} mismatch(es)")
        for p in problems:
            print("  " + p)
        return 1
    print(f"pair check ok: {n_ops} ops answerable by both halves, "
          f"{len(contract['go_contract']['probe_tiers'])} tiers agree with the "
          f"contract")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
