#!/usr/bin/env python3
"""Build-time sanity check for probe.toml and prompt.txt.

Runs inside the stage-3 image build.  ``swerefactor validate`` checks the
structural half of this while a task is being authored; this is the backstop for
the case where a probe was edited and only the image was rebuilt.  Failing here
costs a build.  Failing at run time costs six rounds of model time and produces
a score that is wrong rather than absent.

evaluation.toml is not in this build context -- it is one level up, in tests/ -- so
the adversary count is compared against a number recorded here at authoring time
rather than read from the scoring policy.  ``swerefactor validate`` compares against
the real thing.

Two of the checks here are specific to this task rather than generic.

The deny list's first clause is the one that matters on a byte-order port: the
original is wrong on the big-endian target, so "the submission must agree with the
original on s390x" and "these two targets must disagree" both pass on the original
and fail on every correct submission.  They satisfy every mechanical condition the
adjudicator can test, which means the deny list is the only thing standing between
this stage and a sound submission losing points for being sound.  A probe edit that
dropped it would still validate, still build, and still run six rounds.

And the prompt must actually say so.  The scope block is appended to the prompt at
run time, so a deny clause reaches the adversary either way -- but as one bullet in
a list of fourteen, at the end.  A model that has already spent forty minutes
building the wrong kind of candidate has lost the round regardless of whether the
rule was technically present.  So the prompt is required to carry the warning in
its own body, and both halves are checked here rather than trusted.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from swerefactor import config

#: What [scoring] verification_models says in tests/evaluation.toml.
EXPECTED_ADVERSARIES = 6

#: The three targets run-candidate.sh builds, which is what the prompt documents
#: and what srbqjs exposes.  A probe pointed at a different set would leave the
#: prompt describing binaries no candidate can reach.
EXPECTED_TARGETS = ("x86_64", "s390x", "armhf")

ROOT = Path("/tests/verification")


def _deny_covers_the_leak(deny: list[str]) -> bool:
    """Does some deny clause rule out asserting the original's big-endian answers?

    Matched on meaning rather than wording: a clause must mention the big-endian
    target and either the idea of reproducing the original's answers there or the
    idea of requiring two targets to differ.  Not a spelling test -- a rewrite of
    the clause passes as long as it still says the thing.
    """
    for clause in deny:
        low = clause.lower()
        if "s390x" not in low and "big-endian" not in low:
            continue
        if "disagree" in low or "differ" in low:
            return True
        if "reproduc" in low and "original" in low:
            return True
        if "equals what the original" in low or "original's own" in low:
            return True
    return False


def main() -> int:
    probe = config.Probe.load(ROOT / "probe.toml")
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

    if not _deny_covers_the_leak(list(probe.scope.deny)):
        problems.append(
            "no [scope] deny clause rules out a candidate asserting the "
            "original's big-endian answers, or requiring two targets to "
            "disagree. Both pass on the original and fail on every correct "
            "submission, so nothing mechanical stops them and a sound "
            "submission would lose points for being sound"
        )

    # An adversary with no budget is a round that pays ten points for nothing.
    # A duplicated focus is two rounds covering one part of the surface, which
    # config.Probe does not check because focus is a task-level convention.
    seen_focus: dict[str, str] = {}
    for adv in probe.adversaries:
        if adv.budget_sec <= 0:
            problems.append(f"adversary {adv.id!r} has a non-positive budget")
        focus = str((adv.metadata or {}).get("focus") or "").strip()
        if not focus:
            problems.append(
                f"adversary {adv.id!r} has no focus, so this round is "
                f"unassigned and will converge on whatever the others already "
                f"cover"
            )
        else:
            first = focus.splitlines()[0][:60]
            if first in seen_focus:
                problems.append(
                    f"adversaries {seen_focus[first]!r} and {adv.id!r} open with "
                    f"the same focus, so two rounds cover one part of the surface"
                )
            seen_focus[first] = adv.id

    prompt_path = ROOT / probe.prompt
    if not prompt_path.exists():
        problems.append(f"probe.toml names prompt {probe.prompt!r}, which is "
                        f"not in the image")
    else:
        text = prompt_path.read_text(encoding="utf-8")
        low = text.lower()

        # The placeholders audit.render_prompt fills.  An unknown one is left
        # in place rather than erased, so a typo reaches the adversary as literal
        # braces -- visible, but only after the round has been paid for.
        known = {"task", "original", "workspace", "submission", "gates",
                 "roots", "commands", "findings"}
        unknown = sorted(set(re.findall(r"\{\{(\w+)\}\}", text)) - known)
        if unknown:
            problems.append(
                f"prompt.txt uses placeholder(s) nothing fills: "
                f"{', '.join(unknown)}. They reach the adversary as literal text"
            )
        for needed in ("{{original}}", "{{submission}}"):
            if needed not in text:
                problems.append(f"prompt.txt never names {needed}, so the "
                                f"adversary is not told where the trees are")

        for target in EXPECTED_TARGETS:
            if target not in text:
                problems.append(
                    f"prompt.txt does not mention the {target} target, which "
                    f"run-candidate.sh builds and srbqjs exposes"
                )

        # The warning, in the prompt's own body.  Checked on meaning: it has to
        # say the original is wrong on the big-endian target, and it has to say
        # the reference is not available there.
        wrong_here = ("s390x" in low or "big-endian" in low) and (
            "is wrong" in low or "the original is wrong" in low)
        if not wrong_here:
            problems.append(
                "prompt.txt does not tell the adversary that the original is "
                "wrong on the big-endian target. That is the one fact that "
                "makes the obvious candidate shape a wasted round"
            )
        if "no target argument" not in low:
            problems.append(
                "prompt.txt does not say reference() takes no target argument, "
                "so an adversary will look for the argument, not find it, and "
                "guess at why"
            )

    if problems:
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    print(f"probe: {len(probe.adversaries)} adversaries, "
          f"{probe.scope.reruns} reruns, {len(probe.scope.allow)} in scope / "
          f"{len(probe.scope.deny)} out; the big-endian reference claim is "
          f"denied and the prompt says why")
    return 0


if __name__ == "__main__":
    sys.exit(main())
