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

import re
import sys

from swerefactor import config

#: What [scoring] verification_models says in tests/evaluation.toml.
EXPECTED_ADVERSARIES = 6

#: Every round must name where to spend its hour.  Six unguided rounds
#: converge on the same three obvious constructs -- `let` in ES5, an unclosed
#: string, `--help` -- and the stage then measures one surface six times
#: instead of measuring the parser once.  This is the only per-adversary field
#: that is required rather than merely validated, because it is the one that
#: decides whether the six rounds cover the repository.
FOCUS_KEY = "focus"

#: Where probe.toml is inside the image.  Overridable by argument so this can be
#: run against the tree during authoring: a check that only executes inside a
#: 4 GB image build is a check nobody runs until it fails, and the failure then
#: looks like a broken image rather than a broken probe.
DEFAULT_PROBE = "/tests/verification/probe.toml"


#: A deny clause that names a whole output surface, with no exception attached.
#:
#: Two of these shipped.  One put "any text written to stderr on a successful
#: run" out of scope while instruction.md demands the two-line no-`ecmaVersion`
#: warning on stderr byte for byte and stage 2 grades it that way; the other
#: excluded "the exact prose of `--help`" while instruction.md specifies the
#: usage string and stage 2 grades `cli/help` on bytes.  Both were written
#: against incidental output -- debug chatter, invented failure text -- and both
#: read, to an adversary deciding where to spend an hour, as a promise that a
#: surface stage 2 scores cannot be attacked.  That is the exact failure the
#: [scope] comment warns about: a scope forbidding what the instruction
#: promised.
#:
#: The shape is what is checked rather than the two sentences, because the next
#: one will be about stdout, or exit codes, or the token stream.  A clause may
#: name a whole surface -- some genuinely are out of scope -- but it has to say
#: what it does not cover, which is what these two failed to do.
BLANKET_DENIALS = (
    r"\bany (?:text|output|bytes|data)\b[^.]{0,40}\b(?:stderr|stdout)\b",
    r"\bthe exact prose of\b",
    r"\bany (?:diagnostic|error) (?:wording|message|text)\b(?![^.]{0,80}"
    r"(?:except|unless|does not (?:cover|include)|is not covered))",
)

#: What makes a blanket clause acceptable: it says where it stops.
EXCEPTION_MARKERS = (
    "except", "unless", "does not cover", "does not include", "is not covered",
    "this does not", "other than", "instruction.md requires",
    "instruction.md specifies", "is in scope", "are in scope",
)

#: The marker has to govern the blanket phrase, not merely share a clause with
#: it.  Both clauses that shipped carried a marker scoping something else --
#: "except where instruction.md states the message is compared" governs
#: `SyntaxError` wording, and the stderr blanket sat after it unqualified; "exit
#: status and the stream a byte went to are in scope" governs exit status, not
#: `--help` prose.  A first version of this check looked for a marker anywhere in
#: the clause, so both shipped clauses passed it, and so did everything else: it
#: reported a clean deny list by never firing.  Segments are split on the
#: boundaries these clauses actually use.
SEGMENT_SPLIT = r"(?:\.\s+|;\s+|\s--\s+|,\s+and\s+)"


def blanket_denials(deny: list[str]) -> list[str]:
    """Deny clauses that exclude a whole output surface without a carve-out.

    A clause is read segment by segment.  A segment naming a whole output
    surface has to carry its own exception, or point at one later in the same
    clause -- a marker in an earlier segment does not count, because that is the
    shape that shipped twice.
    """
    problems: list[str] = []
    for clause in deny:
        lowered = clause.lower()
        segments = re.split(SEGMENT_SPLIT, lowered)
        for segment in segments:
            hit = next((p for p in BLANKET_DENIALS if re.search(p, segment)),
                       None)
            if hit is None:
                continue
            # The exception has to be in the same breath as the blanket.  A
            # window reaching into later segments was tried and is too loose:
            # the `--help` clause that shipped qualified its *exit status* three
            # segments on ("exit status and the stream a byte went to are in
            # scope"), and a forward-looking window read that as covering the
            # prose blanket in segment one.  Requiring the marker here also
            # points at the better fix, which is what both corrections did: drop
            # the blanket phrasing and name the narrow thing directly.
            if any(marker in segment for marker in EXCEPTION_MARKERS):
                continue
            problems.append(
                f"deny clause puts a whole output surface out of scope with no "
                f"exception attached: ...{segment.strip()[:70]!r}... -- if "
                f"stage 2 grades any of that surface on bytes, this tells "
                f"six adversaries not to look at something the submission "
                f"is scored on. Say what the clause does not cover."
            )
            break
    return problems


def main(argv: list[str]) -> int:
    path = argv[1] if len(argv) > 1 else DEFAULT_PROBE
    try:
        probe = config.Probe.load(path)
    except config.ConfigError as exc:
        # The loader's own checks -- schema, required fields, duplicate adversary
        # ids -- fire before any of the ones below.  Caught so the build log gets
        # the sentence and not a traceback whose last frame is this file.
        print(f"  - {exc}", file=sys.stderr)
        return 1
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

    problems.extend(blanket_denials(probe.scope.deny))

    # Duplicate ids are not checked here: config.Probe.load raises on them
    # already, which is why this function can never see a pair.  A second check
    # would read as coverage and be unreachable.
    for adv in probe.adversaries:
        # An adversary with no budget is a round that pays ten points for
        # nothing.
        if adv.budget_sec <= 0:
            problems.append(f"adversary {adv.id!r} has a non-positive budget")
        if not str((adv.metadata or {}).get(FOCUS_KEY) or "").strip():
            problems.append(
                f"adversary {adv.id!r} states no {FOCUS_KEY}, so its hour is "
                f"spent wherever the previous eleven already looked"
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
    sys.exit(main(sys.argv))
