#!/usr/bin/env python3
"""Build-time sanity check for probe.toml and the assets beside it.

Runs inside the stage-3 image build.  ``swerefactor validate`` checks probe.toml
while a task is being authored; this is the backstop for the case where a probe
was edited and only the image was rebuilt.  Failing here costs a build.  Failing
at run time costs six rounds of model time and produces a score that is wrong
rather than absent.

evaluation.toml is not in this build context -- it is one level up, in tests/ --
so the adversary count is compared against a number recorded here at authoring
time rather than read from the scoring policy.  ``swerefactor validate`` compares
against the real thing.

The second half checks what makes this stage's image different from stage 2's:
the derived op table, and the corpus that must NOT be here.  A candidate handed
the frozen documents could re-find a case stage 2 already scored, charging the
submission twice for one defect -- so their absence is an assertion, not a
property of how the Dockerfile happens to be written today.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from swerefactor import config

#: What [scoring] verification_models says in tests/evaluation.toml.
EXPECTED_ADVERSARIES = 6

#: The four tiers a submission builds.  Every op must land in one of them, and a
#: fifth name would mean run-candidate.sh has a tier it never starts.
TIERS = ("core", "model", "keywords", "parts")

#: Everything stage 2 uses to grade that must not be reachable from here.  The
#: expectations are the original's own answers, so a candidate holding them could
#: answer "am I on the original?" without asking the artifact anything.
FORBIDDEN = (
    "/opt/probe-assets/catalog.json",
    "/opt/probe-assets/documents.json",
    "/opt/probe-assets/expectations.bin",
    "/opt/probe-assets/docs",
)


def check_probe(problems: list[str]) -> config.Probe:
    probe = config.Probe.load("/tests/verification/probe.toml")

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
        if not adv.metadata.get("focus"):
            # Six unguided rounds converge on the same three statements.
            problems.append(f"adversary {adv.id!r} has no focus")

    return probe


def check_ops(problems: list[str]) -> dict:
    path = Path("/opt/probe-assets/ops.json")
    if not path.exists():
        problems.append("no ops.json: a candidate has no operation table")
        return {}
    table = json.loads(path.read_text(encoding="utf-8"))
    if not table:
        problems.append("ops.json is empty")
    for name, entry in sorted(table.items()):
        if entry.get("tier") not in TIERS:
            problems.append(
                f"op {name!r} is in tier {entry.get('tier')!r}, which "
                f"run-candidate.sh never starts")
        if not entry.get("about"):
            problems.append(f"op {name!r} has no description")
        for tag in entry.get("args", []):
            if tag not in ("d", "e", "s", "n", "i", "b"):
                problems.append(f"op {name!r} declares unknown arg tag {tag!r}")
    return table


def check_absences(problems: list[str]) -> None:
    for path in FORBIDDEN:
        if Path(path).exists():
            problems.append(
                f"{path} is in the image; a candidate holding stage 2's corpus "
                f"can re-find a case stage 2 already charged for")


def check_spec(problems: list[str]) -> None:
    path = Path("/opt/probe-assets/spec.json")
    if not path.exists():
        problems.append("no spec.json: `fmt`, `filter`, `stack` and `validate` "
                        "resolve their arguments in it and would all defect")
        return
    spec = json.loads(path.read_text(encoding="utf-8"))
    for key in ("format_presets", "validate_cases", "filters", "stacks",
                "ttype_names", "encoding_aliases", "accessor_scope",
                "node_kinds", "lexstate_scripts"):
        if not spec.get(key):
            problems.append(f"spec.json has no {key}")


def main() -> int:
    problems: list[str] = []
    probe = check_probe(problems)
    table = check_ops(problems)
    check_spec(problems)
    check_absences(problems)

    if problems:
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    by_tier: dict[str, int] = {}
    for entry in table.values():
        by_tier[entry["tier"]] = by_tier.get(entry["tier"], 0) + 1
    shown = " ".join(f"{t}={by_tier.get(t, 0)}" for t in TIERS)
    print(f"probe: {len(probe.adversaries)} adversaries, "
          f"{probe.scope.reruns} reruns, {len(probe.scope.allow)} in scope / "
          f"{len(probe.scope.deny)} out")
    print(f"ops: {len(table)} over {len(by_tier)} tiers ({shown})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
