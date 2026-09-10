"""The repository's own test suite, executing.

State A ships 211 passing tests across the three web modules — web-bundle, web and
navigation — and they reach places the corpus does not.  They construct requests
against an in-process container, assert on parsed response objects, exercise error
paths the HTTP surface smooths over, and encode invariants the authors thought were
worth writing down.  A migration that keeps them passing has kept those invariants.

The scoring question is what to compare against, and the answer is: State A's own
count, measured in the same run rather than hard-coded.  Two reasons.

A hard-coded 211 would be a number in a file that has to be re-derived whenever
anything upstream changes, and would be wrong silently.  More importantly, a
submission is expected to PORT these tests, not preserve them byte for byte.  A
Dropwizard `ResourceExtension` has no meaning in the target framework; the same
assertion rewritten against the target's test support is the correct migration, and
it may end up as a different number of test METHODS than it started as.  So the
comparison is on how many pass relative to how many the reference runs, floored at
the reference's count — a submission that runs more tests than State A did is not
penalised, and one that runs fewer has lost coverage.

What this cannot detect, and does not pretend to: a submission that ported the
tests and weakened them.  A test method that still exists and asserts nothing
counts here.  That is stage 1's question — its `own_tests_were_ported` gate reads
both trees' test sources and asks whether the assertions survived the port — and
stage 3's, where an adversary who has both trees can look for exactly that.  This
module measures execution, which is the part that can be measured mechanically.

The dependence runs the other way too, which is why the count is worth its weight
even though stage 1 reads the same files.  A method that lost its `@Test`
annotation and kept everything else is INVISIBLE to reading: the body is there, the
assertions are there, the port is faithful, and a reviewer comparing the two trees
sees a correctly migrated test.  It simply is not collected, so it cannot fail, and
the only thing that shows it is a count of what ran.  That is not hypothetical --
it is what the delta caught the first time this module graded a real submission,
one method in one file out of 240 classes, with the other seven modules' counts
identical on both sides.

Deleting them scores zero here.  That asymmetry is deliberate: porting a test suite
is part of porting a repository, and a submission that dropped the suite has
removed the evidence that anything else it did was correct.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))) + "/lib")

import toolchain as tc  # noqa: E402

#: Surefire's summary line, which Maven prints per module and once in aggregate.
_SUMMARY = re.compile(
    r"Tests run: (\d+), Failures: (\d+), Errors: (\d+), Skipped: (\d+)")


def _totals(log: str) -> dict:
    """Sum the per-module summaries, ignoring the aggregate reprint.

    Surefire prints a summary per module and then a "Results:" block per module;
    both match.  Taking the lines that follow a "Results:" marker counts each
    module exactly once, which is what makes the two sides comparable even when
    they have different numbers of modules.
    """
    runs = failures = errors = skipped = 0
    in_results = False
    for line in log.splitlines():
        if line.strip().endswith("Results:"):
            in_results = True
            continue
        m = _SUMMARY.search(line)
        if m and in_results:
            runs += int(m.group(1))
            failures += int(m.group(2))
            errors += int(m.group(3))
            skipped += int(m.group(4))
            in_results = False
    return {"run": runs, "failures": failures, "errors": errors,
            "skipped": skipped,
            "passed": runs - failures - errors - skipped}


def _run_tests(name: str, tree: Path) -> tuple[dict, str, int]:
    """`mvn test` over the whole tree, offline.  Never raises on test failure.

    A failing test is a RESULT here, not an error: the module's job is to count
    what passed, and a non-zero exit from Maven is the normal way that gets
    reported.  Only a Maven that could not run at all is an exception.
    """
    argv = tc.mvn_argv(["test"], skip_tests=False)
    try:
        proc = tc.run(argv, cwd=tree, timeout=5400, log=f"tests-{name}.log")
    except Exception as exc:                       # noqa: BLE001
        return ({"run": 0, "failures": 0, "errors": 0, "skipped": 0,
                 "passed": 0}, f"{type(exc).__name__}: {exc}", -1)
    text = tc.output_of(proc)
    return _totals(text), text, proc.returncode


def main() -> int:
    report = tc.Report()
    reference = tc.REFERENCE_TREE
    if not reference.exists():
        return tc.grader_failure(
            report, "reference-present",
            f"no reference tree at {reference}.  The `build` module unpacks it "
            f"and is declared first and required; if it failed, this is the "
            f"symptom.")

    ref, ref_log, ref_code = _run_tests("reference", reference)
    if ref["run"] == 0:
        return tc.grader_failure(
            report, "reference-tests-ran",
            "the reference tree's own test suite reported no tests at all, so "
            "there is no baseline to compare the submission's against.  The "
            "submission has NOT been judged on this module.")
    report.note("reference-baseline",
                f"the reference ran {ref['run']} test(s): {ref['passed']} passed, "
                f"{ref['failures']} failed, {ref['errors']} errored, "
                f"{ref['skipped']} skipped (exit {ref_code})")

    sub, sub_log, sub_code = _run_tests("submission", tc.REPO)

    # 1. The suite still exists and runs at all.  Separated from the count so that
    #    "deleted the tests" and "the tests fail" are different findings.
    report.record(
        "suite-runs", sub["run"] > 0,
        f"the submission's own test suite ran {sub['run']} test(s)"
        if sub["run"] else
        "the submission's own test suite ran no tests at all — either it was "
        "removed, or the test phase does not execute",
        detail="\n".join(sub_log.splitlines()[-80:]), weight=2.0)

    # 2. Nothing that runs may fail.  A ported suite with failures is a suite
    #    reporting that the port is incomplete.
    clean = sub["failures"] == 0 and sub["errors"] == 0
    failing = [l for l in sub_log.splitlines()
               if re.search(r"\[ERROR\].*(Tests run|<<< (FAILURE|ERROR)!)", l)]
    report.record(
        "no-failures", clean,
        f"{sub['failures']} failure(s) and {sub['errors']} error(s) in the "
        f"submission's own suite"
        if not clean else
        "every test the submission's own suite ran passed",
        detail="\n".join(failing[:60]), weight=3.0)

    # 3. Coverage did not shrink.  Compared against the reference's count from
    #    this same run, so a change upstream cannot make this stale, and a
    #    submission that legitimately restructured its tests into a different
    #    number of methods is judged on the total rather than on a name list.
    kept = sub["passed"] >= ref["passed"]
    report.record(
        "coverage-kept", kept,
        f"the submission passes {sub['passed']} test(s) against the reference's "
        f"{ref['passed']}"
        + ("" if kept else
           f" — {ref['passed'] - sub['passed']} fewer, so coverage the original "
           f"had is no longer being exercised"),
        weight=3.0)

    return report.finish({
        "reference": ref,
        "submission": sub,
        "reference_exit": ref_code,
        "submission_exit": sub_code,
    })


if __name__ == "__main__":
    sys.exit(main())
