#!/usr/bin/env python3
"""Build-time sanity check for probe.toml and the prompt it points at.

Runs inside the stage-3 image build.  ``swerefactor validate`` checks most of this
while a task is being authored; this is the backstop for the case where a probe was
edited and only the image was rebuilt.  Failing here costs a build.  Failing at run
time costs six rounds of model time and produces a score that is wrong rather
than absent.

evaluation.toml is not in this build context -- it is one level up, in tests/ -- so
the adversary count is compared against a number recorded here at authoring time.
``swerefactor validate`` compares against the real thing.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from swerefactor import config

ROOT = Path("/tests/verification")

#: What [scoring] verification_models says in tests/evaluation.toml.
EXPECTED_ADVERSARIES = 6

#: Placeholders render_prompt fills for a stage-3 prompt.  ``gates`` and
#: ``findings`` are in the set but render empty here, because stage 3 is built with
#: no gate list -- a prompt that leaned on them would be describing nothing.
FILLED = {"task", "original", "workspace", "submission", "roots", "commands"}
EMPTY_IN_STAGE_3 = {"gates", "findings"}


def main() -> int:
    probe = config.Probe.load(str(ROOT / "probe.toml"))
    problems: list[str] = []

    if len(probe.adversaries) != EXPECTED_ADVERSARIES:
        problems.append(
            f"probe.toml declares {len(probe.adversaries)} adversaries; the "
            f"scoring policy pays for {EXPECTED_ADVERSARIES}"
        )
    if not probe.scope.allow or not probe.scope.deny:
        # Both halves, not either: an allow list alone leaves every platform
        # consequence in scope, and a deny list alone leaves the round guessing at
        # what the product actually promised.
        problems.append(
            "probe.toml [scope] needs both allow and deny; with one of them "
            "missing a round is either unwinnable or unloseable"
        )
    if not probe.candidate_command:
        problems.append("probe.toml declares no candidate_command")

    # An adversary with no budget is a round that pays ten points for nothing.
    for adv in probe.adversaries:
        if adv.budget_sec <= 0:
            problems.append(f"adversary {adv.id!r} has a non-positive budget")
        if not (adv.metadata or {}).get("focus"):
            # Six rounds with no assignment converge on the same first idea.
            problems.append(f"adversary {adv.id!r} has no focus")

    problems += _check_prompt(ROOT / probe.prompt)
    problems += _check_helper(ROOT / "lib" / "srbsqlite.py")

    if problems:
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    focuses = len({(a.metadata or {}).get("focus") for a in probe.adversaries})
    print(f"probe: {len(probe.adversaries)} adversaries ({focuses} distinct foci), "
          f"{probe.scope.reruns} reruns, {len(probe.scope.allow)} in scope / "
          f"{len(probe.scope.deny)} out")
    return 0


def _check_prompt(path: Path) -> list[str]:
    """The prompt has to be readable, and its placeholders have to be real ones.

    Unknown placeholders are left in place by render_prompt rather than erased, so a
    typo does not vanish -- it arrives in front of the model as literal braces.
    Cheaper to catch here.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"the verification prompt is not readable at {path}: {exc}"]
    problems = []
    used = set(re.findall(r"\{\{([a-z_]+)\}\}", text))
    unknown = used - FILLED - EMPTY_IN_STAGE_3
    if unknown:
        problems.append(f"the prompt uses placeholders nothing fills: {sorted(unknown)}")
    hollow = used & EMPTY_IN_STAGE_3
    if hollow:
        problems.append(
            f"the prompt uses {sorted(hollow)}, which render empty in stage 3"
        )
    for wanted in ("{{original}}", "{{submission}}"):
        if wanted not in text:
            problems.append(f"the prompt never tells the round where {wanted} is")
    return problems


def _check_helper(path: Path) -> list[str]:
    """Every name the prompt advertises has to exist in the helper.

    A prompt promising a function that was renamed costs a round its first three
    candidates, all of them failing with AttributeError on both trees -- which
    reads, in the transcript, as an adversary that could not write Python.
    """
    try:
        source = path.read_text(encoding="utf-8")
        prompt = (ROOT / "prompt.txt").read_text(encoding="utf-8")
    except OSError as exc:
        return [f"cannot cross-check the helper against the prompt: {exc}"]
    names = set(re.findall(r"\bsq\.([A-Za-z_][A-Za-z_0-9]*)", prompt))
    names |= set(re.findall(r"\bSandbox\.([a-z_][a-z_0-9]*)", prompt))
    problems = []
    for name in sorted(names):
        if not re.search(rf"^\s*(?:def {name}\(|{name} = |{name}: )", source, re.M):
            problems.append(f"the prompt advertises {name!r}, which the helper "
                            f"does not define")
    return problems


if __name__ == "__main__":
    sys.exit(main())
