"""Write a module's result file in the shape the runner reads.

One function, and it exists because of one bug.

``docs/SCHEMA.md`` states the check shape: ``{"id": ..., "verdict": "pass" |
"fail" | "skip" | "error", "weight": ...}``.  ``Check.from_dict`` reads exactly
that, and an unknown verdict reads as ``error`` rather than as a pass -- correctly,
since a producer that says nothing intelligible has not said "this passed".

Every module in this suite was written to compose checks as ``{"ok": bool}``,
which is the natural thing to write and is not the contract.  ``ok`` is not a key
the runner looks at, so every check this suite produced -- all 2,653 comparisons
and the 16 checks around them -- arrived with no verdict at all and was scored as
``error``.  Not a failure attributable to any submission: a perfect port scored
zero on all seven modules, because the number came from the absence of a key
rather than from anything either binary did.

So ``ok`` stays as the authoring key -- a boolean is the honest type for "did the
two sides agree", which is what all 2,653 of those comparisons are asserting --
and this module translates at the boundary.  Nothing else in the suite writes
``$SRB_RESULT``.

An explicit ``verdict`` wins over ``ok``.  That is what lets a check say ``skip``
-- "this does not apply to the submission in front of us" -- which no boolean can
express, and which the artifact module needs for the checks that ask a wasm
module a question a native binary has no answer to.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable, Mapping

#: The four the runner knows.  Anything else is a broken producer.
VERDICTS = ("pass", "fail", "skip", "error")


def verdict_of(check: Mapping[str, Any]) -> str:
    """The verdict a check entry is asserting.

    ``verdict`` if it says one, otherwise ``ok`` mapped to pass/fail.  A check
    that says neither is ``error``: it has not made a claim, and reading that as
    a pass is how a suite scores a submission for a key it forgot to write.
    """
    stated = check.get("verdict")
    if isinstance(stated, str) and stated.lower() in VERDICTS:
        return stated.lower()
    if "ok" in check:
        return "pass" if check["ok"] else "fail"
    return "error"


def normalise(checks: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Every check with a verdict the runner will recognise.

    ``ok`` is left in place.  It is what the module's own summary line counts and
    what a person reading the file expects to see next to the detail; dropping it
    would make the file harder to read to no purpose, since the runner ignores
    unknown keys.
    """
    out: list[dict[str, Any]] = []
    for check in checks:
        entry = dict(check)
        entry["verdict"] = verdict_of(check)
        out.append(entry)
    return out


def write(result: Mapping[str, Any], path: str | os.PathLike[str] | None = None) -> Path:
    """Write ``result`` to ``$SRB_RESULT``, with its checks normalised."""
    target = Path(path or os.environ["SRB_RESULT"])
    payload = dict(result)
    payload["checks"] = normalise(payload.get("checks") or [])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=1) + "\n")
    return target


def passed(checks: Iterable[Mapping[str, Any]]) -> int:
    """How many checks a module should report as having passed.

    A skip is not a pass and not a miss, so it is excluded from both sides of the
    figure this module *prints*.  The scorer's denominator is the wider one -- it
    keeps a skip and scores it 0 -- so a module reporting "2/2 passed, 5 skipped"
    is a module the stage rates 0.2857.  Both numbers are wanted: one says what
    could be asked, the other what the submission is paid for.
    """
    return sum(1 for c in checks if verdict_of(c) == "pass")


def scored(checks: Iterable[Mapping[str, Any]]) -> int:
    return sum(1 for c in checks if verdict_of(c) != "skip")
