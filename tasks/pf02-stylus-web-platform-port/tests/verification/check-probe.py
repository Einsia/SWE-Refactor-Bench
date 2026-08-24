#!/usr/bin/env python3
"""Build-time sanity check for probe.toml.

Runs inside the stage-3 image build.  ``swerefactor validate`` checks the same
things while a task is being authored; this is the backstop for the case where a
probe was edited and only the image was rebuilt.  Failing here costs a build.
Failing at run time costs six rounds of model time and produces a score that is
wrong rather than absent.

evaluation.toml is not in this build context -- it is one level up, in tests/ --
so the adversary count is compared against a number recorded here at authoring
time rather than read from the scoring policy.  ``swerefactor validate`` compares
against the real thing.
"""
from __future__ import annotations

import sys
from pathlib import Path

from swerefactor import config

#: What [scoring] verification_models says in tests/evaluation.toml.
EXPECTED_ADVERSARIES = 6

#: Deny clauses this task cannot run without, keyed by a phrase that must appear
#: somewhere in the deny list.  Each one closes a break that is available on
#: every correct submission, for free, in all six rounds:
#:
#:   the identity of the tree  -- reading the role instead of asking the compiler
#:   arguments                 -- sloppy-mode function properties, CJS-only
#:   file layout inside        -- §2.1 moves the built-ins, so paths must differ
#:   uncaught-exception        -- CLI stderr names the staged directory
#:
#: The first three were established by measurement.  Left out, each is worth the
#: whole 60 points to a candidate that notices, against a submission that did
#: exactly what the instruction asked.
REQUIRED_DENY_PHRASES = [
    "the identity of the tree",
    "`arguments` and `caller`",
    "file layout inside the tree",
    "uncaught-exception",
]


def main() -> int:
    probe = config.Probe.load("/tests/verification/probe.toml")
    problems: list[str] = []

    if len(probe.adversaries) != EXPECTED_ADVERSARIES:
        problems.append(
            f"probe.toml declares {len(probe.adversaries)} adversaries; the "
            f"scoring policy pays for {EXPECTED_ADVERSARIES}"
        )
    if not probe.scope.allow and not probe.scope.deny:
        problems.append(
            "probe.toml [scope] states neither allow nor deny, so every "
            "divergence counts and no submission can survive a round"
        )
    if not probe.candidate_command:
        problems.append("probe.toml declares no candidate_command")

    deny_text = "\n".join(probe.scope.deny).lower()
    for phrase in REQUIRED_DENY_PHRASES:
        if phrase.lower() not in deny_text:
            problems.append(
                f"probe.toml [scope] deny does not mention {phrase!r}.  That is "
                "a difference every correct submission has, so leaving it in "
                "scope pays a candidate 60 points for finding the instruction."
            )

    # An adversary with no budget is a round that pays ten points for nothing.
    for adv in probe.adversaries:
        if adv.budget_sec <= 0:
            problems.append(f"adversary {adv.id!r} has a non-positive budget")

    problems.extend(check_fault_plugin(Path(__file__).resolve().parent))

    if problems:
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    print(f"probe: {len(probe.adversaries)} adversaries, "
          f"{probe.scope.reruns} reruns, {len(probe.scope.allow)} in scope / "
          f"{len(probe.scope.deny)} out")
    return 0


def check_fault_plugin(root: Path) -> list[str]:
    """lib/srbfault.py is this task's "the tree could not be tested" defence.

    Checked here because every way it can break is silent.  Its table is keyed by
    (module, class): rename the lib, rename BuildFailed or DriverFailure, or drop
    `-p srbfault` from run-candidate.sh, and the plugin simply never fires.
    Nothing errors -- an uncaught fault goes back to being pytest's exit 1, the
    adjudicator reads "fails on the submission", and six rounds report a defect in
    a tree that never installed.  Failing here costs a build; failing at run time
    costs a report that is wrong rather than absent.
    """
    problems: list[str] = []
    # No .pyc for the imports below.  Run from a checkout rather than from the
    # image, this would otherwise leave a __pycache__/ in the stage directory, and
    # every stage Dockerfile ends in `COPY . /tests/<stage>` -- which is exactly the
    # thing `swerefactor validate` rejects a stage directory for.
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(root / "lib"))
    try:
        import srbfault
    except Exception as exc:                        # noqa: BLE001
        return [f"lib/srbfault.py does not import ({exc!r}), so the `-p srbfault`"
                f" in run-candidate.sh would fail every candidate run"]

    from swerefactor.verification import FAULT_EXIT_CODES

    if not srbfault.FAULTS:
        problems.append("srbfault.FAULTS is empty, so no exception is a fault")
    for (mod_name, cls_name), code in sorted(srbfault.FAULTS.items()):
        try:
            mod = __import__(mod_name)
        except Exception as exc:                    # noqa: BLE001
            problems.append(
                f"srbfault.FAULTS names module {mod_name!r}, which does not "
                f"import ({exc!r}); the plugin would never fire")
            continue
        cls = getattr(mod, cls_name, None)
        if cls is None:
            problems.append(
                f"srbfault.FAULTS names {mod_name}.{cls_name}, which does not "
                f"exist; the plugin would never fire")
        elif not (isinstance(cls, type) and issubclass(cls, BaseException)):
            problems.append(f"{mod_name}.{cls_name} is not an exception class")
        if code not in FAULT_EXIT_CODES:
            problems.append(
                f"srbfault maps {mod_name}.{cls_name} to exit {code}, which is "
                f"outside FAULT_EXIT_CODES ({min(FAULT_EXIT_CODES)}-"
                f"{max(FAULT_EXIT_CODES)}); the adjudicator would read it as a "
                f"verdict about behaviour rather than as a fault")

    runner = root / "run-candidate.sh"
    if runner.is_file() and "-p srbfault" not in runner.read_text(encoding="utf-8"):
        problems.append(
            "run-candidate.sh does not register the plugin with `-p srbfault`, "
            "so a fault would reach the adjudicator as pytest's exit 1")
    return problems


if __name__ == "__main__":
    sys.exit(main())
