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

    # Specific to this task: the deny list has to exclude the things stage 2's
    # normalizer masks, or a candidate can win ten points off a per-run value
    # that neither tree controls.  Checked by keyword rather than by importing
    # the normalizer, which lives in stage 2's build context and is not here.
    must_deny = {
        "date": "the Date header",
        "x-request-id": "the per-request identifier's value",
        "casing": "header name casing",
        "timestamp": "timestamp values",
        "chunked": "chunked vs Content-Length",
    }
    deny_text = " ".join(probe.scope.deny).lower()
    for needle, what in must_deny.items():
        if needle not in deny_text:
            problems.append(
                f"[scope] deny does not mention {needle!r}: {what} is masked by "
                f"the behavioural stage, so a candidate could win a round on it "
                f"while proving nothing"
            )

    # And the reverse: the one thing this task most needs a candidate to be
    # allowed to attack.  The route-template label is the observable Gin used to
    # supply for free, so a scope that forgot to permit it would exclude the
    # most productive round in the probe.
    allow_text = " ".join(probe.scope.allow).lower()
    if "metric" not in allow_text:
        problems.append(
            "[scope] allow does not mention the metrics exposition, which is "
            "where the retired framework's route template used to come from"
        )

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
