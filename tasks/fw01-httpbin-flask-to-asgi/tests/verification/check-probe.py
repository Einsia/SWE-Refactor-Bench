#!/usr/bin/env python3
"""Build-time sanity check for probe.toml.

Runs inside the stage-3 image build.  ``swerefactor validate`` checks the same
things while a task is being authored; this is the backstop for the case where a
probe was edited and only the image was rebuilt.  Failing here costs a build.
Failing at run time costs six rounds of model time and produces a score that
is wrong rather than absent.

evaluation.toml is not in this build context -- it is one level up, in tests/ --
so the adversary count is compared against a number recorded here at authoring
time rather than read from the scoring policy.  ``swerefactor validate`` compares
against the real thing.
"""
from __future__ import annotations

import sys

from swerefactor import config

#: What [scoring] verification_models says in tests/evaluation.toml.
EXPECTED_ADVERSARIES = 6


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

    # An adversary with no budget is a round that pays ten points for nothing.
    for adv in probe.adversaries:
        if adv.budget_sec <= 0:
            problems.append(f"adversary {adv.id!r} has a non-positive budget")

    if problems:
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    print(f"probe: {len(probe.adversaries)} adversaries, "
          f"{probe.scope.reruns} reruns, {len(probe.scope.allow)} in scope / "
          f"{len(probe.scope.deny)} out")
    return 0


if __name__ == "__main__":
    sys.exit(main())
