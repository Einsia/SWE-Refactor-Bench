#!/usr/bin/env python3
"""Run stage 2 against State A and assert it scores full marks.

Not a grading path.  This is the operator's instrument for one question: does
the frozen corpus have an answer that acorn -- the library every expectation was
computed from -- actually produces?  If it does not, a submission's shortfall
stops being readable as a behavioural difference, because it could equally be
the corpus being wrong, and every number this stage reports is uninterpretable.

The benchmark's premise is that a rewrite preserves behaviour.  State A is the
tree all 15,975 expectations were frozen from, so it is the one input whose
correct score is known in advance: 40.00/40.  Anything less is a defect in this
stage, not a finding about acorn.  Stage 1 is what stops a submission from
banking that score by changing nothing.

With one exception, and it is the reason this file cannot simply assert that
nothing skipped: a few dozen of those expectations are not acorn's answer, they
are acorn crashing, and the `message` the response schema requires is V8's
wording for the shape of the code that crashed.  State A reproduces them because
State A *is* the code that crashed; no port in any language can, and a port that
handles the input sanely is marked wrong for doing so.  `freeze.check_unportable`
finds them by measurement, proves both halves of that argument against the
reference's own sources, and writes the list; every module that holds one skips
it.  So the assertion here is that each module skipped exactly the cases the list
puts in its families -- which is a stronger claim than the count it replaced, and
the only one that stays true when the corpus is reseeded.

Two things had to be built before this run was possible, and both are why the
file is committed rather than written when needed:

  1. Nothing could build a JavaScript tree.  The only builder was the Rust one,
     so `make build` on State A failed at argv 1 and all 15,975 behavioural
     cases were recorded against a binary that was never produced.
  2. Seventeen structural cases and six provenance gates ask about a Rust
     delivery.  Four of the six failed honestly on the absent artifact; two --
     `shim-clean` and `no-runtime-spawn` -- *passed with false sentences*,
     because a node build spawns no interpreter through the tripwire and leaves
     its ledger empty.  A gate that passes for the wrong reason is worse than
     one that fails, so provenance now runs at weight 0.00 and skips all six on
     this path.

Usage, from inside the built image:

    docker run --rm -v /tmp/out:/out swerefactor/lang04-behavioural:4 \\
        python3 /tests/behavioural/selftest.py --report /out/selftest.json

Exit status is the verdict: 0 when every assertion below holds.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

SUITE = Path("/tests/behavioural/suite.toml")
LIB = Path("/tests/behavioural/lib")
ASSETS = Path("/opt/assets")

# State A, as the image holds it.  Not `environment/original.tar.gz`: that would
# be a copy of the tree from outside the image, and the whole value of this run
# is that it uses the same bytes the freeze used.  manifest_check.py asserts
# this directory shipped.
STATE_A = Path("/opt/assets/baseline")

# What the behavioural surface must score, exactly.  A range would hide the
# thing this file exists to detect: 0.9997 is five cases in fifteen thousand
# having no answer, and that is the finding, not a rounding artifact.
BEHAVIOURAL_MODULES = (
    "parser-suite", "parser-options", "parser-versions", "tokenizer", "loose",
    "walk", "find-node", "generated", "tables", "whole-libraries", "cli",
)

# Modules whose subject is the Rust delivery.  `structure` asks 10 of its 17
# cases of a pre-migration tree and skips 7.  The assertion made about them is
# not a rate -- see `assertions`.
#
# Every name here has to be a module the suite runs.  The loop reports "the
# module did not report" when it finds none, and about a name nothing answers
# to that is a finding this file invented rather than one it measured.
DELIVERY_MODULES = ("structure",)

# Empty, and it has to stay empty.  Nothing is exempt from scoring nothing: an
# all-skipped module rates 0.0 and keeps its weight, so it caps the stage.  The
# converse loop below is the half with teeth -- a module reporting weight 0.00
# is a module that runs and earns nothing, which means the weight table and
# this file disagree about which modules are exempt.
ZERO_WEIGHT_MODULES: tuple[str, ...] = ()


def run_suite(repo: Path, work: Path, only: list[str] | None) -> object:
    """Run the stage in process.

    Not through `swerefactor behavioural`: that entry point passes a Path where a
    callable is expected (cli.py:545 in the pinned copy), so it raises before
    the first module.  Constructing SuiteRunner is what the CLI would have done.

    No self-test mode is set, and that is the point: `SRB_SHIM=enforce` is left
    exactly as suite.toml declares it.  lang04 needs no such mode because the
    shim directory is populated either way and the JavaScript builder simply
    never puts it on PATH -- so this run exercises the same configuration a
    graded submission does, rather than a relaxed one.
    """
    sys.path.insert(0, "/opt/swerefactor")
    from swerefactor.config import Suite
    from swerefactor.behavioural import SuiteRunner

    suite = Suite.load(SUITE)
    if only:
        keep = set(only)
        unknown = keep - {m.id for m in suite.modules}
        if unknown:
            sys.exit(f"--only names no such module: {', '.join(sorted(unknown))}")
        suite.modules = [m for m in suite.modules if m.id in keep]

    class Log:
        def write(self, msg: str) -> None:
            print(msg, flush=True)
        info = write

        def __call__(self, msg: str) -> None:
            print(msg, flush=True)

    # `original` is State A here as well, and in this one run that is not a
    # mistake: the tree under test *is* the original.
    return SuiteRunner(suite=suite, repo=repo, original=STATE_A,
                       work=work, log=Log()).run()


def score(result: object, evaluation: Path | None) -> tuple:
    """Score with swerefactor's own grader, not a re-derivation of it.

    Re-implementing the arithmetic here would make this file agree with itself
    rather than with the thing that grades submissions -- and the rules that matter
    most (an all-skipped pool rates 0.0 while keeping its weight, a weight-0 check is
    deleted rather than pooled, a timed-out or unreached check is charged as a zero on
    a valid result) are exactly the ones a re-derivation gets wrong.
    """
    from swerefactor.config import ScoringPolicy
    from swerefactor.scoring import Verdict, grade_behavioural
    from swerefactor.tomlcompat import load as toml_load

    if evaluation is not None:
        raw = toml_load(evaluation)
        policy = ScoringPolicy.from_dict(raw.get("scoring") or {}, str(evaluation))
        task = str(raw.get("task", "lang04-acorn-js-to-rust"))
    else:
        # The library defaults are the benchmark's published policy.
        # evaluation.toml is outside this image's build context (which is
        # tests/behavioural/), so it can only arrive as a mount; without it the
        # assertions below compare against the default rather than against
        # this task's own file.
        policy = ScoringPolicy()
        task = "lang04-acorn-js-to-rust"
    verdict = Verdict(task=task, max_score=policy.max_score)
    grade_behavioural(result, verdict, policy)
    return verdict, policy


def state_a(into: Path) -> Path:
    """Copy State A somewhere writable.

    /opt/assets is chmod a-w by the Dockerfile and a build writes inside its own
    tree, so the tree has to be copied rather than built in place -- and the copy
    has to restore write bits, which `chmod -R a-w` removed from every file.
    """
    if not STATE_A.is_dir():
        sys.exit(f"{STATE_A} is not in this image; manifest_check.py should have "
                 f"refused the build")
    shutil.copytree(STATE_A, into)
    for path in [into, *into.rglob("*")]:
        path.chmod(path.stat().st_mode | 0o200)
    return into


def exclusion() -> tuple[dict[str, set[int]], dict[str, int], str]:
    """Which cases each module is expected to skip, and why it may.

    Some of the frozen corpus has no answer any port can give: the expectation is a
    crash inside acorn, and the `message` the response schema demands is V8's
    wording for the shape of the code that crashed rather than anything acorn
    defines.  `freeze.check_unportable` derives that list at image build time and
    proves the premise against the reference's own sources; every module that holds
    one of those cases skips it.

    Those cases are not emitted as checks at all -- a skip is charged 0, and an id
    no port can pass would cost every submission the same question forever -- so
    the assertion below is the strict one, plus a size check that keeps it honest:
    each module must skip nothing, and must have graded exactly its own slice minus
    the excluded cases in it.  Together those say what a skip-count assertion would
    say, without a skip having to exist to say it.

    Derived here from the frozen list and the module map rather than read back out of
    the run, because a count taken from the report would agree with the report no
    matter what the report said.

    Returns ({module_id: {case_id}}, {module_id: cases owned}, error) -- the error
    non-empty when the list could not be read at all, in which case every expected
    count is 0 and the size assertion is skipped rather than asserted against a
    denominator this function could not derive.

    The second map is what the size assertion needs: how many frozen cases each
    module owns in total.  Derived from the same catalog as the first, in one pass,
    so the two cannot disagree about which module a family belongs to.
    """
    try:
        sys.path.insert(0, str(LIB))
        import driver as drivermod

        assets = drivermod.Assets(ASSETS)
        owner = {fam: mid for mid, fams in drivermod.MODULES.items()
                 for fam in fams}
        owns: dict[str, int] = {}
        for cid, case in assets.cases.items():
            mid = owner.get(case.get("family") or "unknown")
            if mid is not None:
                owns[mid] = owns.get(mid, 0) + 1
        owed: dict[str, set[int]] = {}
        orphans = []
        for cid in sorted(assets.excluded):
            family = (assets.cases.get(cid) or {}).get("family") or "unknown"
            mid = owner.get(family)
            if mid is None:
                orphans.append(f"{cid} in {family!r}")
                continue
            owed.setdefault(mid, set()).add(cid)
        if orphans:
            # driver._check_exclusion is what owns this question and runs at
            # --self-check.  Repeated here because this file's per-module arithmetic
            # is only complete if every excluded case landed in some module: an
            # orphan is skipped by nobody, so no module's count would be wrong and
            # the case would simply never be graded.
            return owed, owns, (f"{len(orphans)} excluded case(s) are in families no "
                                f"module claims: {', '.join(orphans[:4])}")
        return owed, owns, ""
    except Exception as exc:  # noqa: BLE001 -- reported, not swallowed
        return {}, {}, f"{type(exc).__name__}: {exc}"


def assertions(result: object, verdict: object, policy: object) -> list[str]:
    """Everything that must hold.  Returns the failures, most specific first."""
    bad: list[str] = []
    by_id = {m.id: m for m in verdict.modules}
    owed_ids, owns_by_module, exclusion_error = exclusion()
    owed = {mid: len(ids) for mid, ids in owed_ids.items()}
    if exclusion_error:
        bad.append(f"exclusion: {exclusion_error}; the per-module skip counts "
                   f"below could not be derived, so they were asserted to be zero")

    for mid in BEHAVIOURAL_MODULES + ("build",):
        m = by_id.get(mid)
        if m is None:
            bad.append(f"{mid}: the module did not report")
            continue
        if m.status != "ok":
            bad.append(f"{mid}: module status={m.status} ({m.note})")
        if m.rate != 1.0:
            scored = m.passed + m.failed + m.errored
            bad.append(f"{mid}: rate={m.rate:.6f}, not 1.0 ({m.failed} fail, "
                       f"{m.errored} error of {scored} scored)"
                       + (f" -- {m.note}" if m.note else ""))
        # Zero, not `want`.  A case with no portable answer is no longer emitted as
        # a skip -- it is not emitted at all, because a skip is charged 0 now and an
        # id no port can pass would sit in every submission's denominator.  So the
        # assertion inverts: this module must skip *nothing*, and the excluded cases
        # are accounted for by size instead, immediately below.
        if m.skipped:
            bad.append(
                f"{mid}: {m.skipped} case(s) skipped, and no behavioural module may "
                f"skip anything. A skip is charged 0, so this is either a case with "
                f"no answer still being emitted as a check -- which would cost every "
                f"submission a question nobody can pass -- or a real failure recorded "
                f"under the one verdict that reads as nobody's fault.")
        # The size is where the exclusion is now visible: the module grades its
        # slice minus the cases the frozen list puts in its families.  Asserted
        # against the catalog rather than against the run, so a module that quietly
        # stopped grading a case it *can* ask fails here even though its rate is 1.0
        # and it reports no skips.
        scored = m.passed + m.failed + m.errored
        owns = owns_by_module.get(mid, 0)
        if owns and scored != owns - want:
            bad.append(
                f"{mid}: graded {scored} case(s); this module owns {owns} frozen "
                f"case(s) and the exclusion list puts {want} of them beyond any "
                f"port's reach, so it should have graded {owns - want}."
                + (f" ({exclusion_error})" if exclusion_error else ""))

    # And which ones, not just how many.  A module that withheld the right number of
    # the wrong cases has both a case with no answer being graded and a case with an
    # answer being waived, and every count in the run above still reconciles.  Read
    # from `unportable_cases`, which the driver writes for exactly this: it is the
    # record of which ids the exclusion actually reached.
    reported: dict[str, set[int]] = {}
    for unit in result.units:
        ids = (unit.metadata.get("metadata") or {}).get("unportable_cases")
        if ids:
            reported[unit.id] = {int(i) for i in ids}
    if not exclusion_error:
        for mid in sorted(set(owed_ids) | set(reported)):
            want, got = owed_ids.get(mid, set()), reported.get(mid, set())
            if want != got:
                bad.append(
                    f"{mid}: skipped case ids {sorted(got)[:6]} but the exclusion "
                    f"list puts {sorted(want)[:6]} in this module's families "
                    f"({len(got)} reported, {len(want)} owed; "
                    f"{sorted(want - got)[:4]} owed and not skipped, "
                    f"{sorted(got - want)[:4]} skipped and not owed)")

    for mid in DELIVERY_MODULES:
        m = by_id.get(mid)
        if m is None:
            bad.append(f"{mid}: the module did not report")
            continue
        # Not a rate assertion, and now for a second reason.  These cases ask about
        # a Rust delivery State A does not have, so the claim is narrower and
        # stronger: nothing here may report a *finding*.  A fail or an error means a
        # case reached a verdict about an artifact that does not exist.
        #
        # The second reason is that State A's rate here is no longer 1.0 and should
        # not be.  Its 7 inapplicable cases are charged 0 like any other skip, so it
        # rates 10/17 on `structure` -- correctly, because it is not a Rust delivery.
        # That is why the calibration claim in this file is about the behavioural
        # modules: they are the ones whose scale needs a readable 1.0.
        if m.failed or m.errored:
            bad.append(f"{mid}: {m.failed} fail + {m.errored} error; a case about "
                       f"the Rust delivery reported a finding against a tree "
                       f"that has none")

    # An all-skipped module rates 0.0 and keeps its weight, so a module that
    # scores nothing caps the stage, and nothing is exempt from that:
    # `ZERO_WEIGHT_MODULES` is empty and the loop below it is what has teeth.  A
    # module reporting weight 0.00 means the weight table and this file disagree
    # about which modules are exempt.
    for mid in ZERO_WEIGHT_MODULES:
        m = by_id.get(mid)
        if m is None:
            bad.append(f"{mid}: the module did not report")
            continue
        if m.weight != 0.0:
            # The cap is computed from the weights this run actually reported,
            # not from a remembered total: the whole failure being described is
            # the weight table having changed.
            wsum = sum(x.weight for x in verdict.modules)
            capped = ((wsum - m.weight) / wsum * policy.behavioural_points
                      if wsum else 0.0)
            bad.append(f"{mid}: weight={m.weight}, not 0.00; an all-skipped "
                       f"module keeps its weight in the denominator, so this "
                       f"caps the stage at {capped:.2f}/"
                       f"{policy.behavioural_points:g}")
        if m.passed:
            bad.append(f"{mid}: {m.passed} case(s) passed at weight 0.00; if "
                       f"this module can earn credit it should not be retired")
    for m in verdict.modules:
        if m.weight == 0.0 and m.id not in ZERO_WEIGHT_MODULES:
            bad.append(f"{m.id}: weight 0.00 but not listed as retired; the "
                       f"module runs and earns nothing, and no assertion here "
                       f"covers it")

    for c in result.checks:
        if c.verdict in ("fail", "error"):
            bad.append(f"{c.unit}/{c.id}: {c.verdict} -- {c.summary[:160]}")
        if c.verdict == "skip" and not (c.summary or c.detail):
            # A skip with no reason is indistinguishable from a case that was
            # forgotten, and it costs its module credit now, so the reason is the
            # only thing that says whether the charge is the right one.
            bad.append(f"{c.unit}/{c.id}: skipped with no reason recorded")

    # The tree that scored has to be identified as the pre-migration one, in a
    # field rather than in prose.  Without this, a report of 40.00/40 with 13
    # skips would be indistinguishable from a Rust submission that had somehow
    # skipped its way past every delivery question -- and that report is the one
    # thing this run must never be able to forge.
    build = next((u for u in result.units if u.id == "build"), None)
    if build is None:
        bad.append("build: the module did not report, so the language and shim "
                   "posture cannot be checked")
    else:
        # Nested, and deliberately read at the nested path: swerefactor's runner
        # puts a module's own emitted metadata at unit.metadata["metadata"]
        # verbatim (behavioural.py:273-276), beside the keys it adds itself.
        # Reading the outer dict would silently never find the flag.
        meta = build.metadata.get("metadata", {})
        if meta.get("language") != "javascript":
            bad.append(f"build: metadata.language is {meta.get('language')!r}; "
                       f"this run is defined over the pre-migration tree, and a "
                       f"report not saying so cannot be read as one")
        if meta.get("shim_enforced") is not True:
            bad.append("build: metadata.shim_enforced is not true; the "
                       "interpreter tripwire was disabled, so this is not the "
                       "configuration a submission is graded under")

    if verdict.harness_error:
        bad.append(f"harness: {verdict.harness_error}")
    if verdict.blocked_by:
        bad.append(f"blocked_by={verdict.blocked_by}")
    if verdict.behavioural_rate != 1.0:
        bad.append(f"behavioural_rate={verdict.behavioural_rate:.6f}, not 1.0")
    if verdict.behavioural_points != policy.behavioural_points:
        bad.append(f"behavioural_points={verdict.behavioural_points:.4f}, not the "
                   f"full {policy.behavioural_points:g}")
    # The scorer's own predicate, not a second reading of the rate above.  Stage 2
    # is paid on `complete` per weighted row, so a row is what a failure should
    # name -- "structure is not complete" sends a reader to a module, where
    # "behavioural_points=0.00" sends them back to this table to work out which.
    for module in verdict.modules:
        if module.weight > 0 and not module.complete:
            bad.append(f"module {module.id} is weighted and not complete: "
                       f"{module.passed}/{module.scored_weight:g} scored check(s) "
                       f"passed")
    return bad


def table(verdict: object) -> str:
    rows = [f"{'module':<17} {'weight':>6} {'rate':>8} "
            f"{'pass':>6} {'fail':>5} {'err':>5} {'skip':>5}  note"]
    for m in verdict.modules:
        rows.append(f"{m.id:<17} {m.weight:>6.2f} {m.rate:>8.4f} "
                    f"{m.passed:>6d} {m.failed:>5d} {m.errored:>5d} "
                    f"{m.skipped:>5d}  {m.note[:56]}")
    return "\n".join(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", type=Path, default=None,
                    help="tree to run against; default is State A from /opt/assets")
    ap.add_argument("--evaluation", type=Path, default=None,
                    help="tests/evaluation.toml, mounted; without it the "
                         "published default policy is used and the report says so")
    ap.add_argument("--only", default="",
                    help="comma-separated module ids, for iterating on one module")
    ap.add_argument("--report", type=Path, default=None, help="write JSON here")
    ap.add_argument("--keep", action="store_true",
                    help="leave the copied tree and build work in place")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="selftest-"))
    try:
        repo = args.repo or state_a(tmp / "src")
        print(f"repo:       {repo}")
        print(f"evaluation: {args.evaluation or '(library default policy)'}")
        result = run_suite(repo, tmp / "work",
                           [s for s in args.only.split(",") if s] or None)
        verdict, policy = score(result, args.evaluation)

        print("\n" + table(verdict))
        bad = assertions(result, verdict, policy)
        partial = bool(args.only) or args.repo is not None

        print(f"\nbehavioural_rate   {verdict.behavioural_rate:.6f}")
        print(f"behavioural_points {verdict.behavioural_points:.2f} / "
              f"{policy.behavioural_points:g}  "
              f"(paid whole, or not at all)")

        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps({
                "repo": str(repo),
                "partial": partial,
                "evaluation": str(args.evaluation) if args.evaluation else None,
                "verdict": verdict.to_dict(),
                "failures": bad,
                "result": result.to_dict(),
            }, indent=2, sort_keys=True))
            print(f"report:           {args.report}")

        if partial:
            # A subset run cannot make the claim: the rate is over the modules
            # that ran, and asserting 1.0 on it would pass for a single module.
            print("\nPARTIAL: --only/--repo given, so the stage-level "
                  "assertions below are reported but not conclusive")
        if bad:
            print(f"\n{len(bad)} assertion(s) failed:")
            for line in bad[:60]:
                print(f"  - {line}")
            if len(bad) > 60:
                print(f"  ... and {len(bad) - 60} more")
            return 0 if partial else 1
        print("\nOK: the frozen corpus is answerable by the library it was "
              "frozen from, and State A scores full marks on the behavioural "
              "stage")
        return 0
    finally:
        if not args.keep:
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            print(f"kept: {tmp}")


if __name__ == "__main__":
    raise SystemExit(main())
