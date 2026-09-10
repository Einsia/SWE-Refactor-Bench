#!/usr/bin/env python3
"""Every unit test State A runs must still run, and reach the same verdict.

1328 cases across three projects, checked one at a time rather than in aggregate,
because aggregates hide the failures that matter: a build that runs 1200 of gson's
1277 cases looks fine on a summary line and is missing 77 checks of the library's
behaviour.

Three of these cases test the *build* rather than the library, and cannot pass
unless the migration reproduced the machinery around compilation:

  regression/OSGiTest
      reads META-INF/MANIFEST.MF off the test classpath and looks for
      ``Bundle-SymbolicName: com.google.gson``.  Under Maven bnd wrote the
      manifest into ``target/classes``, so it was simply there.  A Gradle bundle
      plugin writes it into the jar, so it has to be put on the test runtime
      classpath deliberately.

  behavioural/EnumWithObfuscatedTest
      asserts that its own enum's fields are *not* reflectively findable, i.e.
      that the class it runs against went through ProGuard.  With no obfuscation
      step it fails with "Enum is not obfuscated".

  behavioural/Java17RecordTest and the other Java 17 sources
      do not compile at release 7.  gson's tests need 17 while its main classes
      need 7, in one project.

This module reads the ``full`` configuration's JUnit XML from the build ledger.
It never reads a build script: whether test execution was switched off is stage
1's question, asked there by reading code, and asking it again here by grepping
would be asserting on implementation.
"""
import json
import os
import re

import pytest

DATA = os.path.join(os.environ.get("SRB_SUITE_DIR", "/tests/behavioural"), "data")
with open(os.path.join(DATA, "tests.json")) as _fh:
    TESTS = json.load(_fh)

#: ``data/tests.json`` keys projects by artifact id; the build ledger keys them by
#: Gradle project name.  Same three projects, two spellings.
PROJECT_OF = {"gson": "gson", "gson-extras": "extras", "gson-proto": "proto"}

MODULES = sorted(TESTS)
PASS_CASES = [(m, c) for m in MODULES for c in TESTS[m]["expected_pass"]]
SKIP_CASES = [(m, c) for m in MODULES for c in TESTS[m]["expected_skip"]]

#: (module, class, case count) for every test class State A runs.
CLASS_CASES = []
for _m in MODULES:
    _seen = {}
    for _c in TESTS[_m]["expected_pass"] + TESTS[_m]["expected_skip"]:
        _seen[_c.partition("#")[0]] = _seen.get(_c.partition("#")[0], 0) + 1
    CLASS_CASES.extend((_m, _cls, _n) for _cls, _n in sorted(_seen.items()))

BUILD_COUPLED = [
    ("gson", "com.google.gson.regression.OSGiTest#testComGoogleGsonAnnotationsPackage"),
    ("gson", "com.google.gson.regression.OSGiTest#testSunMiscImportPackage"),
    ("gson", "com.google.gson.functional.EnumWithObfuscatedTest#testEnumClassWithObfuscated"),
]


def _id(pair):
    return "%s/%s" % (pair[0], pair[1])


def _cases(report, module):
    return report[PROJECT_OF[module]]["cases"]


def _norm(s):
    return re.sub(r"[^A-Za-z0-9]+", "", s).lower()


def _lookup(report, module, case):
    """The submission's outcome for one case, tolerating runner name variants.

    Gradle's JUnit runner and surefire do not always spell a case identically:
    nested classes arrive with ``$`` where surefire wrote ``.``, and a
    parameterised case's display name can carry its parameters differently.  A
    name difference is not a behaviour difference, so it is normalised away
    before a case is called missing.
    """
    got = _cases(report, module)
    if case in got:
        return got[case]
    want = _norm(case)
    for key, outcome in got.items():
        if _norm(key) == want:
            return outcome
    cls, _, name = case.partition("#")
    base = _norm(cls + name.split("[")[0])
    hits = {k: v for k, v in got.items()
            if _norm(k.partition("#")[0] + k.partition("#")[2].split("[")[0]) == base}
    if len(hits) == 1:
        return list(hits.values())[0]
    return None


