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

    # Specific to this task: the deny list has to exclude what stage 2's
    # normalizer masks, or a candidate can win ten points off a value neither
    # tree controls.  Checked by keyword rather than by importing the normalizer,
    # which lives in stage 2's build context and is not here.
    #
    # The last two are miniserve's own rather than generic HTTP: the listing
    # renders a humanised relative time that changes with the wall clock, and
    # clap renders --help in a layout that no observable behaviour depends on.
    # Both are real differences a candidate would find in minutes and neither
    # says anything about the port.
    must_deny = {
        "date": "the Date header",
        "server": "the Server header",
        "casing": "header name casing",
        "chunked": "chunked vs Content-Length",
        "reason phrase": "HTTP reason phrases",
        "ago": "the humanised relative mtime in a listing",
        "--help": "clap's help and version rendering",
        "timing": "timing and throughput",
    }
    deny_text = " ".join(probe.scope.deny).lower()
    for needle, what in must_deny.items():
        if needle not in deny_text:
            problems.append(
                f"[scope] deny does not mention {needle!r}: {what} is masked by "
                f"the behavioural stage, so a candidate could win a round on it "
                f"while proving nothing"
            )

    # The single most important deny clause, and the only one that is about the
    # benchmark rather than about HTTP.  A candidate that identifies which tree it
    # is on -- by reading the binary it was handed, by looking for a file only one
    # tree has, by comparing the environment -- passes on the original, fails on
    # the submission and reproduces perfectly, three times.  It meets every
    # mechanical condition for a break and establishes nothing whatsoever.  Stage
    # 3 has no way to detect this at run time: the adjudicator reading the
    # candidate's source is the only defence, and it can only apply a rule that
    # was written down.
    if "identity of the tree" not in deny_text:
        problems.append(
            "[scope] deny does not exclude identifying which tree is under test. "
            "That is the one break a candidate can manufacture without finding "
            "any defect at all, and the adjudicator cannot reject what the scope "
            "did not exclude"
        )

    # And the reverse: the things this task most needs a candidate to be allowed
    # to attack.  Each was supplied by a retired crate and had to be rebuilt by
    # hand, which is what makes them the productive rounds -- a scope that forgot
    # to permit one would silently retire two or three of the twelve.
    must_allow = {
        "range": "byte ranges, which actix-files used to implement",
        "if-none-match": "conditional requests, likewise",
        "etag": "the validator's form",
        "content-disposition": "the download filename encoding",
        "multipart": "the upload parser, which replaced actix-multipart",
        "www-authenticate": "the auth challenge, which replaced "
                           "actix-web-httpauth",
        "tls": "the TLS acceptor, which came from actix-web behind a feature",
        "zip": "the archive writers",
        "exit status": "the CLI, which is half the recorded corpus",
    }
    allow_text = " ".join(probe.scope.allow).lower()
    for needle, what in must_allow.items():
        if needle not in allow_text:
            problems.append(
                f"[scope] allow does not mention {needle!r}: {what}, so a round "
                f"spent there would be thrown out on review"
            )

    # A candidate compiles two Rust trees in release mode with `lto = true` and
    # `codegen-units = 1`.  The image warms both closures, so only the crate's own
    # code and the link are left -- but the link alone is minutes, and the first
    # candidate of the stage pays for it.  A timeout tight enough to kill that
    # first candidate reports "does not pass on the original" and discards a round
    # for a reason that has nothing to do with the submission.
    #
    # It is read out of [metadata] because that is where it lives: the runner's
    # own timeout is passed to CandidateRunner by the caller, and probe.toml
    # records the value the task was authored to need. A missing key is a problem
    # in itself -- the default would be whatever the caller happened to pass.
    timeout = probe.metadata.get("candidate_timeout_sec")
    if not isinstance(timeout, (int, float)):
        problems.append(
            "[metadata] candidate_timeout_sec is missing or not a number; this "
            "task needs an explicit one because a candidate here compiles Rust"
        )
    elif timeout < 900:
        problems.append(
            f"candidate_timeout_sec is {timeout:g}, which is under the fifteen "
            f"minutes a cold release link of this crate can take; the first "
            f"candidate of the stage would be killed mid-build"
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
