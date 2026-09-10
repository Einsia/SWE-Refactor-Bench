"""The body of every differential module.  One entry point, five callers.

A module's ``run.sh`` sets nothing and decides nothing: it execs this file, which
reads ``$SRB_MODULE_ID`` to learn which module it is.  The five case modules are
therefore identical apart from their id, which is the point -- five copies of this
logic would be five places for the comparison to differ, and a difference in how
``engine`` compares versus how ``storage`` compares is exactly the kind of bug that
looks like a failing port.

Result files, not pytest.  The behavioural runner accepts a bare
``{"checks": [...]}`` object and that is all this needs: 2,653 comparisons of two
byte strings do not want fixtures, collection or parametrize ids, and a pytest
node id per case would be less readable than the case key already is.  The scan
suite in stage 1 does use pytest, because there the checks are heterogeneous and
each one wants its own function.
"""

from __future__ import annotations

import os
import sys
import time
import traceback

import builds
import catalog
import differential
import report
from case import CaseError


def fail(summary: str, *, detail: str = "") -> int:
    """Write an errored result and stop.

    ``status: "error"`` rather than a list of failing checks, because these are
    the paths where the module could not measure the submission at all.  Reporting
    a zero for the cases instead would say "the port fails 1,616 comparisons",
    which would be a claim this run has no evidence for.
    """
    result = {
        "status": "error",
        "summary": summary,
        "checks": [],
        "metadata": {"module": os.environ.get("SRB_MODULE_ID", "?")},
    }
    if detail:
        result["notes"] = [detail]
    _write(result)
    print(f"FATAL: {summary}", file=sys.stderr)
    if detail:
        print(detail, file=sys.stderr)
    return 2


def _write(result: dict[str, object]) -> None:
    """Publish, through report.write, which supplies each check's verdict.

    Guarded because this is also the path ``fail`` takes: a module that cannot
    write its result should say so on stderr rather than raise on the way out of
    reporting why it failed.
    """
    try:
        report.write(result)
    except (KeyError, OSError) as exc:
        print(f"FATAL: the result could not be written ({exc}); "
              f"nothing can be reported", file=sys.stderr)


def main() -> int:
    module = os.environ.get("SRB_MODULE_ID", "")
    if module not in catalog.BUILDERS:
        return fail(
            f"SRB_MODULE_ID={module!r} is not a case module; "
            f"known: {', '.join(sorted(catalog.BUILDERS))}"
        )

    try:
        cases = catalog.build(module)
    except CaseError as exc:
        # The case set is generated from this suite's own code, so this is a bug
        # in the suite rather than anything about the submission.  Erroring is the
        # only honest outcome: a partial case set would score a submission
        # against a surface nobody declared.
        return fail(f"the {module} case set is invalid: {exc}")

    try:
        ledger = builds.Ledger.load()
    except builds.LedgerError as exc:
        return fail(str(exc))

    started = time.time()
    try:
        results = differential.compare(
            cases, ledger, workers=differential.workers_from_env()
        )
    except Exception as exc:  # noqa: BLE001 - reported, never swallowed
        return fail(
            f"the {module} comparison could not be completed: "
            f"{type(exc).__name__}: {exc}",
            detail=traceback.format_exc(limit=8),
        )

    checks = differential.checks_for(module, results)
    summary = differential.summarise(results)
    summary["seconds"] = round(time.time() - started, 1)
    summary["reference"] = ledger.reference_provenance.get("source_id", "?")

    _write({
        "checks": checks,
        "metadata": {"module": module, **summary},
    })

    passed = summary["passed"]
    total = summary["cases"]
    print(f"{module}: {passed}/{total} cases agree with the reference "
          f"in {summary['seconds']}s")
    # Non-zero when anything failed, which the runner treats as advisory.  It is
    # here so that running a module by hand behaves like a test command.
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
