"""The repository's own test suite still passes, and still exists.

Half of ChartMuseum's test files import the retired framework directly: one builds
an engine, another fabricates framework context values, another drives the route
matcher through them. Porting those is part of the work, and a submission that
deleted them to make the build green has thrown away the repository's own safety net
while claiming to have modernised it.

Three requirements, in increasing strictness:

* every package passes -- no failures, no build errors, no panics;
* every package that had tests still has tests, under its own import path, because
  that path is public API in Go and a submission may not quietly relocate a package
  out of existence;
* the total number of distinct test ids is at least the baseline's, so tests may be
  renamed, split or added, but not dropped wholesale.

Deliberately *not* required: that individual test names survive. A ported test file
may reasonably rename its suite or restructure its subtests, and grading names would
make a mechanical rename a zero.

The baseline was measured rather than assumed, on State A, in the construction
image, and is committed beside this module.

One flake is tolerated, and it is State A's own. A statefile is saved from three
goroutines and the storage backend writes with a truncate-then-write, so a reader
landing in that window reads zero bytes; zero bytes unmarshal cleanly into a struct
whose embedded pointer stays nil, and the "loaded" path dereferences it. The code is
untouched upstream code -- it fails State A too -- so a failing package is re-run
whole and forgiven only if it comes back completely clean. Beyond a few failing
packages the submission is broken rather than unlucky, and re-running is a waste of
the budget.

Measured on State A, because every part of the retry policy below is derived from
these numbers rather than from a guess at how a flake behaves:

* it is not one test. ``TestStatefiles``, ``TestMetrics`` and ``TestBadChartUpload``
  have each been observed failing, all in ``MultiTenantServerTestSuite`` -- one
  shared race in the storage backend, not three flaky tests;
* the whole-suite initial rate is 6 of 30 iterations (20%), across 4 CPUs, 192 CPUs,
  and the repo on a bind mount as the graded path has it. A single package re-run
  alone fails 2 of 32 (6%): the initial run compiles and runs eight packages
  concurrently, so it contends where a re-run does not;
* every one of those 6 was forgiven on retry 1. Retry 2 has never been needed;
* serialized, the flake does not reproduce: 30 of 30 iterations of the package pass
  at ``GOMAXPROCS=1`` on 4 CPUs, no DATA RACE in any of them, 9-10s each against
  13-15s concurrent. Both halves of that matter. A serialized attempt that
  deadlocked or ran past the budget would convert a flake into a certainty, so the
  duration was measured too, not just the verdict.

The serialized result is why RETRIES is not the lever. A re-run under the same
concurrency is another ticket in the same draw rather than a different experiment,
so clearing it repeatedly says nothing about the next run. Two things make an
attempt genuinely different, and both are below: the last attempt serializes (this
is a filesystem race, so its window is a function of concurrent writers), and a DATA
RACE is never forgiven at all, because this flake cannot produce one -- it is a
truncate-then-write between processes, which the race detector does not see, and it
surfaces as a nil-pointer panic. A submission whose port introduced a real memory
race reports DATA RACE and is not unlucky.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from collections import defaultdict

import toolchain as tc

RETRIES = 2               # extra attempts allowed per failing package
RETRY_MAX_PKGS = 3        # beyond this many failing packages, do not retry

rep = tc.Report()


def prepare_test_environment(source) -> None:
    """Make the repository's own test setup runnable offline.

    Its setup script fetches a helm release into ``./testbin`` and there is no
    network here. The image carries that exact version, so pre-placing it makes the
    script's own ``if [ ! -f testbin/helm ]`` skip the download -- the script is
    left untouched and still does the packaging work it exists to do.
    """
    helm = shutil.which("helm") or "/opt/helm/helm"
    testbin = source / "testbin"
    testbin.mkdir(exist_ok=True)
    target = testbin / "helm"
    if not target.exists() and os.path.exists(helm):
        shutil.copy2(helm, target)
        target.chmod(0o755)


def go_test(env, targets, tag, extra_env=None):
    """Run ``go test -race -json`` and parse the event stream.

    A package that fails to *build* emits no pass/fail event at all, so reading
    only results would score a submission whose tests do not compile as though it
    had no failures. The silent packages are what catch that.

    ``extra_env`` is how the final re-run becomes a different experiment rather
    than another draw in the same lottery -- see the retry block in ``main``.
    """
    if extra_env:
        env = {**env, **extra_env}
    proc = tc.run([tc.GO, "test", "-race", "-json", "-count=1", *targets],
                  cwd=tc.SOURCE, env=env, timeout=7200, log=f"{tag}.log")
    ids: dict[str, set] = defaultdict(set)
    results: dict[str, str] = {}
    failures: list[str] = []
    output: dict[str, list] = defaultdict(list)
    # Output is collected per test as well as per package.  The package tail is
    # what a build error needs, but it is useless for a failing test: this
    # repository's tests log at DEBUG to stdout, so the last 1200 bytes of a
    # package that ran is log spam.  `tc.run`'s tee of the whole stream lives in
    # $SRB_WORK, which is discarded with the container, so what is not put on the
    # check is not recoverable afterwards.
    test_output: dict[tuple, list] = defaultdict(list)
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        pkg, name, action = ev.get("Package"), ev.get("Test"), ev.get("Action")
        if pkg and name and action in ("pass", "fail", "skip"):
            ids[pkg].add(name)
            if action == "fail":
                failures.append(f"{pkg} :: {name}")
        if pkg and not name and action in ("pass", "fail", "skip"):
            results[pkg] = action
        if pkg and action == "output" and ev.get("Output"):
            output[pkg].append(ev["Output"])
            if name:
                test_output[(pkg, name)].append(ev["Output"])
                # A testify suite method's output is attributed to the subtest,
                # but the suite's own teardown and the -race report are attributed
                # to the parent.  Both matter, so the parent keeps a copy.
                parent = name.split("/", 1)[0]
                if parent != name:
                    test_output[(pkg, parent)].append(ev["Output"])
    return proc, ids, results, failures, output, test_output


def reports_a_race(pkg, output) -> bool:
    """Did the race detector fire for this package?

    The tolerated flake CANNOT produce this. It is a truncate-then-write on the
    filesystem -- one writer's bytes are not yet on disk when a reader opens the
    file -- and the race detector instruments memory accesses, not files. It
    surfaces as a nil-pointer panic instead, which is what the captured failing
    stream shows.

    So a DATA RACE means something the tolerated flake is not, and the most likely
    something is a memory race the submission's own port introduced: sharing a
    handler's state across goroutines is the classic way to get one while moving
    off a framework that gave each request its own context. That is a real defect
    and is never forgiven, however clean a re-run comes back -- a race that
    reproduces one run in three is still a race.

    Matched on the substring rather than the full `WARNING: DATA RACE` header so a
    `-race` report in any position or format still counts. The cost of a false
    positive is refusing to forgive one flake; the cost of a false negative is
    passing a concurrency defect, so the loose direction is the safe one.
    """
    return "DATA RACE" in "".join(output.get(pkg, []))


#: Lines worth keeping from a failing test's output.  `\.go:\d+` catches both a
#: testify assertion's file:line and a panic's stack frame; `Error Trace`,
#: `Error`, `Test` and `Messages` are testify's own labels; `Expected`/`actual`
#: catch require/assert diffs that do not carry a label.
_WHY = re.compile(r"--- FAIL|^\s*(Error|Error Trace|Test|Messages):"
                  r"|DATA RACE|Expected|actual|panic:|\.go:\d+")


def failure_detail(failures, test_output, output, failed, silent) -> str:
    """Build the `detail` a failing check carries, from the parsed event stream.

    Separate from ``main`` so it can be run against a recorded stream: what makes
    a detail block wrong is its *contents*, and the wrong version looked correct
    in the source -- ``output[pkg]`` was populated, the tail was non-empty, a
    block was produced.  It contained no assertion.

    The failing test's own lines come first.  A tail of the whole package is kept
    after them, and for a silent package it is all there is, but it cannot be the
    only thing: these tests log every request at DEBUG, so 1200 bytes from the end
    of a package that RAN is log spam by construction.  Measured on a real
    weight-4.0 failure -- "2 of the repository's own tests fail", detail ending
    ``{"L":"DEBUG",...,"M":"Adding chart to index"}``, and not one word of what
    the test expected.  ``tc.run`` tees the full stream to $SRB_WORK, which is
    discarded with the container, so a detail that omits the reason loses it.
    """
    detail = []
    if failures:
        detail.append(f"{len(failures)} failing test(s):\n  "
                      + "\n  ".join(failures[:20]))
    for entry in failures[:3]:
        pkg, _, name = entry.partition(" :: ")
        lines = test_output.get((pkg, name)) or []
        keep = [ln for ln in lines if _WHY.search(ln)]
        # Fall back to the tail of the test's own output when nothing matched,
        # which is still narrower than the package's, and never to nothing: an
        # unrecognised failure shape has to stay readable.
        body = "".join(keep or lines[-25:])[-1500:]
        if body:
            detail.append(f"--- {entry} ---\n{body}")
    for pkg in (silent or failed)[:3]:
        tail = "".join(output.get(pkg, []))[-1200:]
        if tail:
            detail.append(f"--- {pkg} (package output) ---\n{tail}")
    return "\n\n".join(detail)


def adopt_retry_output(pkg, failures, output, test_output,
                       r_failures, r_output, r_test_output) -> list:
    """Make a re-run's output the reported one for ``pkg``.

    Three updates that have to happen together, which is why they are here rather
    than spelled out at each of the call sites that needs them: the package's
    output, the per-test output the detail is built from, and the failure list --
    where the initial run's entries for this package must be dropped BEFORE the
    re-run's are added, or the report shows both and counts the same failure twice.

    ``output`` and ``test_output`` are mutated; the new failure list is returned,
    because the caller holds it by name.
    """
    output[pkg] = r_output.get(pkg, output.get(pkg, []))
    for k, v in r_test_output.items():
        test_output[k] = v
    return [f for f in failures if not f.startswith(pkg + " ::")] + list(r_failures)


def main() -> int:
    baseline_path = tc.DATA / "gotest-baseline.json"
    if not baseline_path.is_file():
        rep.record("baseline-present", False,
                   f"the measured baseline is missing: {baseline_path}")
        return rep.finish()
    baseline = json.loads(baseline_path.read_text())

    if not tc.SOURCE.is_dir():
        rep.record("source-published", False,
                   "the build module published no source tree")
        return rep.finish()

    prepare_test_environment(tc.SOURCE)
    env = tc.go_env(CGO_ENABLED="1")           # -race needs cgo

    proc = tc.run(["make", "setup-test-environment"], cwd=tc.SOURCE, env=env,
                  timeout=1800, log="setup.log")
    if not rep.record("setup-test-environment", proc.returncode == 0,
                      "`make setup-test-environment` failed, so the repository can "
                      "no longer prepare its own tests",
                      detail=tc.output_of(proc), weight=1.0):
        return rep.finish()

    proc, ids, results, failures, output, test_output = go_test(
        env, ["./..."], "go-test")
    baseline_pkgs = sorted(baseline["packages"])
    failed = sorted(p for p, r in results.items() if r == "fail")
    silent = [p for p in baseline_pkgs if p not in results]

    # Flake tolerance: re-run a failing package whole, forgive it only if it comes
    # back clean. Build errors and silent packages are never retried -- they are
    # deterministic by construction.
    forgiven: list[str] = []
    exhausted: list[str] = []
    raced: list[str] = []
    if failed and not silent and len(failed) <= RETRY_MAX_PKGS:
        still: list[str] = []
        for pkg in failed:
            # One outcome per package, decided once, dispatched once.  This was a
            # `for ... else` and the else branch was the exhaustion case, which is
            # a trap as soon as any other branch has to `break`: adding the DATA
            # RACE check below made the loop break with the package unrecorded, so
            # `still` never received it and a race would have been FORGIVEN -- the
            # exact opposite of what the check was added to do.
            outcome = None                      # 'raced' | 'forgiven' | None
            last = None
            # A package whose failure included a DATA RACE is not retried at all.
            # Not "retried and not forgiven": a race that reproduces sometimes is
            # still a race, so a clean re-run is not evidence about it, and two
            # more `-race` runs of a suite this size buy nothing for the budget.
            if reports_a_race(pkg, output):
                outcome = "raced"
                raced.append(f"{pkg} (on the initial run)")
            for attempt in range(1, RETRIES + 1) if outcome is None else ():
                tag = f"retry{attempt}-{pkg.rsplit('/', 1)[-1]}"
                # The last attempt serializes. The tolerated flake's window is a
                # function of concurrent writers, so GOMAXPROCS=1 is a different
                # experiment; re-running identically is another ticket in the same
                # draw, which is how the first graded run lost this check after
                # three attempts. Measured: the gate cleared 6 of 6 whole-suite
                # flakes on retry 1 and still failed a real run three times over.
                extra = {"GOMAXPROCS": "1"} if attempt == RETRIES else None
                r_proc, r_ids, r_results, r_failures, r_output, r_test_output = \
                    go_test(env, [pkg], tag, extra_env=extra)
                last = (r_failures, r_output, r_test_output)
                for p, s in r_ids.items():
                    ids[p].update(s)             # seen passing once means it exists
                # A re-run that reports a race is a race, whatever the initial run
                # showed -- the detector only ever fires on a real one.
                if reports_a_race(pkg, r_output):
                    outcome = "raced"
                    raced.append(f"{pkg} (on retry {attempt})")
                    failures = adopt_retry_output(pkg, failures, output, test_output,
                                                  *last)
                    break
                if r_proc.returncode == 0 and r_results.get(pkg) == "pass" \
                        and not r_failures:
                    # Drop the noisy run's per-test failures too, not just the
                    # package verdict.  `failed` is rebuilt from `still` below,
                    # but `failures` accumulates across runs and no-failing-tests
                    # reads BOTH -- so without this line the module forgives a
                    # package and then fails on the very failures it forgave,
                    # which leaves this whole block printing a note and nothing
                    # else.  Measured on State A: multitenant's TestStatefiles
                    # raced its own statefile writer, came back clean on retry 1,
                    # and own_tests still scored 0.7647 on a weight-4.0 check.
                    failures = [f for f in failures
                                if not f.startswith(pkg + " ::")]
                    forgiven.append(f"{pkg} (passed on retry {attempt}"
                                    + (", serialized)" if extra else ")"))
                    outcome = "forgiven"
                    break

            if outcome is None:
                # Every attempt failed and none of them raced: report the last
                # one's failures.  A reader has to be told how many attempts there
                # were, because a package that failed the initial run and both
                # re-runs is otherwise indistinguishable in the report from one
                # that failed once -- and that difference is exactly what separates
                # a flake from a regression.
                if last is not None:
                    failures = adopt_retry_output(pkg, failures, output,
                                                  test_output, *last)
                exhausted.append(f"{pkg} (failed the initial run and "
                                 f"{RETRIES} re-run(s), the last serialized)")
            if outcome != "forgiven":
                still.append(pkg)
        failed = still
        if forgiven:
            rep.note("flaky-packages", "passed on re-run: " + ", ".join(forgiven))
        if exhausted:
            rep.note("retries-exhausted", "; ".join(exhausted))
        if raced:
            # Never folded into the two notes above: this one says the failure was
            # not a candidate for tolerance at all, which is a different statement
            # from "it was tolerated" or "it ran out of attempts".
            rep.note("data-race-reported",
                     "the race detector fired, which the tolerated statefile flake "
                     "cannot cause -- not re-run, not forgiven: " + ", ".join(raced))

    detail = failure_detail(failures, test_output, output, failed, silent)

    # The summary names the tests.  "2 of the repository's own tests fail" is a
    # count, and a count cannot be looked into: which test failed is the first
    # thing anyone reading this needs, and it belongs where truncation cannot
    # reach it rather than in a detail block below.
    named = ", ".join(f.replace("helm.sh/chartmuseum/pkg/", "") for f in failures[:4])
    if len(failures) > 4:
        named += f", and {len(failures) - 4} more"
    rep.record("no-failing-tests", not failures and not failed,
               f"{len(failures)} of the repository's own tests fail: {named}"
               if failures else
               ("failing package(s): " + ", ".join(failed) if failed else ""),
               detail=detail, weight=4.0)
    rep.record("every-package-builds", not silent,
               "package(s) that had tests in State A produced no test result at "
               "all, which is usually a build error: " + ", ".join(silent)
               if silent else "",
               detail=detail if silent else "", weight=3.0)

    # Per package, so a submission that dropped one package's tests loses one
    # check rather than the module.
    for pkg in baseline_pkgs:
        rep.record(f"tests-survive-{pkg.rsplit('/', 1)[-1]}", bool(ids.get(pkg)),
                   f"{pkg} had tests in State A and now has none; this import path "
                   f"is public API and its tests must be ported, not deleted",
                   weight=1.0)

    total = sum(len(v) for v in ids.values())
    want = baseline["totals"]["ids"]
    rep.record("test-count-not-reduced", total >= want,
               f"the suite now reports {total} distinct test ids, down from "
               f"{want}; tests may be renamed or restructured, not dropped",
               detail="\n".join(f"  {p}: {len(v)}" for p, v in sorted(ids.items())),
               weight=2.0)

    return rep.finish({"ids": total, "baseline_ids": want,
                       "packages": len(ids), "forgiven": forgiven})


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        rep.record("own-tests-module", False,
                   f"the own_tests module raised {type(exc).__name__}: {exc}")
        rep.finish()
        raise
