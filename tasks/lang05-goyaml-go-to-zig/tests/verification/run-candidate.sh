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
# and one positional argument, "original" or "submission", which is this
# script's to know and not the candidate's.  A child process inherits the
# environment automatically and inherits argv never, so the role cannot reach
# the candidate by being forgotten about.
#
# Exit 0 means the candidate passed against this tree; non-zero means it failed.
# That is the whole contract, and it is why a broken candidate reads as "does not
# pass on the original" rather than as a defect in the submission.
#
# What a candidate is given
# -------------------------
# One executable, at $SRB_PROBE, and nowhere to look.  Not the tree, not a build
# directory, not an install prefix -- the probe binary and a scratch directory to
# be its cwd.  The tree is what the migration is *of*; stage 1 reads it and stage
# 3 asks it questions.  A candidate that reads it can tell which side it is on and
# say so, which satisfies every mechanical condition for a break -- passes on the
# original, fails on the submission, reproduces exactly -- while establishing
# nothing whatever about the migration.
#
# Three things keep that shut, and none of them is the adjudicator:
#
#   1. only the binary is published, at a path keyed by the opaque token;
#   2. the candidate runs under `env -i` with an explicit allowlist, so it cannot
#      read SRB_TARGET or SRB_TARGET_TOKEN and correlate them against the two
#      staged paths the adversary was told in its opening prompt;
#   3. screen-candidate.py refuses, before running it, a candidate that imports
#      anything able to look -- os, subprocess, pathlib, socket, open().
#
# The fourth is the adjudicator, which reads the candidate and rejects one that
# asks a question about its surroundings instead of about YAML.  These are in
# front of it so that the ordinary case never reaches it.
#
# Where the reference probe comes from
# ------------------------------------
# Stage 2's image, copied into this one at build time and verified there against
# the digest in the manifest that froze stage 2's answers -- so the binary this
# script calls the original is the same artefact the behavioural suite graded
# against, not a second build of the same source.  It is then asked `hello` in the
# stage 3 image before that image is finished.  This script copies it; it does not
# build it, and there is no Go in this image to build it with.
#
# That is deliberate, and it is about which way a failure points.  If the original
# were built here and the build failed, every candidate would be INVALID, every
# round would come back SURVIVED, and the submission would collect all 60 points
# for a broken verifier.  A missing or unusable reference has to be an error, not
# a survival, so the image does not exist unless the reference works and this
# script exits 73 if it is not there.
#
# The submission is built here, with the published argv -- `zig build` at the
# repository root, no arguments -- which is the same command instruction.md gives
# the agent and the same one stage 2 runs.
# =============================================================================
set -uo pipefail

TARGET="${SRB_TARGET:?SRB_TARGET is not set}"
ROLE="${1:?the target role was not passed as an argument}"
TOKEN="${SRB_TARGET_TOKEN:?SRB_TARGET_TOKEN is not set}"
CANDIDATE="${SRB_CANDIDATE:?SRB_CANDIDATE is not set}"
WORK="${SRB_WORK:-/tmp/srb-verification}"

# The reference, frozen into the image by the Dockerfile's Go stage.
REFERENCE=/opt/reference/yaml-probe
# Where the candidate's own library lives.  Absolute, because the candidate runs
# with a PATH and a PYTHONPATH this script chooses and nothing else.
LIB=/tests/verification/lib
SCREEN=/tests/verification/screen-candidate.py
PYTHON=/opt/venv/bin/python

# Keyed by the token, not the role: the candidate is handed a path under here,
# and a path with "original" in it would answer the question the candidate is
# supposed to answer by observing behaviour.
STATE="$WORK/$TOKEN"
BUILT="$STATE/probe"
mkdir -p "$STATE"

case "$ROLE" in
    original|submission) ;;
    *) echo "unknown target role" >&2; exit 64 ;;
esac

# Belt and braces.  The harness does not set these, but this script may be run by
# hand or from a shell that has them, and an inherited value is inherited all the
# way down to the candidate.
unset SRB_TARGET_ROLE SRB_TARGET_NAME SRB_ORIGINAL

log() { printf '[%s] %s\n' "$ROLE" "$*" >&2; }

