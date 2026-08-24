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
# The role does not reach the candidate: a candidate that reads the role
# meets every mechanical condition for a break while establishing nothing.
#
# and one positional argument, "original" or "submission", which is this
# script's to know and not the candidate's.
#
# Exit 0 means the candidate passed against this tree; non-zero means it failed.
# That is the whole contract, and it is why a broken candidate reads as "does not
# pass on the original" rather than as a defect in the submission.
#
# Both trees are configured, built and installed with the SAME published argv --
# the one instruction.md gives the agent and the one stage 2 uses:
#
#   cmake -S . -B build -DCMAKE_BUILD_TYPE=Release \
#         -DBUILD_SHARED_LIBS=ON -DCMAKE_INSTALL_PREFIX=<prefix>
#
# One toolchain image serves both, which is why this is the only stage with a C
# compiler and cargo in the same container.  That is not a loophole: stage 2 runs
# in an image where repository C fails to compile at all and has already measured
# whether the submission needs one.  What this image adds is the ability to ask
# the C a question at grading time, which is the entire point of the stage -- a
# candidate's claim is empty unless the original demonstrably passes it.
#
# What a candidate gets is an *installed* library and an *installed* cmark(1),
# never a build tree.  A migration is graded on what it ships.
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

# Keyed by the token, not the role: the candidate is handed $PREFIX, and a path
# with "original" in it would answer the question it is meant to answer by
# observing behaviour.
STATE="$WORK/$TOKEN"
PREFIX="$STATE/install"
BUILD="$STATE/build"
mkdir -p "$STATE"

case "$ROLE" in
    original|submission) ;;
    *) echo "unknown target role" >&2; exit 64 ;;
esac

# Belt and braces.  The harness does not set these, but this script may be run
# by hand or from a shell that has them, and an inherited value is inherited
# all the way down to the candidate.
unset SRB_TARGET_ROLE SRB_TARGET_NAME SRB_ORIGINAL

log() { printf '[%s] %s\n' "$ROLE" "$*" >&2; }

# --- 1. Build and install the tree, once per tree per stage -------------------
# Six rounds share this.  Rebuilding for each of ~120 candidate runs would
# spend the stage's budget on cargo rather than on finding defects, and the tree
# does not change between them.  The marker file records a completed install: a
# prefix that exists because a build was interrupted must not be reused.
if [ ! -f "$STATE/.installed" ]; then
    log "building and installing the tree (first candidate pays for this)"
    rm -rf "$BUILD" "$PREFIX"

    # A submission may arrive carrying its own build output.  Stage 2 scrubs a
    # copy for the same reason: a stale build/ directory with a CMakeCache.txt
    # pointing at another prefix makes the configure step below reuse decisions
    # nobody here made, and a checked-in target/ could hold objects from a build
    # this container never ran.
    find "$TARGET" -maxdepth 2 -depth \
        \( -name build -o -name target -o -name '_build' -o -name 'cmake-build*' \) \
        -type d -prune -exec rm -rf {} + 2>/dev/null

    {
        cmake -S "$TARGET" -B "$BUILD" \
              -DCMAKE_BUILD_TYPE=Release \
              -DBUILD_SHARED_LIBS=ON \
              -DCMAKE_INSTALL_PREFIX="$PREFIX" \
        && cmake --build "$BUILD" --parallel 4 \
        && cmake --install "$BUILD"
    } >>"$STATE/build.log" 2>&1 || {
        log "the tree does not build; see $STATE/build.log"
        tail -30 "$STATE/build.log" >&2
        # A submission that will not build is stage 2's finding, not stage 3's.
        # 71 is in the fault range the adjudicator reads (sysexits 64-78), so the
        # candidate is invalid on whichever tree this happened to: nothing was
        # learned about the submission either way.  Requiring a PASS on the original
        # does not cover this on its own -- a submission that will not build passes
        # there, and the round would read the pair as a divergence.
        exit 71
    }

    if [ ! -x "$PREFIX/bin/cmark" ]; then
        log "the install produced no bin/cmark"
        exit 72
    fi
    touch "$STATE/.installed"
    log "installed into $PREFIX"
fi

# --- 2. Hand the candidate an installed library ------------------------------
# srbcmark.py is on PYTHONPATH and reads these.  It is the only import a
# candidate may make beyond the standard library, and it exists because the
# interesting half of this API is not reachable from a command line: the
# allocator, the streaming parser and the node tree need a C program, and
# compiling one by hand in every candidate would mean six models writing the
# same forty lines of cc invocation.
export SRB_PREFIX="$PREFIX"
export SRB_CMARK="$PREFIX/bin/cmark"
export SRB_LIBDIR="$PREFIX/lib"
export SRB_INCLUDEDIR="$PREFIX/include"
export SRB_TARGET_TOKEN="$TOKEN"
export SRB_SCRATCH="$STATE/candidates/${SRB_CANDIDATE_NAME:-candidate}"
mkdir -p "$SRB_SCRATCH"

# The library is found through the install prefix, not through a build tree and
# not through anything on the system.  There is no cmark in /usr here, but a
# submission that vendored one would otherwise be able to answer with it.
export LD_LIBRARY_PATH="$PREFIX/lib"
export PKG_CONFIG_PATH="$PREFIX/lib/pkgconfig"

# --- 3. Run the candidate -----------------------------------------------------
# -p no:cacheprovider because both trees share the candidates directory, and a
# .pytest_cache beside the candidate would be the one piece of state that crosses
# between the two halves of a comparison.
#
# A fresh scratch directory per (candidate, tree) for the same reason: a C helper
# compiled against the original must not still be sitting there when the same
# candidate runs against the submission.
rm -rf "$SRB_SCRATCH"; mkdir -p "$SRB_SCRATCH"

env \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=0 \
    PYTHONPATH=/tests/verification/lib \
    /opt/venv/bin/python -m pytest \
        -p no:cacheprovider -q --no-header -x --timeout=120 \
        "$CANDIDATE"
STATUS=$?
log "candidate ${SRB_CANDIDATE_NAME:-?}: pytest exit $STATUS"
exit $STATUS
