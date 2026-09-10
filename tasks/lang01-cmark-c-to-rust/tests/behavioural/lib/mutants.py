#!/usr/bin/env python3
"""Mutation testing for the suite: does the case catalog actually detect?

A suite that passes on the reference proves only that it is self-consistent.  A
suite worth grading with also has to *fail* when behavior changes -- and the
changes that matter are the specific ones a from-scratch reimplementation gets
wrong.  So each mutant here is a realistic porting mistake, injected into State
A's C source: a tab stop of 8 instead of 4, an unescaped double quote, a
sourcepos line numbered from zero, smart quotes that never turn.

Each mutant is graded by the real stage-2 runner and must be caught.  A mutant
no module detects is a hole in the catalog, and the exit status says so.

The mutation half is a maintainer's tool.  It exists because stage 2 is the
rule-based half of the ladder, and the honest objection to a rule-based suite is
that nobody knows whether its 4,169 cases discriminate or merely agree with
themselves.  Running it answers that in numbers: eleven mutants, ten of which
must be caught and one of which must not.

The identity half is not optional and not a maintainer's tool: `--identity-only`
runs it alone, in about twenty seconds, and the stage-2 Dockerfile runs it at
image build time.  It is there because nothing else in the ladder can catch a
suite that its own reference cannot pass.  Stage 3 cannot answer that question:
it never executes these cases, and it only runs when stage 2 paid all 40 -- so in
exactly the failure this catches, the ladder stops at stage 2 and stage 3 is
never reached.

Mutants are C by construction, and this runs them in exactly the mode the ladder
grades in: the shim records a repository compile rather than refusing it, so the
behavioural comparison happens without the suite being reconfigured for it.  That
matters more than it sounds.  A self-test that forced SRB_SHIM=off would measure a
configuration nothing else uses, and could report a clean 1.0 on a tree the graded
mode scores near zero.  The C-toolchain guards do fail on every tree here, at
weight zero, which is why `provenance` is excluded from the rate and asserted
separately.

  python3 lib/mutants.py --source /path/to/state-a --suite ./suite.toml \\
      --out /tmp/mut/out --workspace /tmp/mut/work
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Mutant:
    name: str
    path: str
    old: str
    new: str
    expect: str
    count: int = 1


# Every `old` string below was verified to occur exactly `count` times in the
# pinned 0.31.1 source.  The count is re-asserted at injection time, so a mutant
# that stopped applying (because the string drifted) fails loudly instead of
# quietly reporting an undetected mutation and blaming the catalog.
MUTANTS: list[Mutant] = [
    # --- block parsing -----------------------------------------------------
    Mutant(
        "tab-stop-8", "src/blocks.c",
        "#define TAB_STOP 4", "#define TAB_STOP 8",
        "tab expansion in indented code, list markers and block quotes",
    ),
    # --- HTML escaping -----------------------------------------------------
    Mutant(
        "no-quot-escape", "src/houdini_html_e.c",
        '{"",      "&quot;", "&amp;", "&#39;",',
        '{"",      "\\"",     "&amp;", "&#39;",',
        "HTML attribute escaping of the double quote",
    ),
    Mutant(
        "gt-escape-case", "src/houdini_html_e.c",
        '"&#47;", "&lt;",   "&gt;"}', '"&#47;", "&lt;",   "&GT;"}',
        "HTML escaping of the greater-than sign",
    ),
    # --- inline smart punctuation -----------------------------------------
    Mutant(
        "smart-quote-direction", "src/inlines.c",
        'static const char *LEFTDOUBLEQUOTE = "\\xE2\\x80\\x9C";',
        'static const char *LEFTDOUBLEQUOTE = "\\xE2\\x80\\x9D";',
        "--smart opening double quote (renders as the closing one)",
    ),
    Mutant(
        "emdash-to-endash", "src/inlines.c",
        'static const char *EMDASH = "\\xE2\\x80\\x94";',
        'static const char *EMDASH = "\\xE2\\x80\\x93";',
        "--smart em dash",
    ),
    # --- options handling --------------------------------------------------
    Mutant(
        "hardbreaks-ignored", "src/html.c",
        "if (options & CMARK_OPT_HARDBREAKS) {",
        "if (0 && (options & CMARK_OPT_HARDBREAKS)) {",
        "--hardbreaks",
    ),
    # --- source positions, seen through both the API and the XML renderer --
    Mutant(
        "start-line-off-by-one", "src/node.c",
        "int cmark_node_get_start_line(cmark_node *node) {\n"
        "  if (node == NULL) {\n    return 0;\n  }\n  return node->start_line;",
        "int cmark_node_get_start_line(cmark_node *node) {\n"
        "  if (node == NULL) {\n    return 0;\n  }\n  return node->start_line - 1;",
        "cmark_node_get_start_line (API only; the XML renderer reads the field "
        "directly, so this separates probe coverage from CLI coverage)",
    ),
    Mutant(
        "xml-sourcepos-swap", "src/xml.c",
        "node->start_line, node->start_column, node->end_line,\n"
        "               node->end_column);",
        "node->start_line, node->end_column, node->end_line,\n"
        "               node->start_column);",
        "--sourcepos columns in XML output",
    ),
    # --- the commonmark round-trip renderer -------------------------------
    Mutant(
        "cm-bullet-marker-width", "src/commonmark.c",
        "      marker_width = 4;", "      marker_width = 2;",
        "commonmark bullet-list indentation on re-render",
    ),
    # --- the man renderer --------------------------------------------------
    Mutant(
        "man-no-hyphen-escape", "src/man.c",
        "  case 45:\n    cmark_render_ascii(renderer, \"\\\\-\");",
        "  case 45:\n    cmark_render_ascii(renderer, \"-\");",
        "man output escaping of the hyphen",
    ),
    # --- control: semantically identical, must NOT be detected -------------
    Mutant(
        "control-noop", "src/blocks.c",
        "parser->line_number++;", "parser->line_number += 1;",
        "nothing: a no-op control mutant that must NOT be detected",
    ),
]


def apply_mutant(tree: Path, mutant: Mutant) -> None:
    target = tree / mutant.path
    text = target.read_text(encoding="utf-8")
    found = text.count(mutant.old)
    if found != mutant.count:
        raise SystemExit(
            f"mutant {mutant.name}: expected {mutant.count} occurrence(s) of "
            f"{mutant.old!r} in {mutant.path}, found {found}. The mutant is stale "
            f"and would have reported a false hole in the catalog."
        )
    target.write_text(text.replace(mutant.old, mutant.new), encoding="utf-8")


#: Modules whose verdict is not evidence about a mutant.
#:
#: `provenance` asks whether the shipped objects were produced by rustc and
#: whether any C translation unit is recorded in the artifacts.  Every tree this
#: tool grades is State A with one line changed, so those gates fail for all of
#: them, identically, before any document is rendered.  Counting that as
#: "detected" would let a catalog with no behavioural coverage at all report a
#: clean sweep.  Detection here means a *behavioural* module noticed.
NON_BEHAVIOURAL = ("provenance",)

# Imported rather than restated: which provenance gates are recorded-but-not-charged
# is a fact about the stage, and a copy here would let the two drift apart in the
# direction that matters -- a gate quietly dropped from the charge would also be
# quietly dropped from the identity assertion below.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from driver import RECORDED_ONLY_GATES  # noqa: E402


def _runner():
    """The shared stage-2 runner, imported the way a stage image sees it.

    Deliberately the real one rather than a copy: a mutation harness that graded
    with its own scoring would measure the harness, and the number this tool
    reports has to be the number the ladder would report.  `pooled` comes from the
    same import for the same reason.
    """
    candidates = [Path(os.environ.get("SRB_INFRA") or "/opt/swerefactor")]
    # Built one at a time rather than as a literal tuple.  In a checkout this file
    # is five levels below the repo root and `parents[4]` is infra/; inside a stage
    # image it lives at /tests/behavioural/lib and there is no parents[4] at all, so
    # evaluating it eagerly raised IndexError before the first candidate -- the one
    # that exists there -- was ever tried.
    here = Path(__file__).resolve()
    if len(here.parents) > 4:
        candidates.append(here.parents[4] / "infra")
    for candidate in candidates:
        if (candidate / "swerefactor" / "behavioural.py").is_file():
            if str(candidate) not in sys.path:
                sys.path.insert(0, str(candidate))
            from swerefactor import config, behavioural  # noqa: PLC0415
            from swerefactor.result import pooled  # noqa: PLC0415
            return config, behavioural, pooled
    raise SystemExit(
        "cannot find the swerefactor package. Set SRB_INFRA to the directory "
        "holding swerefactor/, or run this from a checkout with infra/ in it."
    )


def grade(repo: Path, args: argparse.Namespace, tag: str) -> dict:
    """Run the whole stage-2 suite over `repo` and return a per-module digest."""
    config, behavioural, pooled = _runner()
    out = args.out / tag
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)
    workspace = args.workspace / tag
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True, exist_ok=True)

    suite = config.Suite.load(args.suite)
    # Deliberately NOT overridden.  Forcing SRB_SHIM=off here would make the shim
    # refuse to compile a C mutant, so no behavioural case would run and this tool
    # would measure a configuration the ladder never uses -- reporting a clean
    # identity run on a tree the graded mode scores near zero.  The graded mode
    # records repository compiles instead of refusing them, so the mutants build
    # under it and the number below is the number the stage would report.
    if args.assets:
        suite.env["SWEREFACTOR_ASSETS"] = str(args.assets)
    # The fifth argument is the log *callable*, not a directory.  Passing a path
    # makes SuiteRunner.run() call a PosixPath and raise TypeError on its first
    # line, which does not fail loudly here: it fails the identity self-test that
    # is the one thing in the ladder able to catch a suite its own reference cannot
    # pass, so the check meant to catch that would be dead while reporting nothing.
    def _log(message: str) -> None:
        print(f"[{tag}] {message}", flush=True)

    result = behavioural.SuiteRunner(suite, repo, None, workspace, _log).run()
    result.write(out / "behavioural.json")

    modules: dict[str, dict] = {}
    earned = weight = 0.0
    for unit in result.units:
        checks = result.checks_of(unit.id)
        passed = sum(1 for c in checks if c.verdict == "pass")
        # The stage's own arithmetic, not a copy of it: a mutation harness that
        # scored differently from the ladder would measure the harness.  `pooled`
        # counts checks, charges skips, and drops only weight-0 observations, so
        # the rate a mutant moves here is the rate the ladder would report.
        earned_checks, scored = pooled(checks)
        rate = (earned_checks / scored) if scored else 0.0
        if unit.status != "ok":
            rate = 0.0
        modules[unit.id] = {
            "status": unit.status,
            "rate": round(rate, 6),
            "passed": passed,
            "failed": sum(1 for c in checks if c.verdict == "fail"),
            "errored": sum(1 for c in checks if c.verdict == "error"),
            "total": len(checks),
            "summary": unit.summary[:400],
            "failing": [c.id for c in checks if c.verdict == "fail"][:12],
        }
        if unit.id in NON_BEHAVIOURAL:
            continue
        earned += unit.weight * rate
        weight += unit.weight
    return {
        "status": result.status,
        "rate": round(earned / weight, 6) if weight else 0.0,
        "modules": modules,
        "notes": list(result.notes),
    }


def failing_modules(report: dict) -> dict[str, int]:
    """Per-module failure counts, which is what a coverage claim is built from."""
    return {mid: slot["failed"] + slot["errored"]
            for mid, slot in report["modules"].items()
            if mid not in NON_BEHAVIOURAL
            and (slot["failed"] or slot["errored"] or slot["status"] != "ok")}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path,
                        help="a pristine State A tree to mutate")
    parser.add_argument("--suite", type=Path,
                        default=Path(__file__).resolve().parent.parent / "suite.toml",
                        help="the stage-2 suite manifest (default: ../suite.toml)")
    parser.add_argument("--assets", type=Path, default=None,
                        help="override the frozen expectations directory")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--only", action="append", default=[],
                        help="run only these mutants, by name; repeatable")
    parser.add_argument("--identity-only", action="store_true",
                        help="run the identity assertions and stop, skipping every "
                             "mutant; this is the build-time check")
    args = parser.parse_args(argv)
    if args.identity_only and args.only:
        parser.error("--identity-only runs no mutants, so --only cannot select any")
    for name in ("source", "suite", "assets", "out", "workspace"):
        value = getattr(args, name)
        if value is not None:
            setattr(args, name, value.resolve())
    unknown = sorted(set(args.only) - {m.name for m in MUTANTS})
    if unknown:
        parser.error("no such mutant(s): " + ", ".join(unknown))
    args.out.mkdir(parents=True, exist_ok=True)
    args.workspace.mkdir(parents=True, exist_ok=True)

    results: list[dict] = []
    print("=" * 72)
    print("identity: unmutated State A must pass every behavioral case")
    print("=" * 72)
    baseline_tree = args.workspace / "tree-identity"
    if baseline_tree.exists():
        shutil.rmtree(baseline_tree)
    shutil.copytree(args.source, baseline_tree, symlinks=True)
    identity = grade(baseline_tree, args, "identity")
    ident_rate = identity["rate"]
    graded = [m for m in identity["modules"] if m not in NON_BEHAVIOURAL]
    print(f"  weighted rate={ident_rate:.6f} over {len(graded)} behavioural "
          f"module(s); {', '.join(NON_BEHAVIOURAL)} excluded by construction")
    ok = True
    if ident_rate != 1.0:
        print("  FAIL: the reference does not pass its own suite; the catalog or "
              "the expectations are wrong")
        for module, count in sorted(failing_modules(identity).items()):
            print(f"    {module:24s} {count} failed")
        ok = False
    else:
        print("  OK")

    # Checked separately, because the rate above cannot see it.  `provenance` is
    # excluded from the rate by construction -- its toolchain gates fail on any C
    # tree, so counting them would mark every mutant "detected" for a reason that
    # has nothing to do with the mutation.  But the exclusion also hid a plain bug:
    # `no-corpus-answers` is weight 1.0 and raised AttributeError on a missing
    # constant, which the gate table turns into a failed gate rather than an error,
    # so it read as a finding about the submission and cost the reference a gate
    # while the identity run reported 1.0.  A gate that the reference cannot pass is
    # a broken gate; the weight-0 toolchain gates are the ones it is *expected* to
    # fail, so those are the only exemption.
    prov = identity["modules"].get("provenance")
    if prov is None:
        print("  FAIL: no provenance module in the identity result at all")
        ok = False
    else:
        unexpected = [g for g in prov["failing"]
                      if g.rsplit("/", 1)[-1] not in RECORDED_ONLY_GATES]
        # `failing` is a truncated list and `failed` is the true count, so a name
        # check alone would pass while the failures it could not name went unread.
        if prov["failed"] > len(prov["failing"]):
            print(f"  FAIL: {prov['failed']} provenance failure(s) but only "
                  f"{len(prov['failing'])} named; cannot tell whether the unnamed "
                  f"ones are the weight-0 gates")
            ok = False
        elif prov["failed"] > len(RECORDED_ONLY_GATES):
            print(f"  FAIL: {prov['failed']} provenance gate(s) failed but only "
                  f"{len(RECORDED_ONLY_GATES)} are recorded-only")
            ok = False
        if prov["errored"] or unexpected:
            print(f"  FAIL: the reference fails {len(unexpected)} scored "
                  f"provenance gate(s) and errors on {prov['errored']}; a gate the "
                  f"C implementation cannot pass is measuring the verifier")
            for gate in unexpected:
                print(f"    {gate}")
            ok = False
        else:
            print(f"  OK: provenance clean apart from the {len(prov['failing'])} "
                  f"weight-0 toolchain gate(s), which State A is meant to fail")

    selected = ([] if args.identity_only
                else [m for m in MUTANTS if not args.only or m.name in args.only])
    for mutant in selected:
        print()
        print("=" * 72)
        print(f"mutant {mutant.name}: breaks {mutant.expect}")
        print("=" * 72)
        tree = args.workspace / f"tree-{mutant.name}"
        if tree.exists():
            shutil.rmtree(tree)
        shutil.copytree(args.source, tree, symlinks=True)
        apply_mutant(tree, mutant)
        report = grade(tree, args, mutant.name)
        func = report["rate"]
        modules = failing_modules(report)
        detected = func < 1.0
        control = "must NOT be detected" in mutant.expect
        total_failed = sum(slot["failed"] + slot["errored"]
                           for slot in report["modules"].values())
        print(f"  weighted rate={func:.6f}  cases failed={total_failed}")
        for module, count in sorted(modules.items(), key=lambda kv: -kv[1])[:8]:
            print(f"    {module:24s} {count} failed")
            for case in report["modules"][module]["failing"][:3]:
                print(f"      {case}")
        if control:
            verdict = "OK" if not detected else "FAIL (control mutant detected)"
            ok = ok and not detected
        else:
            verdict = "OK" if detected else "FAIL (undetected: catalog hole)"
            ok = ok and detected
        print(f"  {verdict}")
        results.append({
            "mutant": mutant.name,
            "expect": mutant.expect,
            "control": control,
            "rate": func,
            "cases_failed": total_failed,
            "modules": modules,
            "detected": detected,
        })

    summary = args.out / "mutation-summary.json"
    summary.write_text(
        json.dumps(
            {
                "identity": {"rate": ident_rate,
                             "modules": identity["modules"]},
                "mutants": results,
                "all_ok": ok,
            },
            indent=1, sort_keys=True,
        ) + "\n",
        encoding="utf-8",
    )
    print()
    print(f"summary written to {summary}")
    print("RESULT:", "all mutants behaved as expected" if ok else "FAILURES ABOVE")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
