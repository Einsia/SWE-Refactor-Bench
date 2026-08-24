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
# The role does not reach the candidate: a candidate that reads the role meets
# every mechanical condition for a break while establishing nothing.
#
# and one positional argument, "original" or "submission", which is this script's
# to know and not the candidate's.
#
# Exit 0 means the candidate passed against this tree; non-zero means it failed.
# That is the whole contract, and it is why a broken candidate reads as "does not
# pass on the original" rather than as a defect in the submission.
#
# THREE BUILDS, NOT ONE
#
# The subject of this task is what a tree does when the machine that compiles the
# bytecode and the machine that reads it disagree about byte order.  So a candidate
# needs the tree built for all three targets stage 2 uses -- x86-64 native, armhf
# and s390x cross -- with the same published argv:
#
#   make CONFIG_LTO= -j<n> [CROSS_PREFIX=<prefix>] \
#        qjs qjsc libquickjs.a tests/bjson.so examples/point.so examples/fib.so \
#        examples/hello examples/test_fib [host-qjsc]
#
# quickjs builds in its own tree, so the three cannot share one directory: each
# gets a copy.  Six builds serve the whole stage -- three targets times two roles
# -- because SRB_WORK is shared across all six rounds and the marker below is
# keyed by the tree's token.  Only the first candidate of the stage waits.
#
# WHAT IS DELIBERATELY NOT HERE
#
# No `reference` build.  The pristine original built for x86-64 is baked into the
# image at /opt/reference, read-only, identical for every candidate and every
# round.  Computing it here would mean running a submission's `make` in the same
# container while the reference answers were being decided, and a Makefile is a
# program.  See the Dockerfile.
#
# No way to ask the pristine original what it does on a cross target.  On s390x
# the original is wrong -- that is the defect -- so its cross answers are not
# evidence, and a candidate built on them asserts the bug while satisfying every
# mechanical condition for a break.  probe.toml's first deny clause says so; this
# script's not shipping the artifact is what makes it awkward to do by accident.
# =============================================================================
set -uo pipefail

TARGET_TREE="${SRB_TARGET:?SRB_TARGET is not set}"
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

# Belt and braces.  The harness does not set these, but this script may be run by
# hand or from a shell that has them, and an inherited value is inherited all the
# way down to the candidate.
unset SRB_TARGET_ROLE SRB_TARGET_NAME SRB_ORIGINAL

# Keyed by the token, not the role: the candidate is handed these paths, and a
# path with "original" in it would answer the question it is meant to answer by
# observing behaviour.
STATE="$WORK/$TOKEN"
TREES="$STATE/trees"
mkdir -p "$TREES"

log() { printf '[%s] %s\n' "${TOKEN:0:8}" "$*" >&2; }

# --- The published build ------------------------------------------------------
GOALS=(qjs qjsc libquickjs.a tests/bjson.so examples/point.so examples/fib.so
       examples/hello examples/test_fib)

cross_prefix_for() {
    case "$1" in
        x86_64) printf '' ;;
        s390x)  printf 's390x-linux-gnu-' ;;
        armhf)  printf 'arm-linux-gnueabihf-' ;;
    esac
}

# Build output a submission may have left in its tree, plus the four C files the
# Makefile generates by running a bytecode compiler.  Both are removed from the
# copy before anything is built, for the same reason stage 2 removes them: a
# shipped `.o` was produced by some other compiler on some other host, and a
# shipped repl.c carries a blob written in whatever byte order that host had.
# Either would let a tree be measured through an artifact this container did not
# produce, which on a byte-order task is the whole question.
scrub_tree() {
    local root="$1"
    find "$root" -type f \( -name '*.o' -o -name '*.a' -o -name '*.so' \
         -o -name '*.obj' -o -name '*.lib' -o -name '*.d' \
         -o -name '*.gcda' -o -name '*.gcno' \) -delete 2>/dev/null
    find "$root" -maxdepth 2 -depth \
         \( -name '.obj' -o -name 'build' -o -name '_build' -o -name 'out' \
            -o -name '.git' -o -name '__pycache__' -o -name '.cache' \) \
         -type d -prune -exec rm -rf {} + 2>/dev/null
    # Named exactly, never globbed: these four are generated, and a glob over *.c
    # would delete the sources.
    rm -f "$root/repl.c" "$root/qjscalc.c" "$root/hello.c" "$root/test_fib.c"
}

