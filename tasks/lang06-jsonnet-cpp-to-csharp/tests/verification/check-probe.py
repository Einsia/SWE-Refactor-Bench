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

The last check is this task's own.  Stage 2 excludes two behaviours *as checked
rules* because the reference cannot do them either -- jsonnetfmt
--debug-desugaring's output, and the two parsers on a number that overflows a
double.  If stage 3's deny list does not carry them, an adversary can win a round
on the one surface the benchmark decided not to grade, and the submission loses
ten points for being no worse than the C++.  The deny list is prose, so what is
checked is that the phrases naming each exclusion are present -- a weak check on a
strong invariant, which is why the phrases are quoted from spec.py rather than
paraphrased.
"""
from __future__ import annotations

import sys

from swerefactor import config

#: What [scoring] verification_models says in tests/evaluation.toml.
EXPECTED_ADVERSARIES = 6

#: What [scoring] reruns the adjudicator needs; probe.toml states it, and a probe
#: that dropped to 1 would let a flake cost a submission ten points.
MIN_RERUNS = 3

#: The surfaces tests/behavioural/lib/spec.py excludes, and the substring that has
#: to appear somewhere in [scope].deny for each.  Lowercased before comparing.
STAGE2_EXCLUSIONS = {
    "jsonnetfmt --debug-desugaring output "
    "(spec.ASSERTED_REFERENCE_BUGS)": "--debug-desugaring",
    "std.parseJson/std.parseYaml number overflow "
    "(spec.PARSER_NUMBER_OVERFLOW)": "overflows a double",
}


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
    if probe.scope.reruns < MIN_RERUNS:
        problems.append(
            f"probe.toml asks for {probe.scope.reruns} rerun(s); a candidate "
            f"upheld on fewer than {MIN_RERUNS} can be a flake worth 10 points"
        )
    if probe.scope.network_allowed:
        problems.append(
            "probe.toml allows the network to a candidate; neither program "
            "opens a socket, so a candidate that needs one is not testing this "
            "product"
        )

    # An adversary with no budget is a round that pays ten points for nothing.
    seen: set[str] = set()
    for adv in probe.adversaries:
        if adv.budget_sec <= 0:
            problems.append(f"adversary {adv.id!r} has a non-positive budget")
        if adv.id in seen:
            problems.append(f"two adversaries share the id {adv.id!r}, so their "
                            f"rounds cannot be told apart in the report")
        seen.add(adv.id)

    deny_text = "\n".join(probe.scope.deny).lower()
    for surface, phrase in STAGE2_EXCLUSIONS.items():
        if phrase.lower() not in deny_text:
            problems.append(
                f"[scope].deny says nothing matching {phrase!r}, so nothing "
                f"stops a round being won on {surface} -- which stage 2 excludes "
                f"because the reference fails it too"
            )

    if problems:
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    print(f"probe: {len(probe.adversaries)} adversaries, "
          f"{probe.scope.reruns} reruns, {len(probe.scope.allow)} in scope / "
          f"{len(probe.scope.deny)} out, "
          f"{len(STAGE2_EXCLUSIONS)} stage-2 exclusion(s) carried")
    return 0


if __name__ == "__main__":
    sys.exit(main())
