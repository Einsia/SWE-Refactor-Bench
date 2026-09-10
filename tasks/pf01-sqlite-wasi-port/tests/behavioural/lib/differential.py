"""Run one module's cases on both builds and compare what came out.

This is the only file in the suite that decides whether a case passed, and it
decides it by comparing two processes' output.  It never reads the submission's
source, never looks for a symbol, a macro or a filename, and has no opinion about
how the port was written.  A submission that reimplemented the VFS from scratch, a
submission that wrapped wasi-libc, and a submission that did something nobody has
thought of yet are all scored the same way: run it, run the reference, diff.

What a check reports
--------------------
One check per case, named by the case key, weighted 1.  A case asserting three
things (stdout, stderr, exit) is still one check: the case is the unit of
behaviour, and splitting it would make a case that fails all three cost three
times a case that fails one.

The report carries both sides' output on failure, truncated.  That matters more
than it looks: "cli.mode.column.3 failed" is unactionable, and a diff of the two
renderings is usually enough to see the bug without re-running anything.

The denominator
---------------
``catalog.EXPECTED`` decides how many checks a module must produce, and the module
emits a check asserting it produced exactly that many.  So a module that crashes
halfway through does not score 100% of the cases it got to -- it fails its own
count check, and a scored check that did not pass closes the stage, which is the
honest reading of "we do not know how this submission behaves".
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Iterable

import builds
import catalog
import execute
from case import Case

#: How much of each side's output a failing check reports.
EXCERPT = 1400


@dataclass
class CaseResult:
    key: str
    ok: bool
    operation: str
    detail: str = ""


def _excerpt(value: Any) -> str:
    text = value if isinstance(value, str) else repr(value)
    if len(text) <= EXCERPT:
        return text
    return text[:EXCERPT] + f"\n... [{len(text) - EXCERPT} more characters]"


def _diff(case: Case, want: dict[str, Any], got: dict[str, Any]) -> str:
    """Name every part of the observation that disagreed, and show it."""
    lines: list[str] = []
    for field in case.checks:
        a, b = want.get(field), got.get(field)
        if a == b:
            continue
        if field == "files":
            a = a or {}
            b = b or {}
            for name in sorted(set(a) | set(b)):
                if a.get(name) != b.get(name):
                    lines.append(
                        f"  files[{name}]: reference={a.get(name)!r} "
                        f"submission={b.get(name)!r}"
                    )
            continue
        lines.append(f"  {field}: reference={_excerpt(a)!r}")
        lines.append(f"  {field}: submission={_excerpt(b)!r}")
    if not lines:  # pragma: no cover - only reachable if want == got
        return "no difference (the comparison should not have failed)"
    return "\n".join(lines)


def _literal_of(case: Case) -> dict[str, Any]:
    """A platform case's written-down expectation, in ``select`` shape."""
    literal = dict(case.literal or {})
    want: dict[str, Any] = {}
    for field in case.checks:
        key = "exit" if field == "exit" else field
        if key not in literal:
            raise KeyError(
                f"{case.key}: the expectation names no {key!r}, but the case "
                f"asserts it"
            )
        want[key] = literal[key]
    return want


def compare(cases: Iterable[Case], ledger: builds.Ledger,
            workers: int | None = None) -> list[CaseResult]:
    """Score every case in ``cases``.

    The reference runs first and only for the cases that need it -- a platform
    case's expectation is written down precisely because the reference's answer to
    it is the wrong answer, so asking would be a waste at best and a source of
    confusion at worst.
    """
    cases = list(cases)
    reference, submission = ledger.targets()

    # A platform case's literal is the *WASI* answer, written down because native
    # cannot produce it.  Against a pre-migration submission that reasoning runs
    # the other way: there is no WASI here, the literal is the wrong expectation
    # by construction, and the reference can answer the case by being run -- which
    # is the stronger instrument anyway.  So on the native path every case is
    # differential, and the 78 platform cases measure what they can still measure,
    # that the tree behaves like the reference on the surface they probe.
    differential_only = ledger.is_native
    needs_reference = [c for c in cases if differential_only or c.differential]
    expectations: dict[str, dict[str, Any]] = {}
    if needs_reference:
        observed = execute.run_all(
            needs_reference, reference, native_oracle=ledger.oracle, workers=workers
        )
        for case in needs_reference:
            expectations[case.key] = observed[case.key].select(case.checks)

    actual = execute.run_all(
        cases, submission, native_oracle=ledger.oracle, workers=workers
    )

    results: list[CaseResult] = []
    for case in cases:
        got = actual[case.key].select(case.checks)
        if differential_only or case.differential:
            want = expectations[case.key]
            source = "reference"
        else:
            want = _literal_of(case)
            source = "the WASI contract"
        ok = want == got
        detail = "" if ok else f"disagrees with {source}:\n{_diff(case, want, got)}"
        results.append(CaseResult(case.key, ok, case.operation, detail))
    return results


def checks_for(module: str, results: list[CaseResult]) -> list[dict[str, Any]]:
    """Turn case results into the module's check list, count check included."""
    checks: list[dict[str, Any]] = []
    expected = catalog.EXPECTED[module]

    # Required, and first, so a reader of the result file sees the denominator
    # before the individual outcomes.  This is what stops a module that died
    # early from scoring well on the part it reached.
    checks.append({
        "id": "case-count",
        "title": f"the {module} module ran all {expected} of its cases",
        "ok": len(results) == expected,
        "required": True,
        "weight": 1,
        "detail": (
            f"ran {len(results)}, expected {expected}"
            + ("" if len(results) == expected else
               "; a short run means cases were lost, and the ones that did run "
               "are not a measurement of the whole surface")
        ),
    })

    for r in results:
        checks.append({
            "id": r.key,
            "title": r.operation,
            "ok": r.ok,
            "weight": 1,
            "detail": r.detail,
        })
    return checks


def summarise(results: list[CaseResult]) -> dict[str, Any]:
    """Per-operation pass counts, for the report.  Not scored."""
    by_op: dict[str, list[int]] = {}
    for r in results:
        slot = by_op.setdefault(r.operation, [0, 0])
        slot[1] += 1
        if r.ok:
            slot[0] += 1
    failing = sorted(op for op, (ok, total) in by_op.items() if ok < total)
    return {
        "cases": len(results),
        "passed": sum(1 for r in results if r.ok),
        "operations": len(by_op),
        "operations_with_a_failure": len(failing),
        # Named, because "37 operations failed" sends someone hunting and
        # "these 37 operations failed" does not.
        "failing_operations": failing[:60],
    }


def workers_from_env() -> int:
    """How many cases to run at once.

    Capped at 8 regardless of the machine.  wasmtime is not the expensive part --
    process startup is -- and past eight the runs start contending for the same
    page cache and the timings get noisy enough that a case with a 60s budget can
    fail for load rather than for behaviour.
    """
    try:
        want = int(os.environ.get("PF02_WORKERS", "0"))
    except ValueError:
        want = 0
    if want > 0:
        return want
    return max(1, min(8, os.cpu_count() or 4))
