#!/usr/bin/env python3
"""Build-time sanity check for probe.toml.

Runs inside the stage-3 image build.  ``swerefactor validate`` checks the same
things while a task is being authored; this is the backstop for the case where a
probe was edited and only the image was rebuilt.  Failing here costs a build.
Failing at run time costs six rounds of model time and produces a score that is
wrong rather than absent -- and wrong in the generous direction, because a stage 3
that cannot run any candidate records six survivals and pays out every point.

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

#: What [scoring] verification_models says in tests/evaluation.toml.
EXPECTED_ADVERSARIES = 6

#: Every flag the original's FlagSet declares, read off app.go at authoring time.
#: Checked because a profile naming a flag that does not exist is not a typo with
#: a small cost: this server builds its FlagSet with `flag.ExitOnError`, so the
#: process exits at startup, the readiness probe fails, run-candidate.sh exits
#: before running anything, and every candidate "fails" on both trees.  All six
#: rounds then find nothing, which scores identically to six rounds that were
#: honestly survived.  A draft of run-candidate.sh had a `-read_only` profile; the
#: flag does not exist, and nothing downstream would have said so.
ORIGINAL_FLAGS = {
    "config", "document_root", "addr", "enable_cors", "max_upload_size",
    "file_naming_strategy", "shutdown_timeout", "enable_auth",
    "read_only_tokens", "read_write_tokens", "read_timeout", "write_timeout",
}

RUNNER = Path("/tests/verification/run-candidate.sh")


def profile_flags() -> list[tuple[str, list[str]]]:
    """The profile table out of run-candidate.sh, as (name, flags).

    Parsed rather than imported because the table is a heredoc in a shell script,
    and duplicating it into a Python file so this check could import it would mean
    the check no longer read what the harness runs.
    """
    text = RUNNER.read_text()
    m = re.search(r"cat >\"\$PROFILE_TABLE\" <<'EOF'\n(.*?)\nEOF", text, re.S)
    if not m:
        return []
    out = []
    for line in m.group(1).splitlines():
        if not line.strip():
            continue
        name, _, rest = line.partition("\t")
        out.append((name.strip(), rest.split()))
    return out


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
            "probe.toml [scope] states neither allow nor deny, so every divergence "
            "counts and no submission can survive a round"
        )
    if not probe.candidate_command:
        problems.append("probe.toml declares no candidate_command")

    # An adversary with no budget is a round that pays ten points for nothing.
    for adv in probe.adversaries:
        if adv.budget_sec <= 0:
            problems.append(f"adversary {adv.id!r} has a non-positive budget")

    # The flag check.  See ORIGINAL_FLAGS.
    profiles = profile_flags()
    if not profiles:
        problems.append(
            f"could not find the profile table in {RUNNER}; if it moved, this "
            f"check is no longer reading what the harness runs"
        )
    for name, flags in profiles:
        for flag in flags:
            if not flag.startswith("-"):
                continue
            bare = flag.lstrip("-").split("=", 1)[0]
            if bare not in ORIGINAL_FLAGS:
                problems.append(
                    f"profile {name!r} passes -{bare}, which the original's "
                    f"FlagSet does not declare. The server would exit at startup "
                    f"and every candidate would fail on both trees, scoring as "
                    f"six survivals"
                )

    if problems:
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    print(f"probe: {len(probe.adversaries)} adversaries, {probe.scope.reruns} "
          f"reruns, {len(probe.scope.allow)} in scope / {len(probe.scope.deny)} "
          f"out, {len(profiles)} profiles "
          f"({', '.join(n for n, _ in profiles)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
