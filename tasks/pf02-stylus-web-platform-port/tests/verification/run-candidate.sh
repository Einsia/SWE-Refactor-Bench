#!/usr/bin/env bash
# =============================================================================
# Run one verification candidate against one tree.
#
# Invoked by swerefactor.verification.CandidateRunner, once per (candidate, tree),
# with cwd set to the tree and:
#
#   SRB_CANDIDATE       absolute path to the candidate's pytest file
#   SRB_CANDIDATE_NAME  its short name
#   SRB_TARGET          the tree to test  (a copy: safe to install into)
#   SRB_TARGET_TOKEN    an opaque per-run label for the same tree
#   SRB_WORK            scratch, shared across candidates and rounds
#
# and one positional argument, "original" or "submission", which is this
# script's to know and not the candidate's.
#
# Exit 0 means the candidate passed against this tree; non-zero means it failed.
# That is the whole contract, and it is why a broken candidate reads as "does not
# pass on the original" rather than as a defect in the submission.
#
# The role does not reach the candidate. A candidate that reads the role
# satisfies every mechanical condition for a break -- passes on the original,
# fails on the submission, three times identically -- while establishing nothing
# about the migration, so this script drops it and uses SRB_TARGET_TOKEN for
# anything a candidate can observe. infra/tests/test_verification.py asserts that
# it is gone.
#
# This script does not build anything, because for this task there is nothing to
# build: both trees are npm packages, and what a candidate needs is the tree
# installed with its five dependencies and reachable the way a consumer reaches
# it. srbstylus does that on first use -- `npm ci --offline` into the tree, then a
# consumer directory holding `node_modules/stylus` -> the tree -- and caches it
# under $SRB_STATE for every candidate and round after the first.
#
# So this script's job is preparation and nothing else: scrub what the submission
# arrived carrying, hand over the environment, run pytest.  Section 2 records what
# is deliberately NOT handed over.
# =============================================================================
set -uo pipefail

TARGET="${SRB_TARGET:?SRB_TARGET is not set}"
# Passed as this script's last argument, never in the environment: a child
# process inherits the environment automatically and inherits argv never, so
# the role cannot reach the candidate by being forgotten about.
ROLE="${1:?the target role was not passed as an argument}"
TOKEN="${SRB_TARGET_TOKEN:?SRB_TARGET_TOKEN is not set}"
CANDIDATE="${SRB_CANDIDATE:?SRB_CANDIDATE is not set}"
WORK="${SRB_WORK:-/tmp/srb-verification}"

case "$ROLE" in
    original|submission) ;;
    *) echo "unknown target role" >&2; exit 64 ;;
esac

# Belt and braces.  The harness does not set these, but this script may be run
# by hand or from a shell that has them, and an inherited value is inherited
# all the way down to the candidate.
unset SRB_TARGET_ROLE SRB_TARGET_NAME SRB_ORIGINAL

log() { printf '[%s] %s\n' "$ROLE" "$*" >&2; }

# Keyed by the token, not the role: the candidate is handed paths under $STATE
# and a directory named "original" would answer the question for it.
STATE="$WORK/$TOKEN"
mkdir -p "$STATE"

# --- 1. Scrub what the submission arrived carrying ----------------------------
# Once per tree, before the install in section 3 can see any of it.
#
# A committed `node_modules/` is the one that matters. The install below is `npm
# ci --offline`, which builds the tree from `package-lock.json` against the image's
# cache -- but a delivered `node_modules/` holding a `stylus` of its own, or a
# sixth dependency §3 forbids, or a build of `src/` from some earlier run, would
# be what `import('stylus')` actually resolved. The submission would then be
# graded on code this container never installed.
#
# Every directory is named in full and none is matched by a glob. `dist` and
# `build` are output directories a bundler may have left; `.parcel-cache` and
# `.rollup.cache` likewise. `test/` is NOT touched -- §3 makes the corpus
# read-only and it is a candidate's widest source of inputs.
if [ ! -f "$STATE/.scrubbed" ]; then
    find "$TARGET" -maxdepth 3 -depth -type d \
        \( -name node_modules -o -name dist -o -name build -o -name out \
           -o -name .parcel-cache -o -name .rollup.cache -o -name .cache \
           -o -name coverage -o -name .nyc_output \) \
        -prune -exec rm -rf {} + 2>/dev/null
    find "$TARGET" -maxdepth 2 -type f \
        \( -name '*.tsbuildinfo' -o -name npm-debug.log \
           -o -name yarn-error.log -o -name .npmrc \) \
        -delete 2>/dev/null
    touch "$STATE/.scrubbed"
fi

# --- 2. No pristine manifest --------------------------------------------------
# build01's stage 3 takes one here and hands it to candidates, because "what the
# build writes into the source tree" is in its allow list. This task has no build
# and nothing in its allow list needs a file listing -- and its deny list rules
# out the file layout inside the tree and every path within it, because §2.1
# deletes `lib/` and §1.4 declines to say where its replacement goes.
#
# A manifest would therefore be a listing of exactly what scope forbids, handed
# to the one reader with an incentive to use it. It is left out rather than
# provided and denied: a capability a candidate does not have is one no
# adjudicator has to rule on.

# --- 3. Hand the candidate its environment -----------------------------------
export SRB_STATE="$STATE/install"
export SRB_TARGET="$TARGET"
export SRB_TARGET_TOKEN="$TOKEN"
export SRB_SCRATCH="$STATE/candidates/${SRB_CANDIDATE_NAME:-candidate}"

# A fresh scratch per (candidate, tree): a `.styl` file written while testing the
# original must not still be sitting there when the same candidate runs against
# the submission, and a `style.css` the middleware wrote for one must not be
# mistaken for the other's.
rm -rf "$SRB_SCRATCH"
mkdir -p "$SRB_SCRATCH" "$SRB_STATE"

# --- 4. Run the candidate ----------------------------------------------------
# -p no:cacheprovider because both trees share the candidates directory, and a
# .pytest_cache beside the candidate would be the one piece of state that crosses
# between the two halves of a comparison.
#
# No -x: a candidate here often asserts several properties of one render, and
# letting the rest of the file run after the first failure gives the adjudicator
# the whole picture for the same money.
#
# --timeout is per test and generous relative to a compile, because the first test
# to run also pays for `npm ci`. A candidate whose subject IS a hang has
# srbstylus's own per-command timeouts, which return an Output rather than raise:
# "the original compiles this and the submission does not return" is a finding
# only if the hang comes back as a result.
#
# -p srbfault is where this task's "the tree does not install" defence lives, and it
# is a plugin rather than a check in this script because the install happens inside
# the process started below: srbstylus.tree() runs `npm ci --offline` and raises
# BuildFailed there. The plugin turns a BuildFailed that no candidate caught into
# exit 71 -- and a DriverFailure into 70 -- so $STATUS below carries it and the
# adjudicator reads the tree as untestable rather than as a divergence. Every other
# task in this benchmark installs in its run-candidate.sh and writes `exit 71` in
# the failure branch directly; see lib/srbfault.py for why the two spellings mean
# the same thing.
env \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=0 \
    PYTHONPATH=/tests/verification/lib \
    /opt/venv/bin/python -m pytest \
        -p no:cacheprovider -p srbfault -q --no-header --timeout=420 \
        "$CANDIDATE"
STATUS=$?
log "candidate ${SRB_CANDIDATE_NAME:-?}: pytest exit $STATUS"
exit $STATUS
