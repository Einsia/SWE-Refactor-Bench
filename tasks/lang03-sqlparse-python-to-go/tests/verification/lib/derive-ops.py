#!/usr/bin/env python3
"""Derive the operation table a stage-3 candidate is handed.

Run once, in the stage-3 image build, against the same two files stage 2 grades
with: the frozen catalog and the reference probe's source.  Emits ops.json, which
srbsqlparse reads to route a request to the right tier binary and to encode a
candidate's arguments with the tags the operation actually accepts.

Derived rather than written out, and that is the whole point of the file
existing.  Three facts about an operation have to be right or a candidate spends
its round on defects instead of on the port:

  its tier      -- the submission ships one binary per tier and each registers
                   only its own operations, so a misrouted request answers
                   `defect: unknown op` on the submission and is answered
                   normally by the reference, which is a break that establishes
                   nothing.
  its arg tags  -- `rt` takes `e:` and `rt-raw` takes `d:`; both name a document
                   and only the operation knows which.  A hand-written table
                   would be a completeness claim over 28 operations and 6 tags.
  what it does  -- one line, so an adversary can pick an operation without
                   reading a probe it has no access to.

The first two come out of the catalog, which is what stage 2 actually sends, so
the table cannot describe a shape the operations were never asked for.  The third
comes out of probe.py's docstrings, read with `ast` rather than by importing:
probe.py imports sqlparse at module scope through _api_methods(), and no stage-3
build stage has an importable sqlparse.  Five operations carry no docstring, and
their summaries are written out in UNDOCUMENTED below under an assertion that the
two sets are exactly each other, so neither a docstring appearing nor one going
missing can pass silently.
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path


def registered_ops(probe_src: Path) -> dict[str, str]:
    """Op name -> first line of its docstring, read out of probe.py's AST.

    The registration is `@op("name")` on a module-level function, which is a
    shape the parser can see without running anything.
    """
    tree = ast.parse(probe_src.read_text(encoding="utf-8"), str(probe_src))
    out: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        for dec in node.decorator_list:
            if (isinstance(dec, ast.Call)
                    and isinstance(dec.func, ast.Name)
                    and dec.func.id == "op"
                    and dec.args
                    and isinstance(dec.args[0], ast.Constant)
                    and isinstance(dec.args[0].value, str)):
                name = dec.args[0].value
                doc = ast.get_docstring(node) or ""
                summary = doc.strip().split("\n")[0].strip()
                if name in out:
                    raise SystemExit(f"probe.py registers {name!r} twice")
                out[name] = summary
    return out


def go_registrations(probe_dir: Path) -> dict[str, str]:
    """Op name -> tier, read out of each tier's `r.Register("name", ...)` calls.

    A second, independent source for the tier split.  The catalog is what the
    executor routes by, so the catalog is authoritative here -- but the Go side is
    what answers, and if the two ever disagreed a candidate would get `unknown op`
    from a binary the table swore had that operation.  Cross-checked below rather
    than merged.
    """
    out: dict[str, str] = {}
    for main_go in sorted(probe_dir.glob("*/main.go")):
        tier = main_go.parent.name
        for line in main_go.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line.startswith("r.Register("):
                continue
            rest = line[len("r.Register("):]
            if not rest.startswith('"'):
                continue
            name = rest[1:].split('"', 1)[0]
            if name in out:
                raise SystemExit(
                    f"{name!r} is registered by both {out[name]} and {tier}")
            out[name] = tier
    return out


def arg_shapes(catalog: dict) -> tuple[dict[str, list[str]], dict[str, str]]:
    """Per-op argument tags and tier, from the cases stage 2 actually sends.

    An operation whose cases do not all agree on the tag shape is an error rather
    than a merge: srbsqlparse encodes a candidate's arguments from this list, and
    two shapes for one name would mean encoding by guess.
    """
    tags: dict[str, list[str]] = {}
    tiers: dict[str, str] = {}
    for case in catalog["cases"]:
        if case.get("kind") != "probe":
            continue
        name = case["op"]
        shape = [a.split(":", 1)[0] for a in case.get("args", [])]
        if name in tags and tags[name] != shape:
            raise SystemExit(
                f"op {name!r} is sent with two argument shapes: "
                f"{tags[name]} and {shape}")
        tags[name] = shape
        tier = case["tier"]
        if tiers.setdefault(name, tier) != tier:
            raise SystemExit(f"op {name!r} appears in two tiers")
    return tags, tiers


#: Summaries for the operations probe.py leaves undocumented.
#:
#: Five of the 28 carry no docstring, and reading them it is clear why -- each is
#: three lines that call one function and render its result, and a docstring would
#: have restated the call.  A candidate reading only this table has no such
#: context, so the line is written here instead of there.
#:
#: Written out by hand, and therefore checked in both directions below: an entry
#: for an op that has since acquired a docstring, and an op with neither a
#: docstring nor an entry, are both errors.  A hand-written list of exceptions to a
#: derived table is still a completeness claim, and the only thing that makes it
#: safe is that the claim is what gets asserted.
UNDOCUMENTED = {
    "optkeys-sorted":
        "The option names validate_options reads, sorted.",
    "remove-quotes":
        "utils.remove_quotes on a literal string.",
    "split":
        "sqlparse.split: the statement boundaries, with each statement's text.",
    "split-unquoted-newlines":
        "utils.split_unquoted_newlines on a literal string.",
    "version":
        "sqlparse.__version__, as the library reports it.",
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--catalog", required=True, type=Path)
    ap.add_argument("--probe-py", required=True, type=Path)
    ap.add_argument("--probe-dir", required=True, type=Path,
                    help="the probe module root, holding <tier>/main.go")
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()

    catalog = json.loads(args.catalog.read_text(encoding="utf-8"))
    summaries = registered_ops(args.probe_py)
    go_tiers = go_registrations(args.probe_dir)
    tags, tiers = arg_shapes(catalog)

    problems: list[str] = []

    # Every op the reference registers has to appear in the catalog, and vice
    # versa.  An op with no cases is one stage 2 never grades, so nothing has
    # established that both halves agree about it -- handing it to an adversary
    # would be handing over an ungraded surface.
    for name in sorted(set(summaries) - set(tags)):
        problems.append(f"probe.py registers {name!r} but no catalog case sends it")
    for name in sorted(set(tags) - set(summaries)):
        problems.append(f"the catalog sends {name!r} but probe.py does not register it")

    # And the two tier maps have to be the same map.
    for name in sorted(set(tiers) & set(go_tiers)):
        if tiers[name] != go_tiers[name]:
            problems.append(
                f"op {name!r} is tier {tiers[name]!r} in the catalog and "
                f"{go_tiers[name]!r} in the Go probe")
    for name in sorted(set(tiers) - set(go_tiers)):
        problems.append(f"op {name!r} has catalog cases but no Go tier registers it")

    # Fill the undocumented ops from UNDOCUMENTED, and assert that the two sets
    # are exactly each other.  Either direction of drift is a build failure: an
    # override left behind after someone documents the op would quietly outrank
    # the docstring, and an op that loses its docstring would ship an empty line
    # to the candidate.
    undocumented = {name for name, summary in summaries.items() if not summary}
    for name in sorted(undocumented - set(UNDOCUMENTED)):
        problems.append(
            f"op {name!r} has no docstring and no UNDOCUMENTED entry")
    for name in sorted(set(UNDOCUMENTED) - undocumented):
        if name not in summaries:
            problems.append(f"UNDOCUMENTED names {name!r}, which is not an op")
        else:
            problems.append(
                f"UNDOCUMENTED overrides {name!r}, which now has a docstring")
    for name in undocumented & set(UNDOCUMENTED):
        summaries[name] = UNDOCUMENTED[name]

    if problems:
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    table = {
        name: {
            "tier": tiers[name],
            "args": tags[name],
            "about": summaries[name],
        }
        for name in sorted(tags)
    }
    args.out.write_text(
        json.dumps(table, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    by_tier: dict[str, int] = {}
    for entry in table.values():
        by_tier[entry["tier"]] = by_tier.get(entry["tier"], 0) + 1
    shown = "  ".join(f"{t}={n}" for t, n in sorted(by_tier.items()))
    print(f"ops: {len(table)} over {len(by_tier)} tiers  ({shown})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
