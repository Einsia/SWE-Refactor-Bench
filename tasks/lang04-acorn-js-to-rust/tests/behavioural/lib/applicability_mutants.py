#!/usr/bin/env python3
"""Mutation testing for the applicability tables: does the self-check detect?

Narrower than lang01's and lang02's `mutants.py`, and deliberately named so.
Those mutate a reference implementation to ask whether the *case catalog* would
notice a behavioural change.  This file mutates no behaviour at all.  Its subject
is everything that decides **which cases are asked**, of which tree, and whether
they are asked at all:

    structure.JS_STRUCT_KEEP    the 10 structural cases a pre-migration tree can
                                answer
    structure.JS_STRUCT_SKIP    the 7 it cannot, each with the reason
    driver.JS_PROV_SKIP         all 6 provenance gates, likewise
    corpus/excluded.json        the frozen corpus cases whose own expected answer
                                is a crash inside the reference, so no port has an
                                answer to give -- see freeze.check_unportable

Those tables are what let State A take full marks, so an error in them is not a
lost point but a wrong premise: a case moved into a skip table is weight awarded
for a question nobody asked, and a case moved out of one is a Rust delivery
demanded of a JavaScript tree.  `driver._check_applicability` and
`driver._check_exclusion` exist to catch both directions, and a green run of an
assertion proves nothing about the assertion.  So each mutation below is one an
editor could plausibly make while maintaining the tables, and each must be
reported.

Mutation 6 is the one that shapes the file.  A check comparing the asked list
against `JS_STRUCT_KEEP` would be a tautology -- the asked list is *computed from*
`JS_STRUCT_KEEP`, so the comparison can only fire on an invented name and says
nothing whatever about the skip table.  Which is why the assertion is written
against the catalog instead, and why a rename that reaches neither table has a
mutation of its own: a count catches mutation 1, and only a check that owns the
question catches this one.

The exclusion mutations end with one that is not a table edit at all: a corpus
run, over four synthetic cases and a probe that answers every one of them
wrongly, asserting that the excluded case comes back skipped and the other three
come back failed.  Without it, every check on this path could pass while
`run_corpus` ignored the list entirely -- the two directions are separately
mutable, which is what lets a tautology on one of them survive.

Run from anywhere, on the host or in the image:

    python3 /tests/behavioural/lib/applicability_mutants.py

The exclusion group needs the frozen assets and is skipped, loudly, when they are
absent -- which is the case on an author's host.  Exit status is the verdict: 0
when the shipped tables pass their own check and every mutation that ran was
caught.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Same reason as driver.py's: `python3 -I` implies -P and drops the script's own
# directory from sys.path, and the engine modules import each other by bare name.
sys.path.insert(0, str(HERE))

import json  # noqa: E402
import os  # noqa: E402
import stat  # noqa: E402
import tempfile  # noqa: E402

import build as buildmod  # noqa: E402
import catalog  # noqa: E402
import driver as drivermod  # noqa: E402
import probe as probemod  # noqa: E402
import structure  # noqa: E402
import vlib  # noqa: E402

ASSETS = Path(os.environ.get("SWEREFACTOR_ASSETS", "/opt/assets"))

FAILURES: list[str] = []
RAN = 0


def mutate(label: str, apply, undo, check=None) -> None:
    """Break one table, run the check, restore it.

    The check is expected to *return* problems; a SystemExit is also a catch,
    because some of these tables are validated at import.  Restoring happens in
    a finally so that one uncaught mutation cannot cascade into the rest.

    `check` defaults to `_check_applicability` because that is what most of these
    mutations are about.  It is a parameter so the exclusion group can point at
    the check that owns *its* question: a mutation caught by the wrong assertion
    is the failure mode this file was written for.
    """
    global RAN
    RAN += 1
    check = check or drivermod._check_applicability
    apply()
    try:
        found = check()
    except SystemExit as exc:
        found = [f"SystemExit: {exc}"]
    except RuntimeError as exc:
        found = [f"RuntimeError: {exc}"]
    finally:
        undo()
    if not found:
        FAILURES.append(label)
    print(f"\n  {'caught' if found else 'MISSED'}: {label}")
    for problem in found[:2]:
        print(f"      {str(problem)[:150]}")


def exclusion_group() -> None:
    """The cases with no portable answer: is the list followed, and checked?

    Needs the frozen assets, because the list is a frozen input and `Assets`
    refuses to load one whose digest does not match.  So the mutations here edit
    the loaded object rather than the file: the digest check is not the subject,
    what the grader does with a list that passed it is.
    """
    if not (ASSETS / "verifier-manifest.json").is_file():
        # No count in this message.  The group's size is whatever the calls below
        # add up to, and a number written here would be a second claim about it
        # that nothing checks -- which is the shape of bug this file exists to
        # catch.
        print(f"\n  -- exclusion group skipped: no frozen assets at {ASSETS}, so "
              f"the mutations over the unportable-case list did not run. The "
              f"Dockerfile invokes this file in the image, after --self-check, "
              f"where they do.")
        return
    assets = drivermod.Assets(ASSETS)
    print(f"\nexclusion: {len(assets.excluded)} of {assets.count} corpus cases "
          f"have no portable answer")
    clean = drivermod._check_exclusion(assets)
    if clean:
        print("  the shipped exclusion list does not pass its own check:")
        for problem in clean:
            print(f"    - {problem}")
        FAILURES.append("the shipped exclusion list fails _check_exclusion")
        return
    victim = min(assets.excluded)

    # 8. An id in the list that the corpus does not hold: the list and the corpus
    #    were frozen from different runs, and every module would then owe a skip
    #    for a case that reaches no slice.
    mutate("the exclusion list names a case the corpus does not hold",
           lambda: assets.excluded.__setitem__(10 ** 9, "invented"),
           lambda: assets.excluded.pop(10 ** 9, None),
           check=lambda: drivermod._check_exclusion(assets))

    # 9. An excluded case in a family no module claims.  This is the drift
    #    `run_corpus` structurally cannot see -- no module holds the case, so no
    #    module notices it was never asked, and the report looks like a pass.
    orig_family = assets.cases[victim].get("family")
    mutate("an excluded case is in a family no module claims",
           lambda: assets.cases[victim].__setitem__("family", "orphan_family"),
           lambda: assets.cases[victim].__setitem__("family", orig_family),
           check=lambda: drivermod._check_exclusion(assets))

    # 10. The same case claimed by two modules: it would be skipped twice, and
    #     both modules' denominators would lose it.
    orig_modules = drivermod.MODULES
    doubled = dict(orig_modules)
    doubled["tables"] = tuple(doubled["tables"]) + (orig_family,)
    mutate("two modules claim the family an excluded case is in",
           lambda: setattr(drivermod, "MODULES", doubled),
           lambda: setattr(drivermod, "MODULES", orig_modules),
           check=lambda: drivermod._check_exclusion(assets))

    # 11. And the direction no table check can reach: does `run_corpus` follow the
    #     list at all?  Over a synthetic corpus, against a probe that answers
    #     every case wrongly, so a case that was asked cannot come back anything
    #     but failed.
    synthetic_corpus_group()


def _synthetic_corpus(root: Path) -> tuple[Path, Path, dict[int, dict], Path]:
    """Four cases, one expectation each, and a probe that is wrong about all of them.

    Written rather than sliced out of the frozen corpus so the shapes are visible
    here: what is being tested is the bookkeeping, and a real case would make the
    test depend on what the real probe does with it.
    """
    requests = root / "requests.ndjson"
    expected = root / "expected.ndjson"
    with requests.open("w", encoding="utf-8") as reqs, \
         expected.open("w", encoding="utf-8") as exps:
        for cid in (1, 2, 3, 4):
            reqs.write(json.dumps({"id": cid, "op": "noop"}) + "\n")
            exps.write(json.dumps({"id": cid, "ok": True, "answer": cid}) + "\n")
    cases = {cid: {"id": cid, "family": "synthetic"} for cid in (1, 2, 3, 4)}

    probe = root / "wrong-probe"
    probe.write_text(
        "#!/usr/bin/env python3\n"
        "# Answers every request, and is wrong about every one of them: a case\n"
        "# that reached this probe cannot come back passed.\n"
        "import json, sys\n"
        "for line in sys.stdin:\n"
        "    line = line.strip()\n"
        "    if not line:\n"
        "        continue\n"
        "    req = json.loads(line)\n"
        "    sys.stdout.write(json.dumps(\n"
        "        {'id': req['id'], 'ok': False, 'answer': 'wrong'}) + '\\n')\n"
        "    sys.stdout.flush()\n",
        encoding="utf-8",
    )
    probe.chmod(probe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return requests, expected, cases, probe


def synthetic_corpus_group() -> None:
    """Run one module's slice twice: with the exclusion honoured, and without."""
    global RAN
    with tempfile.TemporaryDirectory(prefix="l04-mutant-") as tmp:
        root = Path(tmp)
        requests, expected, cases, probe = _synthetic_corpus(root)
        log = vlib.Log(root / "log.txt")
        excluded = {3: "no portable answer: this is the case under test"}

        def run(exclusion):
            runner = probemod.Runner([sys.executable, str(probe)], log=log,
                                     cwd=root, env=vlib.base_env())
            return runner.run_corpus(
                requests, expected, cases, label="synthetic",
                weights={"synthetic": 1.0}, corpus_lines=4, excluded=exclusion)

        # The shipped path first: three asked and wrong, one skipped with its
        # reason.  This is a precondition, not a mutation -- it does not count
        # toward the tally -- and if it does not hold the mutation below proves
        # nothing, the same way a dirty `clean` run invalidates the rest.
        outcomes = run(excluded)
        asked = [c for c in outcomes if not c.not_applicable]
        skipped = [c for c in outcomes if c.not_applicable]
        ok = (len(outcomes) == 4 and len(skipped) == 1
              and skipped[0].case_id == "synthetic:3"
              and skipped[0].not_applicable == excluded[3]
              and not skipped[0].passed
              and len(asked) == 3 and not any(c.passed for c in asked))
        print(f"\n  {'ok' if ok else 'BROKEN'} (precondition): run_corpus skips "
              f"the excluded case and asks the rest")
        print(f"      {len(asked)} asked ({sum(c.passed for c in asked)} passed), "
              f"{len(skipped)} skipped"
              + (f", reason {skipped[0].not_applicable!r}" if skipped else ""))
        if not ok:
            FAILURES.append("run_corpus does not skip the excluded case")
            return

        # And the mutation: the list is dropped on the way in, as it would be by
        # an edit that forgot to thread it through one call site.  Every case is
        # then asked, and the one with no answer comes back failed -- a submission
        # charged for a question the image had decided is unanswerable.
        RAN += 1
        mutated = run({})
        leaked = [c for c in mutated if not c.not_applicable
                  and c.case_id == "synthetic:3"]
        caught = bool(leaked) and not leaked[0].passed
        print(f"\n  {'caught' if caught else 'MISSED'}: the exclusion is dropped "
              f"on the way into run_corpus")
        print(f"      case 3 came back "
              + ("asked and failed, which is what a submission would be charged"
                 if caught else "unchanged, so the skip did not come from the list"))
        if not caught:
            FAILURES.append("dropping the exclusion list changes nothing")


