#!/usr/bin/env python3
"""Build-time sanity check for probe.toml and the driver it names.

Runs inside the stage-3 image build.  ``swerefactor validate`` checks some of this
while a task is being authored; this is the backstop for the case where a probe or
a driver was edited and only the image was rebuilt.  Failing here costs a build.
Failing at run time costs six rounds of model time and produces a score that is
wrong rather than absent -- and wrong in the generous direction, because a stage 3
in which no candidate can be executed records six survivals and pays out all 30
points.  That asymmetry is why every check here is worth a build failure.

evaluation.toml is not in this build context -- it is one level up, in tests/ -- so
the adversary count is compared against a number recorded here at authoring time
rather than read from the scoring policy.  ``swerefactor validate`` compares against
the real thing.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from swerefactor import config

ROOT = Path("/tests/verification")

#: What [scoring] verification_models says in tests/evaluation.toml.
EXPECTED_ADVERSARIES = 6

#: The two graph profiles stage 3 boots per tree, and the config file each is
#: started from.  Checked against lib/stage3.py's own table rather than trusted,
#: because the distinction between them is load-bearing: /route-pt, /isochrone-pt
#: and /pt-mvt are registered only when the configuration names a GTFS file, three
#: of the six rounds are pointed at exactly that, and two servers accidentally
#: booted from the same config would make those rounds attack a distinction that is
#: not there.  They would then report nothing found, which scores identically to
#: three honestly survived rounds.
EXPECTED_PROFILES = {"base": "config-base.yml", "pt": "config-pt.yml"}

#: Exit codes run-candidate.sh translates into an SRB-INFRA-FAULT line, and which
#: score.py reads out of $SRB_WORK/faults.  A driver that stopped emitting one of
#: these would turn an infrastructure failure into a silent "the candidate failed",
#: which on the original side is the generous direction again.
FAULT_CODES = {71, 74, 70}


def problems() -> list[str]:
    out: list[str] = []
    probe = config.Probe.load(str(ROOT / "probe.toml"))

    if len(probe.adversaries) != EXPECTED_ADVERSARIES:
        out.append(
            f"probe.toml declares {len(probe.adversaries)} adversaries; the "
            f"scoring policy pays for {EXPECTED_ADVERSARIES}")
    if probe.task != "fw07-graphhopper-dropwizard-to-springboot":
        out.append(f"probe.toml names task {probe.task!r}")
    if not probe.candidate_command:
        out.append("probe.toml declares no candidate_command")
    if not probe.scope.allow or not probe.scope.deny:
        out.append(
            "probe.toml [scope] must state both allow and deny: with no deny "
            "every framework difference counts and no submission can survive a "
            "round, and with no allow the adjudicator has nothing to uphold a "
            "genuine finding against")
    if probe.scope.reruns < 2:
        out.append(
            f"scope.reruns is {probe.scope.reruns}; one run cannot tell a "
            f"divergence from a flake, and this application answers a `took` and "
            f"a `Date` on every request")
    if probe.scope.network_allowed:
        out.append("scope.network_allowed is true; the grading container has none")

    for adv in probe.adversaries:
        if adv.budget_sec <= 0:
            out.append(f"adversary {adv.id!r} has a non-positive budget")
        if not str((adv.metadata or {}).get("focus") or "").strip():
            out.append(
                f"adversary {adv.id!r} has no focus; six unguided rounds converge "
                f"on the same two or three requests and the stage stops covering "
                f"the surface")

    # The round that pays for both Maven builds and all four graph imports needs a
    # budget the others do not, because tool time is charged to the round that
    # spends it.  Whichever round runs first pays; the engine runs them in the
    # order they are declared, so it is the first declared one that must be large.
    first = probe.adversaries[0] if probe.adversaries else None
    if first is not None:
        rest = [a.budget_sec for a in probe.adversaries[1:]]
        if rest and first.budget_sec <= max(rest):
            out.append(
                f"the first adversary ({first.id!r}, {first.budget_sec:g}s) has no "
                f"more budget than the later rounds ({max(rest):g}s), but it pays "
                f"for both trees' builds and all four cold graph imports out of "
                f"its own wall clock and would spend most of it compiling")

    # The candidate_command must be the script that is actually in the image.
    runner = probe.candidate_command[-1] if probe.candidate_command else ""
    if not runner.startswith("/tests/verification/") or not Path(runner).exists():
        out.append(f"candidate_command names {runner!r}, which is not in this image")

    # The profile table, read out of the driver rather than duplicated here.
    driver = (ROOT / "lib" / "stage3.py").read_text()
    m = re.search(r"^PROFILES\s*=\s*\{(.*?)\}", driver, re.S | re.M)
    if not m:
        out.append(
            "could not find PROFILES in lib/stage3.py; if it moved, this check is "
            "no longer reading what the harness boots")
    else:
        found = dict(re.findall(r"""["'](\w+)["']\s*:\s*["']([^"']+)["']""",
                                m.group(1)))
        if found != EXPECTED_PROFILES:
            out.append(
                f"lib/stage3.py boots {found!r}; this check expects "
                f"{EXPECTED_PROFILES!r}. Two profiles booted from the same config "
                f"would silently disable the conditional-registration rounds")
        for name, cfg in sorted(found.items()):
            if not (ROOT / "data" / "configs" / cfg).exists():
                out.append(f"profile {name!r} names data/configs/{cfg}, absent")

    # The fault contract, likewise read out of the script that implements it.
    shell = (ROOT / "run-candidate.sh").read_text()
    for code in sorted(FAULT_CODES):
        if str(code) not in shell:
            out.append(
                f"run-candidate.sh does not mention exit code {code}; an "
                f"infrastructure failure it cannot report becomes an ordinary "
                f"candidate failure, and on the original side that pays the "
                f"submission")
    if "SRB-INFRA-FAULT" not in shell:
        out.append("run-candidate.sh never emits an SRB-INFRA-FAULT line")

    return out


def main() -> int:
    found = problems()
    if found:
        print("probe.toml / driver check FAILED:", file=sys.stderr)
        for problem in found:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    probe = config.Probe.load(str(ROOT / "probe.toml"))
    print(f"probe: {len(probe.adversaries)} adversaries "
          f"({', '.join(a.id.split('-')[0] for a in probe.adversaries)}), "
          f"{probe.scope.reruns} reruns, {len(probe.scope.allow)} in scope / "
          f"{len(probe.scope.deny)} out, "
          f"{sum(a.budget_sec for a in probe.adversaries) / 3600:.1f} budget-hours, "
          f"{len(EXPECTED_PROFILES)} graph profiles per tree")
    return 0


if __name__ == "__main__":
    sys.exit(main())