# -------------------------------------------------------------- the test task --
@pytest.mark.audit
def test_test_task_ran(test_report):
    """Some JUnit XML exists.  Without it nothing below can be judged."""
    total = sum(r["total"] for r in test_report.values())
    assert total > 0, (
        "the build produced no JUnit XML at all.\n"
        "State A runs %d test cases; the build must run them and write their "
        "results where a report reader can find them (Gradle's default is "
        "<project>/build/test-results/test/)." % len(PASS_CASES + SKIP_CASES))


@pytest.mark.audit
def test_no_test_failed(test_report):
    """Not one test fails.  Every one of them passes or skips in State A."""
    failed = sorted("%s: %s" % (p, c)
                    for p, r in test_report.items() for c in r["failed"])
    assert not failed, (
        "%d test(s) failed:\n%s" % (len(failed), "\n".join(failed[:25])))


@pytest.mark.audit
@pytest.mark.parametrize("module", MODULES)
def test_module_test_count(test_report, module):
    """This project runs at least as many cases as State A runs.

    The commonest partial migration runs most of a project's tests and silently
    drops the ones needing something special -- the Java 17 sources, the
    obfuscated class, the protobuf-generated messages.
    """
    got = test_report[PROJECT_OF[module]]["total"]
    want = TESTS[module]["count"]
    missing = sorted(set(TESTS[module]["expected_pass"] +
                         TESTS[module]["expected_skip"]) -
                     set(_cases(test_report, module)))
    assert got >= want, (
        "%s ran %d test cases, State A runs %d.\nMissing, first ten: %s"
        % (module, got, want, missing[:10]))


@pytest.mark.audit
def test_total_test_count(test_report):
    got = sum(r["total"] for r in test_report.values())
    want = sum(TESTS[m]["count"] for m in TESTS)
    assert got >= want, \
        "the build ran %d test cases in total, State A runs %d" % (got, want)


@pytest.mark.behavioural
@pytest.mark.parametrize("module", MODULES)
def test_module_test_classes(test_report, module):
    """Every test class State A runs is still discovered.

    A test task whose include pattern is subtly wrong -- ``**/*Test.class``
    against ``**/*Test*`` -- silently loses whole classes.
    """
    want = {c.partition("#")[0] for c in
            TESTS[module]["expected_pass"] + TESTS[module]["expected_skip"]}
    got = {k.partition("#")[0] for k in _cases(test_report, module)}
    missing = sorted(want - {c.replace("$", ".") for c in got})
    assert not missing, (
        "%s did not run %d test class(es): %s"
        % (module, len(missing), missing[:10]))


@pytest.mark.behavioural
@pytest.mark.parametrize("module,cls,count", CLASS_CASES,
                         ids=["%s/%s" % (m, c) for m, c, _ in CLASS_CASES])
def test_test_class_ran_all_its_cases(test_report, module, cls, count):
    """This class contributes as many cases as it does in State A.

    Per-class rather than per-project because the two ways of losing a case look
    identical in a project total: a class that never ran, and a class that ran
    with half its methods undiscovered.  Only the second one still reports the
    class name, so only a per-class count separates them.
    """
    got = _cases(test_report, module)
    key = _norm(cls)
    n = sum(1 for k in got if _norm(k.partition("#")[0]) == key)
    assert n >= count, (
        "%s ran %d case(s), State A runs %d" % (cls, n, count))


# ---------------------------------------------------- build-coupled test cases --
@pytest.mark.audit
@pytest.mark.parametrize("pair", BUILD_COUPLED, ids=[_id(p) for p in BUILD_COUPLED])
def test_build_coupled_case_passes(test_report, pair):
    """This case fails unless the build machinery around it was reproduced."""
    module, case = pair
    got = _lookup(test_report, module, case)
    assert got is not None, (
        "%s did not run.\nIt is one of the cases that tests the build itself "
        "rather than the library." % case)
    assert got == "passed", "%s is %s, State A passes it" % (case, got)