def main() -> int:
    declared = [c for _, c, _, _ in catalog.STRUCT_CASES]
    print(f"catalog declares {len(declared)} structural cases, "
          f"{len(drivermod.PROVENANCE_GATES)} provenance gates")

    clean = drivermod._check_applicability()
    print(f"\nclean: {len(clean)} problem(s)")
    for problem in clean:
        print(f"  - {problem}")
    if clean:
        print("\nthe shipped tables do not pass their own check; "
              "no mutation below would mean anything")
        return 1

    asked = [c for c in declared if not structure.skip_reason(c, buildmod.LANG_JS)]
    skipped = [c for c in declared if structure.skip_reason(c, buildmod.LANG_JS)]
    print(f"\n  JS tree:   asked {len(asked)} -> {' '.join(sorted(asked))}")
    print(f"             skipped {len(skipped)} -> {' '.join(sorted(skipped))}")
    rust = [c for c in declared if structure.skip_reason(c, buildmod.LANG_RUST)]
    print(f"  Rust tree: skipped {len(rust)} (must be 0)")

    keep = structure.JS_STRUCT_KEEP

    # 1. A measurable case moved into the skip table: weight awarded for a check
    #    that was never run.
    mutate("a measurable case is moved into the skip table (clean-target)",
           lambda: (setattr(structure, "JS_STRUCT_KEEP", keep - {"clean-target"}),
                    structure.JS_STRUCT_SKIP.__setitem__("clean-target",
                                                         "invented")),
           lambda: (setattr(structure, "JS_STRUCT_KEEP", keep),
                    structure.JS_STRUCT_SKIP.pop("clean-target", None)))

    # 2. A Rust-only case moved into KEEP: fails on a JavaScript tree, and the
    #    failure reads as a behavioural regression rather than as a bad table.
    mutate("a Rust-only case is moved into the keep table (lockfile-present)",
           lambda: setattr(structure, "JS_STRUCT_KEEP", keep | {"lockfile-present"}),
           lambda: setattr(structure, "JS_STRUCT_KEEP", keep))

    # 3. The Rust direction, which is the one a reader forgets: a real submission
    #    must be asked all 17, so the language branch has to stay a branch.
    orig_skip_reason = structure.skip_reason
    mutate("skip_reason stops special-casing Rust",
           lambda: setattr(structure, "skip_reason",
                           lambda check, language:
                           structure.js_struct_skip_reason(check)),
           lambda: setattr(structure, "skip_reason", orig_skip_reason))

    # 4. A provenance gate loses its reason, so the report carries a bare skip.
    prov_reason = drivermod.JS_PROV_SKIP["shim-clean"]
    mutate("a provenance gate loses its reason (shim-clean)",
           lambda: drivermod.JS_PROV_SKIP.__setitem__("shim-clean", ""),
           lambda: drivermod.JS_PROV_SKIP.__setitem__("shim-clean", prov_reason))

    # 5. Whitespace, which is present to the table and empty to a reader.
    struct_reason = structure.JS_STRUCT_SKIP["install-modes"]
    mutate("a structural skip reason becomes whitespace (install-modes)",
           lambda: structure.JS_STRUCT_SKIP.__setitem__("install-modes", "   "),
           lambda: structure.JS_STRUCT_SKIP.__setitem__("install-modes",
                                                        struct_reason))

    # 6. The catalog renames a case and the table keeps the old name.  This is
    #    the drift a count cannot see: 10 and 7 both still hold, and the renamed
    #    case reaches neither table.  See the note in the module docstring.
    orig_cases = catalog.STRUCT_CASES
    renamed = tuple(
        (cid, "release-artefacts" if check == "release-artifacts" else check,
         weight, note)
        for cid, check, weight, note in orig_cases
    )
    mutate("the catalog renames a skipped case and the table keeps the old name",
           lambda: setattr(catalog, "STRUCT_CASES", renamed),
           lambda: setattr(catalog, "STRUCT_CASES", orig_cases))

    # 7. The other direction of the same drift: a table entry for a case the
    #    catalog never declared.
    mutate("the keep table names a case the catalog does not declare",
           lambda: setattr(structure, "JS_STRUCT_KEEP",
                           keep | {"install-manpages"}),
           lambda: setattr(structure, "JS_STRUCT_KEEP", keep))

    exclusion_group()

    print("\n" + "=" * 70)
    if FAILURES:
        print(f"MISSED {len(FAILURES)}/{RAN}:")
        for label in FAILURES:
            print(f"  - {label}")
        return 1
    print(f"all {RAN} mutations caught, and the shipped tables pass "
          f"their own check")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
