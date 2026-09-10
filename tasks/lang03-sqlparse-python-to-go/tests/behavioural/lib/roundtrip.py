#!/usr/bin/env python3
"""The contract and the API dumper must agree, in both directions.

`structure.py` grades a submission's public surface by running apidump over it and
comparing the result to the contract's symbol lists.  That comparison is only
meaningful if the two sides spell things the same way -- a symbol the contract
renders one way and apidump renders another is reported as both missing and extra,
which fails a correct submission twice for one authoring slip.  It has happened
once here, with `func (*Options) ToMap()`.

So the contract is round-tripped: the skeleton generator emits a module from the
contract, apidump reads that module back, and the two symbol sets must be equal.
Fields are checked in one direction only, and the docstring below says why.

Run at freeze time, over a skeleton built in a scratch directory, so a contract
that has stopped being expressible fails the image build.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def check(contract: dict, dump: dict) -> list[str]:
    """Every disagreement between the contract's surface and the dumped one."""
    packages = contract["go_contract"]["packages"]
    problems: list[str] = []

    # cmd/sqlformat is a main package with no exported surface to compare; the
    # contract lists it for the binary it builds, not for symbols.
    want_pkgs = {k for k, v in packages.items() if v["symbols"]}
    got_pkgs = {k for k, v in dump.items() if v.get("symbols")}
    for missing in sorted(want_pkgs - got_pkgs):
        problems.append(f"{missing}: package absent from the dump")
    for extra in sorted(got_pkgs - want_pkgs):
        problems.append(f"{extra}: package in the dump but not the contract")

    for pkg in sorted(want_pkgs & got_pkgs):
        want = set(packages[pkg]["symbols"])
        got = set(dump[pkg]["symbols"])
        for sym in sorted(want - got):
            problems.append(f"{pkg}: contract has, dump does not: {sym}")
        for sym in sorted(got - want):
            problems.append(f"{pkg}: dump has, contract does not: {sym}")

        # Fields are checked per declared struct, and in one direction only.  The
        # contract's field list is a floor, not a closed world: a port may add
        # unexported fields, or exported ones the grader never names, without
        # having grown the API the way an extra function would.  What must not
        # happen is a declared field being absent, because the probe constructs the
        # struct by name and a missing field is a compile error in the grader.
        dumped = set(dump[pkg].get("fields") or [])
        for _struct, lines in sorted(packages[pkg].get("fields", {}).items()):
            for line in lines:
                if line not in dumped:
                    problems.append(f"{pkg}: contract declares field, dump does "
                                    f"not have it: {line}")
    return problems


def summary(contract: dict) -> tuple[int, int, int]:
    """(packages, symbols, declared fields) the check covers."""
    packages = contract["go_contract"]["packages"]
    want = {k: v for k, v in packages.items() if v["symbols"]}
    symbols = sum(len(v["symbols"]) for v in want.values())
    fields = sum(len(lines) for v in want.values()
                 for lines in v.get("fields", {}).values())
    return len(want), symbols, fields


def main() -> int:
    here = Path(__file__).resolve().parent
    contract = json.loads((here / "source-contract.json").read_text())
    dump_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/apidump.json")
    dump = json.loads(dump_path.read_text())
    problems = check(contract, dump)
    packages, symbols, fields = summary(contract)
    if problems:
        print(f"round-trip FAILED: {len(problems)} disagreement(s) over "
              f"{symbols} symbols")
        for problem in problems:
            print("  " + problem)
        return 1
    print(f"round-trip ok: {packages} packages, {symbols} symbols identical in "
          f"both directions, {fields} declared fields present")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