# --- 1. Produce the probe, once per tree per stage ----------------------------
# Six rounds and ~120 candidate runs share this.  Rebuilding per run would
# spend the stage's budget on the Zig compiler rather than on finding defects, and
# the tree does not change in between.  The marker records a *finished* build: a
# binary that exists because a build was interrupted must not be reused.
if [ ! -f "$STATE/.ready" ]; then
    if [ "$ROLE" = original ]; then
        if [ ! -x "$REFERENCE" ]; then
            log "the reference probe is missing from the image at $REFERENCE"
            # Not a finding about anything.  The image is broken, and the round
            # must come back as an error rather than as a survival.
            exit 73
        fi
        cp "$REFERENCE" "$BUILT" && chmod 0755 "$BUILT" || exit 73
        log "using the reference probe frozen into the image"
    else
        log "building the tree (the first candidate pays for this)"

        # A submission may arrive carrying its own build output.  Stage 2 scrubs a
        # copy for the same reason: a committed zig-out/ was produced by a build
        # this container never watched, in a container that had a network, and it
        # could equally be a binary with no sources behind it.  Rebuilding from
        # source is the point.
        for name in zig-out .zig-cache zig-cache; do
            rm -rf "$TARGET/$name"
        done

        # The published argv, offline, with the caches outside the repository so
        # that a cache this script created cannot be mistaken for one the agent
        # committed.  Same variables stage 2 sets, for the same reasons.
        (
            cd "$TARGET" || exit 71
            env \
                PATH=/usr/local/bin:/usr/bin:/bin \
                HOME="$STATE/buildhome" \
                ZIG_GLOBAL_CACHE_DIR="$STATE/zigcache/global" \
                ZIG_LOCAL_CACHE_DIR="$STATE/zigcache/local" \
                SOURCE_DATE_EPOCH=1700000000 \
                LC_ALL=C.UTF-8 TZ=UTC NO_COLOR=1 \
                zig build
        ) >>"$STATE/build.log" 2>&1 || {
            log "the tree does not build; see $STATE/build.log"
            tail -40 "$STATE/build.log" >&2
            # A submission that will not build is stage 2's finding, not stage
            # 3's.  71 is in the fault range the adjudicator reads (sysexits
            # 64-78), so the candidate is invalid on whichever tree this happened
            # to.  Requiring a PASS on the original does not cover this on its own
            # -- a submission that will not build passes there, and the round would
            # read the pair as a divergence.
            exit 71
        }

        if [ ! -x "$TARGET/zig-out/bin/yaml-probe" ]; then
            log "the build produced no zig-out/bin/yaml-probe"
            exit 72
        fi
        # Copied out under a fixed name, so that the path the candidate is handed
        # differs between the two trees only in the token.  Left in place as well;
        # nothing reads it from there, but a human debugging a round wants it.
        cp "$TARGET/zig-out/bin/yaml-probe" "$BUILT" && chmod 0755 "$BUILT" || exit 72
        log "built $BUILT"
    fi
    touch "$STATE/.ready"
fi

if [ ! -x "$BUILT" ]; then
    log "no probe at $BUILT despite a completed build"
    exit 72
fi

# --- 2. Screen the candidate --------------------------------------------------
# Before it runs, on both trees, so a refusal is not evidence about either one.
# The reason goes to stderr, where the adversary reads it in the same turn and can
# spend the rest of its budget on a real question rather than on wondering why an
# apparently working test failed everywhere.
"$PYTHON" "$SCREEN" "$CANDIDATE"
SCREENED=$?
if [ "$SCREENED" -ne 0 ]; then
    log "candidate ${SRB_CANDIDATE_NAME:-?} was refused by the screen"
    exit "$SCREENED"
fi

# --- 3. Run it ----------------------------------------------------------------
# A fresh scratch directory per (candidate, tree): the probe's cwd, and the one
# place the candidate could leave state.  Anything a candidate wrote while running
# against the original must not still be there when it runs against the
# submission, because that is a channel between the two halves of a comparison.
SCRATCH="$STATE/candidates/${SRB_CANDIDATE_NAME:-candidate}"
rm -rf "$SCRATCH"; mkdir -p "$SCRATCH"

# `env -i`, not `env`.  With plain `env` the candidate inherits SRB_TARGET and
# SRB_TARGET_TOKEN, and the adversary's opening prompt tells it both staged tree
# paths -- so `assert "..." in os.environ["SRB_TARGET"]` identifies the tree in
# one line, from a variable this script never meant to publish.  Everything below
# is either identical between the two roles or opaque.
#
# PATH is a fixed constant: the probe and the interpreter are both invoked by
# absolute path, so PATH exists for the standard library's benefit and says
# nothing about which tree is running.
#
# -p no:cacheprovider because both trees share the candidates directory, and a
# .pytest_cache beside the candidate file would be the one piece of state that
# crosses between the two halves of a comparison.  --timeout as a backstop: the
# library bounds its own reads, but a candidate can loop without asking it
# anything.
env -i \
    PATH=/usr/local/bin:/usr/bin:/bin \
    HOME="$SCRATCH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=0 \
    PYTHONPATH="$LIB" \
    LC_ALL=C.UTF-8 \
    TZ=UTC \
    NO_COLOR=1 \
    SRB_PROBE="$BUILT" \
    SRB_SCRATCH="$SCRATCH" \
    "$PYTHON" -m pytest \
        -p no:cacheprovider -q --no-header -x --timeout=180 \
        "$CANDIDATE"
STATUS=$?
log "candidate ${SRB_CANDIDATE_NAME:-?}: pytest exit $STATUS"
exit $STATUS