# --------------------------------------------------------------- per-case parity
@pytest.mark.behavioural
@pytest.mark.parametrize("pair", PASS_CASES, ids=[_id(p) for p in PASS_CASES])
def test_case_passes(test_report, pair):
    """This test case runs and passes, as it does in State A."""
    module, case = pair
    got = _lookup(test_report, module, case)
    assert got is not None, "%s did not run in %s" % (case, module)
    assert got == "passed", "%s is %s, State A passes it" % (case, got)


@pytest.mark.behavioural
@pytest.mark.parametrize("pair", SKIP_CASES, ids=[_id(p) for p in SKIP_CASES])
def test_case_skipped(test_report, pair):
    """This case is reported, and reported as skipped.

    These are ``@Ignore``d methods and one assumption failure.  They must still
    be *discovered* -- a build that never sees them is not running the same suite
    -- and they must not be forced to run.
    """
    module, case = pair
    got = _lookup(test_report, module, case)
    assert got is not None, (
        "%s was not reported at all. State A reports it as skipped, which means "
        "its class is being run but this method is not discovered." % case)
    assert got == "skipped", "%s is %s, State A skips it" % (case, got)


@pytest.mark.behavioural
def test_skip_count_not_inflated(test_report):
    """Skips are not a way to pass.  Only State A's 19 cases may skip.

    Marking a failing test ignored would satisfy ``test_no_test_failed`` while
    removing the check entirely.
    """
    allowed = {_norm(c) for _, c in SKIP_CASES}
    extra = sorted("%s: %s" % (p, c)
                   for p, r in test_report.items() for c in r["skipped"]
                   if _norm(c) not in allowed)
    assert not extra, (
        "%d test(s) were skipped that State A runs:\n%s"
        % (len(extra), "\n".join(extra[:20])))


# ------------------------------------------------------------- report shape ---
@pytest.mark.behavioural
@pytest.mark.parametrize("module", MODULES)
def test_test_reports_are_per_project(b_full, module):
    """Each project with tests reports its own results.

    Collapsing the four projects into one source set produces a single report --
    and loses the per-project release levels that let gson's tests compile at 17
    while its main classes compile at 7.
    """
    project = PROJECT_OF[module]
    dirs = b_full.test_result_dirs(project)
    assert dirs, (
        "no JUnit XML under %s/build: %s's tests did not run as part of that "
        "project" % (project, module))


@pytest.mark.behavioural
def test_metrics_project_has_no_tests(test_report):
    """metrics has no test sources; State A runs none there.

    Recorded so that a submission which moves PerformanceTest out of gson, where
    State A keeps it, is noticed rather than rewarded for a tidier layout.
    """
    assert "gson-metrics" not in TESTS
    got = test_report["metrics"]["total"]
    assert got == 0, \
        "the metrics project ran %d test case(s); State A runs none" % got


@pytest.mark.behavioural
def test_no_project_invented_extra_cases(test_report):
    """A project does not report cases State A has never heard of.

    Extra passing tests are not a defect in themselves, so this is deliberately
    loose: it fires only when a project's total is more than a tenth above State
    A's, which is the shape of a suite that has had its own tests added to it
    rather than one whose runner spells two names differently.
    """
    loud = []
    for module in MODULES:
        want = TESTS[module]["count"]
        got = test_report[PROJECT_OF[module]]["total"]
        if got > want * 1.1 + 5:
            loud.append("%s: %d cases, State A runs %d" % (module, got, want))
    assert not loud, (
        "test suites grew substantially:\n%s\n\nThe migration is meant to "
        "preserve the suite, not extend it." % "\n".join(loud))
