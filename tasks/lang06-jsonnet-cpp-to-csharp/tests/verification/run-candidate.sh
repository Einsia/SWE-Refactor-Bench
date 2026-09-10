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
# and one positional argument, "original" or "submission", which is this script's
# to know and not the candidate's.  This script needs it because the two trees are
# built by different tools -- `make` on the C++, `dotnet publish` on the C# -- and
# there is no honest way to guess.  It goes no further: the candidate is handed a
# prefix keyed by the token, and everything role-shaped is unset below.
#
# Exit 0 means the candidate passed against this tree; non-zero means it failed.
# That is the whole contract, and it is why a broken candidate reads as "does not
# pass on the original" rather than as a defect in the submission.
#
# Both trees end up in the same shape: an install prefix with bin/jsonnet and
# bin/jsonnetfmt, both native executables, invoked identically.  The submission is
# published with the SAME argv stage 2 uses, including the five properties forced
# off, so a candidate upheld here is a candidate the behavioural suite can run.
#
# This is the only image with a C++ compiler and the .NET SDK together.  That is
# not a loophole in stage 2's "no C++ toolchain" assertion: stage 2 has already
# measured whether the submission needs one, in an image where it could not have
# had one.  What this image adds is the ability to ask the C++ a question at
# grading time, which is the entire point of the stage -- a candidate's claim is
# empty unless the original demonstrably passes it.
#
# What a candidate gets is two *installed programs*, never a build tree and never
# a source file.  A migration is graded on what it ships.
# =============================================================================
set -uo pipefail

TARGET="${SRB_TARGET:?SRB_TARGET is not set}"
# Passed as this script's last argument, never in the environment: a child process
# inherits the environment automatically and inherits argv never, so the role
# cannot reach the candidate by being forgotten about.
ROLE="${1:?the target role was not passed as an argument}"
TOKEN="${SRB_TARGET_TOKEN:?SRB_TARGET_TOKEN is not set}"
CANDIDATE="${SRB_CANDIDATE:?SRB_CANDIDATE is not set}"
WORK="${SRB_WORK:-/tmp/srb-verification}"

case "$ROLE" in
    original|submission) ;;
    *) echo "unknown target role" >&2; exit 64 ;;
esac

# Keyed by the token, not the role: the candidate is handed $PREFIX, and a path
# with "original" in it would answer by inspection the question it is meant to
# answer by observing behaviour.
STATE="$WORK/$TOKEN"
PREFIX="$STATE/install"
mkdir -p "$STATE"

# Belt and braces.  The harness pops these, but this script may be run by hand or
# from a shell that has them, and an inherited value is inherited all the way down
# to the candidate.
unset SRB_TARGET_ROLE SRB_TARGET_NAME SRB_ORIGINAL

log() { printf '[%s] %s\n' "$ROLE" "$*" >&2; }

# --- 1. Build and install the tree, once per tree per stage --------------------
# Six rounds share this.  Rebuilding for each of up to 120 candidate runs would
# spend the stage's budget on g++ and MSBuild rather than on finding defects, and
# the tree does not change between them.  The marker file records a *completed*
# install: a prefix that exists because a build was interrupted must not be reused.
if [ ! -f "$STATE/.installed" ]; then
    log "building and installing the tree (the first candidate pays for this)"
    rm -rf "$PREFIX" "$STATE/build"
    mkdir -p "$PREFIX/bin"

    if [ "$ROLE" = "original" ]; then
        # State A, built the way its own README documents and the way stage 2's
        # reference stage builds it.  -j is capped: six rounds may be building
        # at once and g++ on this tree will take every core it is offered.
        {
            make -C "$TARGET" -j4 jsonnet jsonnetfmt \
            && install -m 0755 "$TARGET/jsonnet" "$PREFIX/bin/jsonnet" \
            && install -m 0755 "$TARGET/jsonnetfmt" "$PREFIX/bin/jsonnetfmt"
        } >>"$STATE/build.log" 2>&1 || {
            log "the original does not build; see $STATE/build.log"
            tail -30 "$STATE/build.log" >&2
            exit 70
        }
    else
        # The submission, published exactly as stage 2 publishes it: projects
        # found by the assembly name they produce, five properties forced off, one
        # directory per program, offline feed.  publish.py is the shared
        # implementation of that argv; see the note at the top of it about why
        # this stage carries its own copy.
        {
            /opt/venv/bin/python /tests/verification/lib/publish.py \
                --repo "$TARGET" --prefix "$PREFIX" --state "$STATE"
        } >>"$STATE/build.log" 2>&1 || {
            log "the submission does not publish; see $STATE/build.log"
            tail -40 "$STATE/build.log" >&2
            # A submission that will not build is stage 2's finding, not stage 3's,
            # and stage 2 has already had it: `build` is required and first, so a
            # tree that reaches this stage published there.  71 is in the fault
            # range the adjudicator reads (sysexits 64-78), so the candidate is
            # invalid on whichever tree this happened to.  Requiring a PASS on the
            # original does not cover this on its own -- a submission that will not
            # publish passes there, and the round would read the pair as a
            # divergence.
            exit 71
        }
    fi

    for prog in jsonnet jsonnetfmt; do
        if [ ! -x "$PREFIX/bin/$prog" ]; then
            log "the install produced no bin/$prog"
            exit 72
        fi
    done
    touch "$STATE/.installed"
    log "installed into $PREFIX"
fi

# --- 2. Hand the candidate two installed programs -----------------------------
# srbjsonnet.py is on PYTHONPATH and reads these.  It is the only import a
# candidate may make beyond the standard library.
export SRB_PREFIX="$PREFIX"
export SRB_JSONNET="$PREFIX/bin/jsonnet"
export SRB_JSONNETFMT="$PREFIX/bin/jsonnetfmt"
export SRB_TARGET_TOKEN="$TOKEN"
export SRB_SCRATCH="$STATE/candidates/${SRB_CANDIDATE_NAME:-candidate}"

# A fresh scratch directory per (candidate, tree): an input tree written while
# testing the original must not still be sitting there when the same candidate
# runs against the submission, or a case that writes files would compare one run's
# output against the other run's leftovers.
rm -rf "$SRB_SCRATCH"; mkdir -p "$SRB_SCRATCH"

# --- 3. Run the candidate -----------------------------------------------------
# -p no:cacheprovider because both trees share the candidates directory, and a
# .pytest_cache beside the candidate would be the one piece of state that crosses
# between the two halves of a comparison.
env \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=0 \
    PYTHONPATH=/tests/verification/lib \
    DOTNET_CLI_TELEMETRY_OPTOUT=1 \
    DOTNET_NOLOGO=1 \
    DOTNET_SKIP_FIRST_TIME_EXPERIENCE=1 \
    HOME="$SRB_SCRATCH" \
    /opt/venv/bin/python -m pytest \
        -p no:cacheprovider -q --no-header -x --timeout=120 \
        "$CANDIDATE"
STATUS=$?
log "candidate ${SRB_CANDIDATE_NAME:-?}: pytest exit $STATUS"
exit $STATUS
