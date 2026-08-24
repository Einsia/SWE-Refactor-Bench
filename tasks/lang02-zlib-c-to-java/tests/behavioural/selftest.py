#!/usr/bin/env python3
"""Run stage 2 against State A and assert the corpus is answerable.

Not a grading path.  This is the operator's instrument for one question: does the
frozen corpus have an answer that the library every expectation was computed from
actually produces?  If it does not, a submission's shortfall stops being readable
as a behavioural difference -- it could equally be the corpus being wrong -- and
every number this stage reports is uninterpretable.

Committed rather than written when needed.  A self-test that has to be
reconstructed before it can be run is a self-test that stops being run.

Usage, from inside the built image:

    docker run --rm -v /tmp/out:/out swerefactor/lang02-behavioural:1 \\
        python3 /tests/behavioural/selftest.py --report /out/selftest.json

Exit status is the verdict: 0 when every assertion below holds.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

SUITE = Path("/tests/behavioural/suite.toml")

# State A, as the image holds it.  Not `environment/original.tar.gz`: that would be
# a copy of the tree from outside the image, and the whole value of this run is that
# it uses the same bytes the freeze used.  manifest_check.py asserts this directory
# shipped; the Dockerfile deletes the tarball it came from.
STATE_A = Path("/opt/assets/baseline")

# What the behavioural surface must score, exactly.  A range would hide the thing
# this file exists to detect: 0.999 means some case has no answer, and the whole
# argument for reading a submission's rate as a behavioural measurement rests on
# this being 1.0 rather than nearly 1.0.
BEHAVIOURAL_MODULES = (
    "deflate-params", "deflate-strategy", "streaming", "dictionary", "inflate",
    "errors", "recovery", "oneshot", "deflate-state", "inflate-state", "gzip",
    "compose", "memory", "api-surface", "drivers",
)

# Modules whose subject is the Java delivery.  Their cases are skipped in this run
# -- see vlib.NotApplicable -- and the assertion made about them is not a rate but
# that every non-pass carries a reason naming the absent evidence.
DELIVERY_MODULES = ("structure", "provenance")


def run_suite(repo: Path, work: Path, only: list[str] | None) -> object:
    """Run the stage in process.

    Not through `swerefactor behavioural`: that entry point passes a Path where a
    callable is expected (cli.py:545 in the pinned copy), so it raises before the
    first module.  Constructing SuiteRunner is what the CLI would have done.

    `SRB_SELFTEST` goes into the process environment rather than into suite.env
    because the point is to exercise the path an operator has -- if the only way to
    switch the mode on were to edit the suite, this file would be testing a
    configuration nobody grading will ever run.
    """
    sys.path.insert(0, "/opt/swerefactor")
    from swerefactor.config import Suite
    from swerefactor.behavioural import SuiteRunner

    os.environ["SRB_SELFTEST"] = "1"
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

    # `original` is State A here as well, and in this one run that is not a mistake:
    # the tree under test *is* the original.  lang02's modules never read
    # SRB_ORIGINAL, so this only affects what the runner exports.
    return SuiteRunner(suite=suite, repo=repo, original=STATE_A,
                       work=work, log=Log()).run()


def score(result: object, evaluation: Path | None) -> object:
    """Score with swerefactor's own grader, not a re-derivation of it.

    Re-implementing the arithmetic here would make this file agree with itself
    rather than with the thing that grades submissions -- and the rules that matter
    most (an all-skipped pool rates 0.0 and keeps its weight, a weight-0 check is
    deleted rather than pooled, a timed-out or unreached check is charged as a zero
    on a valid result) are exactly the ones a re-derivation gets wrong.
    """
    from swerefactor.config import ScoringPolicy
    from swerefactor.scoring import Verdict, grade_behavioural
    from swerefactor.tomlcompat import load as toml_load

    if evaluation is not None:
        raw = toml_load(evaluation)
        policy = ScoringPolicy.from_dict(raw.get("scoring") or {}, str(evaluation))
        task = str(raw.get("task", "lang02-zlib-c-to-java"))
    else:
        # The library defaults are the benchmark's published policy.  evaluation.toml
        # is outside this image's build context (which is tests/behavioural/), so it
        # can only arrive as a mount; without it the assertions below compare
        # against the default rather than against this task's own file.
        policy = ScoringPolicy()
        task = "lang02-zlib-c-to-java"
    verdict = Verdict(task=task, max_score=policy.max_score)
    grade_behavioural(result, verdict, policy)
    return verdict, policy


def state_a(into: Path) -> Path:
    """Copy State A somewhere writable.

    /opt/assets is chmod a-w by the Dockerfile and configure writes into its source
    directory, so the tree has to be copied rather than built in place -- and the
    copy has to restore write bits, which `chmod -R a-w` removed from every file.
    """
    if not STATE_A.is_dir():
        sys.exit(f"{STATE_A} is not in this image; manifest_check.py should have "
                 f"refused the build")
    shutil.copytree(STATE_A, into)
    for path in [into, *into.rglob("*")]:
        path.chmod(path.stat().st_mode | 0o200)
    return into


def assertions(result: object, verdict: object, policy: object) -> list[str]:
    """Everything that must hold.  Returns the failures, most specific first."""
    bad: list[str] = []
    by_id = {m.id: m for m in verdict.modules}

    for mid in BEHAVIOURAL_MODULES + ("build",):
        m = by_id.get(mid)
        if m is None:
            bad.append(f"{mid}: the module did not report")
            continue
        if m.status != "ok":
            bad.append(f"{mid}: module status={m.status} ({m.note})")
        if m.rate != 1.0:
            # Deliberately exact.  0.9997 is one case in three thousand having no
            # answer, and that is the finding, not a rounding artifact.
            scored = m.passed + m.failed + m.errored
            bad.append(f"{mid}: rate={m.rate:.6f}, not 1.0 ({m.failed} fail, "
                       f"{m.errored} error of {scored} scored)"
                       + (f" -- {m.note}" if m.note else ""))
        if m.skipped:
            bad.append(f"{mid}: {m.skipped} case(s) skipped; the behavioural corpus "
                       f"is answerable by the reference and must not skip")

    for mid in DELIVERY_MODULES:
        m = by_id.get(mid)
        if m is None:
            bad.append(f"{mid}: the module did not report")
            continue
        # Not a rate assertion.  These cases ask about a jar that State A does not
        # have, so the claim is narrower and stronger: nothing here may report a
        # *finding*.  A fail or an error means the case reached a verdict about a
        # submission that does not exist.
        if m.failed or m.errored:
            bad.append(f"{mid}: {m.failed} fail + {m.errored} error; a case about the "
                       f"Java delivery reported a finding against a tree that has none")
        if m.rate <= 0.0:
            # An all-skipped pool is not a neutral outcome: a module with no scored
            # check rates 0.0 and is charged its own weight, so State A would pay for
            # observing an absence correctly.  Each of these modules has to keep at
            # least one check State A can pass.
            bad.append(f"{mid}: rate={m.rate}; a module with no passing scored check "
                       f"rates 0.0 and is still charged its weight")

    for c in result.checks:
        if c.verdict in ("fail", "error"):
            bad.append(f"{c.unit}/{c.id}: {c.verdict} -- {c.summary[:160]}")
        if c.verdict == "skip" and not (c.summary or c.detail):
            # A skip with no reason is indistinguishable from a case that was
            # forgotten, and it is the one verdict nobody re-reads.
            bad.append(f"{c.unit}/{c.id}: skipped with no reason recorded")

    # The run has to be labelled as a self-test, and labelled in a field rather than
    # in prose.  If this key were missing, a report of 74 skips and full marks would
    # be indistinguishable from a grading run that had somehow been handed a disabled
    # shim -- and that report is the one thing this mode must never be able to forge.
    build = next((u for u in result.units if u.id == "build"), None)
    if build is None:
        bad.append("build: the module did not report, so the self-test marker "
                   "cannot be checked")
    elif not build.metadata.get("metadata", {}).get("selftest"):
        # Nested, and deliberately read at the nested path: swerefactor's runner puts a
        # module's own emitted metadata at unit.metadata["metadata"] verbatim
        # (behavioural.py:273-276), beside the keys it adds itself.  Reading the outer
        # dict would silently never find the flag.
        bad.append("build: metadata.selftest is not set, so this report does not "
                   "declare itself a self-test")

    if verdict.harness_error:
        bad.append(f"harness: {verdict.harness_error}")
    if verdict.blocked_by:
        bad.append(f"blocked_by={verdict.blocked_by}")
    if verdict.behavioural_rate != 1.0:
        bad.append(f"behavioural_rate={verdict.behavioural_rate:.6f}, not 1.0")
    if verdict.behavioural_points != policy.behavioural_points:
        bad.append(f"behavioural_points={verdict.behavioural_points:.2f}, not the "
                   f"{policy.behavioural_points:g} a complete stage pays")
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
    rows = [f"{'module':<16} {'weight':>6} {'rate':>8} "
            f"{'pass':>6} {'fail':>5} {'err':>5} {'skip':>5}  note"]
    for m in verdict.modules:
        rows.append(f"{m.id:<16} {m.weight:>6.2f} {m.rate:>8.4f} "
                    f"{m.passed:>6d} {m.failed:>5d} {m.errored:>5d} {m.skipped:>5d}"
                    f"  {m.note[:60]}")
    return "\n".join(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", type=Path, default=None,
                    help="tree to run against; default is State A from /opt/assets")
    ap.add_argument("--evaluation", type=Path, default=None,
                    help="tests/evaluation.toml, mounted; without it the published "
                         "default policy is used and the report says so")
    ap.add_argument("--only", default="",
                    help="comma-separated module ids, for iterating on one module")
    ap.add_argument("--report", type=Path, default=None, help="write JSON here")
    ap.add_argument("--keep", action="store_true",
                    help="leave the extracted tree and build work in place")
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

        # This mode always sets SRB_SELFTEST=1 (run_suite), which switches the
        # compiler shim off and so lets vlib.c_self_test fire.  On State A that is
        # the point.  On a --repo tree that still contains the C it is a trap: the
        # tree installs a C zlib and no jar, c_self_test returns its reason, and
        # every check that reads the delivery becomes not_applicable -> skip.
        #
        # Half of that is absorbed by the scorer: a skip stays in the denominator and
        # scores 0 (result.pooled), so the modules that measure the jar are charged
        # for every check the missing delivery cost them rather than rating 1.0 over
        # the handful that did not need it.
        #
        # The other half is not.  With the shim off the C compiles, so `build`
        # publishes something and the modules reading it are measured against a C
        # zlib rather than against nothing.  A graded run enforces the shim, the build
        # fails, and what those modules read is absent.  So the two numbers still
        # differ, for a reason about the build rather than about the denominator, and
        # the PARTIAL notice below -- "not conclusive" -- reads as "close enough"
        # rather than "this tree was measured with an escape the grader does not
        # grant".
        #
        # So say it at the number, not in a footnote, and say it only when the licence
        # actually fired -- read from the marker the report carries rather than
        # re-deriving the predicate.  The marker is a bare `True` rather than a reason
        # string, so the mechanism is named in prose instead of interpolated.
        licensed = next(
            (u for u in result.units if u.id == "build"), None)
        licence = (licensed.metadata.get("metadata", {}).get("selftest")
                   if licensed is not None else None)
        if licence and args.repo is not None:
            skipped = sum(1 for c in result.checks if c.verdict == "skip")
            print(f"\nNOT A GRADED SCORE: this tree was measured under the "
                  f"reference self-test's licence -- SRB_SELFTEST=1, which switches "
                  f"the compiler shim off, so the C compiled and the modules above "
                  f"read whatever it published. A graded run enforces the shim: the "
                  f"build fails, and what those modules read is not there. The "
                  f"{skipped} skipped check(s) are already charged 0 in the rates "
                  f"above -- a skip stays in the denominator -- so the gap is in what "
                  f"the surviving checks were measured against, not in how many there "
                  f"were. Grade with `python3 -m swerefactor "
                  f"behavioural --task-dir / --repo ...` before believing a number.")

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
            # A subset run cannot make the claim: the rate is over the modules that
            # ran, and asserting 1.0 on it would pass for one module.
            print("\nPARTIAL: --only/--repo given, so the stage-level assertions "
                  "below are reported but not conclusive")
        if bad:
            print(f"\n{len(bad)} assertion(s) failed:")
            for line in bad[:60]:
                print(f"  - {line}")
            if len(bad) > 60:
                print(f"  ... and {len(bad) - 60} more")
            return 0 if partial else 1
        if partial:
            # The identity claim names State A, and a --repo/--only run did not grade
            # State A -- or graded only part of the suite.  This is the line a reader
            # takes as the verdict, so it has to be the line that knows the
            # difference: the PARTIAL notice above hedges "not conclusive", which is
            # not the same as declining to make the claim.
            #
            # The hedge is not enough because full marks can be the *correct* result
            # for a partial port.  A tree whose C is still present and whose
            # CMakeLists still builds it has the behavioural modules measuring the C
            # library against a corpus frozen from the C, with the jar-shaped checks
            # skipping for want of a jar.  Stage 2 measures behaviour; the do-nothing
            # case belongs to stage 1's required gates, which do catch it.  So the
            # number is right and only the sentence would be wrong, which is the
            # dangerous combination: nothing else in the output disagrees with it,
            # and only report.json's `repo` key shows which tree ran.
            print(f"\nOK (PARTIAL): every assertion this run could check passed, over "
                  f"{len(verdict.modules)} module(s) of {repo}")
            print("NOT the identity claim: run with neither --repo nor --only to "
                  "assert that State A scores full marks.")
            return 0
        print("\nOK: the frozen corpus is answerable by the library it was frozen "
              "from, and State A scores full marks on the behavioural stage")
        return 0
    finally:
        if not args.keep:
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            print(f"kept: {tmp}")


if __name__ == "__main__":
    raise SystemExit(main())
