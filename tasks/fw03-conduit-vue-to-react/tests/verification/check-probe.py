#!/usr/bin/env python3
"""Build-time sanity check for probe.toml and the candidate's API.

Runs inside the stage-3 image build. ``swerefactor validate`` checks the probe's
own shape while a task is being authored; this is the backstop for the case where
something was edited and only the image was rebuilt. Failing here costs a build.
Failing at run time costs six rounds of model time and produces a score that
is wrong rather than absent.

evaluation.toml is not in this build context -- it is one level up, in tests/ --
so the adversary count is compared against a number recorded here at authoring
time rather than read from the scoring policy. ``swerefactor validate`` compares
against the real thing.

The second half checks what prompt.txt promises a candidate can do, because the
prompt is the contract and a round spent discovering that ``srbprobe`` does not
import is a round that pays ten points for nothing.
"""
from __future__ import annotations

import sys
from pathlib import Path

from swerefactor import config

#: What [scoring] verification_models says in tests/evaluation.toml.
EXPECTED_ADVERSARIES = 6

HERE = Path("/tests/verification")


def check_probe(problems: list[str]) -> config.Probe:
    probe = config.Probe.load(str(HERE / "probe.toml"))

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

    return probe


def check_candidate_api(problems: list[str]) -> None:
    """The candidate's library imports, and offers what the prompt says it does.

    Imported rather than merely present: ``srbprobe`` reads the standard selector
    list out of the harness at import time, so a harness whose export was renamed
    would be found here rather than by an adversary.
    """
    sys.path.insert(0, str(HERE / "lib"))
    try:
        import srbprobe
    except Exception as exc:  # pragma: no cover - this is the check
        problems.append(f"the candidate's library does not import: {exc!r}")
        return

    for name in ("observe", "Observation", "Probe", "ProbeError", "STANDARD_PROBES"):
        if not hasattr(srbprobe, name):
            problems.append(f"srbprobe offers no {name!r}, which prompt.txt promises")

    # 35 selectors, read from normalize.mjs rather than duplicated. Zero means the
    # export was renamed or reformatted past the parser, and a candidate would
    # then see an empty STANDARD_PROBES and conclude nothing is observed.
    n = len(getattr(srbprobe, "STANDARD_PROBES", ()))
    if n != 35:
        problems.append(
            f"srbprobe read {n} standard selectors out of the harness, expected 35"
        )

    # Every accessor prompt.txt tells an adversary to assert on.
    obs = srbprobe.Observation(
        {"stepErrors": [], "pageErrors": [], "requests": [], "observed": None}
    )
    for name in (
        "rendered", "url", "title", "node_count", "has_app", "body_classes",
        "nodes", "text", "probes", "controls", "requests", "storage",
        "storage_keys", "step_errors", "page_errors",
    ):
        try:
            getattr(obs, name)
        except Exception as exc:  # pragma: no cover - this is the check
            problems.append(f"Observation.{name} raises on an empty observation: {exc!r}")
    for name, arg in (("probe", ".card"), ("control", "Email"), ("calls", None)):
        try:
            getattr(obs, name)(arg) if arg is not None else getattr(obs, name)()
        except Exception as exc:  # pragma: no cover - this is the check
            problems.append(f"Observation.{name}() raises on an empty observation: {exc!r}")

    # An unset SRB_PROBE_DIST must be a clear error rather than a stack trace: it
    # is what an adversary sees if it tries to run a candidate outside the stage.
    try:
        srbprobe.observe([{"goto": "#/"}])
    except srbprobe.ProbeError:
        pass
    except Exception as exc:  # pragma: no cover - this is the check
        problems.append(
            f"observe() without SRB_PROBE_DIST raises {type(exc).__name__}, "
            "not ProbeError"
        )
    else:
        problems.append("observe() succeeded with no application to drive")


def main() -> int:
    problems: list[str] = []
    probe = check_probe(problems)
    check_candidate_api(problems)

    if problems:
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    print(
        f"probe: {len(probe.adversaries)} adversaries, "
        f"{probe.scope.reruns} reruns, {len(probe.scope.allow)} in scope / "
        f"{len(probe.scope.deny)} out"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
