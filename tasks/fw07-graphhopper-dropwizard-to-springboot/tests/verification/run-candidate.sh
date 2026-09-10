#!/usr/bin/env bash
# =============================================================================
# Run one verification candidate against one tree.
#
# Invoked by swerefactor.verification.CandidateRunner, once per (candidate, tree),
# with cwd set to the tree and:
#
#   SRB_CANDIDATE       absolute path to the candidate's pytest file
#   SRB_CANDIDATE_NAME  its short name
#   SRB_TARGET          the tree to test  (a copy: safe to build in)
#   SRB_TARGET_TOKEN    an opaque per-run label for the same tree
#   SRB_WORK            scratch, shared across candidates and rounds
#
# and one positional argument, "original" or "submission".
#
# Exit 0 means the candidate passed against this tree; non-zero means it failed.
# That is the whole contract, and it is why a broken candidate reads as "does not
# pass on the original" rather than as a defect in the submission.
#
# THE ROLE IS CHECKED AND THEN DISCARDED, which is the interesting difference from
# the other tasks in this benchmark.  On fw06 the role selects a module mirror --
# State A needs an archive of the retired router and the submission must not have
# one -- so the script cannot do its job without it.  Here both trees are built by
# one command against one Maven repository: this image keeps the COMPLETE closure,
# Dropwizard and Spring Boot both, because State A's whole web layer is written
# against the retired stack and a stage that cannot build its own reference cannot
# run a differential.  So there is nothing for the role to select, and the only
# thing this script does with it is refuse an unrecognised one -- which would mean
# the harness contract had changed under it.
#
# Everything else is in lib/stage3.py: building the tree, keeping one server per
# graph profile alive across candidates, and running pytest against them with the
# tree's path, the token and the work root scrubbed from its environment.  Bash
# holds the role and the exit-code mapping; Python holds anything that has to be
# right about which artefact is built and which client asks the questions, because
# those come from stage 2's own modules and reimplementing them in shell is how
# the two stages would drift apart.
# =============================================================================
set -uo pipefail

ROLE="${1:?the target role was not passed as an argument}"
: "${SRB_TARGET:?SRB_TARGET is not set}"
: "${SRB_TARGET_TOKEN:?SRB_TARGET_TOKEN is not set}"
: "${SRB_CANDIDATE:?SRB_CANDIDATE is not set}"

case "$ROLE" in
    original|submission) : ;;
    *) echo "unknown target role: $ROLE" >&2; exit 64 ;;
esac

# Belt and braces.  The harness pops all three before it starts this script, but
# it may be run by hand from a shell that has them, and an inherited value is
# inherited all the way down to the candidate.  stage3.py scrubs them again from
# the environment pytest actually gets; this is the outer of the two layers.
unset SRB_TARGET_ROLE SRB_TARGET_NAME SRB_ORIGINAL

# Not exec, and not because of a trap -- there is none here.  The servers are
# meant to outlive this process: they are the cache that makes a round affordable,
# and stage3.py re-checks their liveness over HTTP on the next invocation.  What
# `exec` would cost is the exit-code translation below.
python3 /tests/verification/lib/stage3.py run
STATUS=$?

# 71 and 74 are stage3.py's infrastructure faults -- the tree would not build, or
# would not boot.  It has already written the marker score.py reads; this echo is
# for whoever is reading the round transcript, where the exit code is all there is.
case "$STATUS" in
    71) echo "SRB-INFRA-FAULT: tree not buildable (exit 71)" >&2 ;;
    74) echo "SRB-INFRA-FAULT: tree not bootable (exit 74)" >&2 ;;
    70) echo "SRB-INFRA-FAULT: the stage-3 driver itself failed (exit 70)" >&2 ;;
esac
exit $STATUS