build_one() {
    local target="$1" root="$TREES/$target" prefix
    prefix="$(cross_prefix_for "$target")"

    rm -rf "$root"
    cp -a "$TARGET_TREE" "$root" || return 1
    scrub_tree "$root"

    local argv=(make "CONFIG_LTO=" "-j4")
    if [ -n "$prefix" ]; then
        argv+=("CROSS_PREFIX=$prefix")
    fi
    argv+=("${GOALS[@]}")
    if [ -n "$prefix" ]; then
        # Upstream's Makefile redirects every blob-emitting rule at ./host-qjsc, a
        # host binary built from the same sources with the host compiler, and adds
        # it to PROGS only when CROSS_PREFIX is set.  Naming it as a goal makes a
        # cross build that never produced it a build failure rather than a
        # confusing missing-file error later.
        argv+=(host-qjsc)
    fi

    # CFLAGS, LDFLAGS, CROSS_PREFIX and every SRB_ variable are stripped: the
    # build must be the published argv and nothing this script's caller happened
    # to be holding.  PATH and the locale are pinned for the same reason.
    ( cd "$root" && env -i \
        PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
        HOME="$root" TZ=UTC LC_ALL=C.UTF-8 LANG=C.UTF-8 \
        "${argv[@]}" ) >>"$STATE/build-$target.log" 2>&1
}

# --- 1. Build the tree three ways, once per tree per stage --------------------
# Six rounds share this.  Rebuilding for each of ~120 candidate runs would
# spend the stage's budget on gcc rather than on finding defects, and the tree does
# not change between them.  The marker records a completed set: a directory that
# exists because a build was interrupted must not be reused.
if [ ! -f "$STATE/.built" ]; then
    log "building the tree for three targets (first candidate pays for this)"
    for target in x86_64 s390x armhf; do
        if ! build_one "$target"; then
            log "the tree does not build for $target; see $STATE/build-$target.log"
            tail -40 "$STATE/build-$target.log" >&2
            # A submission that will not build is stage 2's finding, not stage 3's.
            # 71 is in the fault range the adjudicator reads (sysexits 64-78), so
            # the candidate is invalid on whichever tree this happened to.
            # Requiring a PASS on the original does not cover this on its own -- a
            # submission that will not build passes there, and the round would read
            # the pair as a divergence.
            exit 71
        fi
        if [ ! -x "$TREES/$target/qjs" ]; then
            log "the $target build produced no qjs"
            exit 72
        fi
        log "built $target"
    done
    touch "$STATE/.built"
fi

# --- 2. Hand the candidate three built trees and a reference ------------------
# srbqjs.py is on PYTHONPATH and reads these.  It is the only import a candidate
# may make beyond the standard library.
export SRB_TREES="$TREES"
export SRB_REFERENCE="${SRB_REFERENCE:-/opt/reference}"
export SRB_TARGET_TOKEN="$TOKEN"
export SRB_SCRATCH="$STATE/candidates/${SRB_CANDIDATE_NAME:-candidate}"

# --- 3. Run the candidate -----------------------------------------------------
# -p no:cacheprovider because both trees share the candidates directory, and a
# .pytest_cache beside the candidate would be the one piece of state that crosses
# between the two halves of a comparison.
#
# A fresh scratch directory per (candidate, tree) for the same reason: a C helper
# compiled against one tree must not still be sitting there when the same
# candidate runs against the other.
rm -rf "$SRB_SCRATCH"; mkdir -p "$SRB_SCRATCH"

env \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=0 \
    PYTHONPATH=/tests/verification/lib \
    SRB_TREES="$SRB_TREES" \
    SRB_REFERENCE="$SRB_REFERENCE" \
    SRB_TARGET_TOKEN="$TOKEN" \
    SRB_SCRATCH="$SRB_SCRATCH" \
    TZ=UTC LC_ALL=C.UTF-8 LANG=C.UTF-8 \
    /opt/venv/bin/python -m pytest \
        -p no:cacheprovider -q --no-header -x --timeout=800 \
        "$CANDIDATE"
STATUS=$?
log "candidate ${SRB_CANDIDATE_NAME:-?}: pytest exit $STATUS"
exit $STATUS
