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
# script's business and not the candidate's.  It arrives as an argument because a
# child process inherits the environment automatically and inherits argv never,
# so the role cannot reach the candidate by anyone forgetting about it.
#
# Exit 0 means the candidate passed against this tree; non-zero means it failed.
# That is the whole contract, and it is why a broken candidate reads as "does not
# pass on the original" rather than as a defect in the submission.
#
# WHAT THIS BUILDS, AND WHY IT BUILDS IT HERE
#
# The two trees do not build the same way, and that is the task: one is a POSIX
# source tree that produces an x86-64 executable, the other is supposed to be a
# wasm32-wasi source tree that produces a module.  So:
#
#   original    scripts/build-native-oracle.sh -- configure && make sqlite3.c
#               && gcc.  The published argv, byte-identical to the copy in
#               environment/scripts that the agent was given.
#   submission  ./build-wasi.sh, the tree's OWN build, the one instruction.md
#               requires it to ship.  Nothing here reaches inside it.
#
# Both land at $STATE/product/sqlite3 -- the same filename for both, deliberately,
# so that argv[0] and any path in an error message read alike.
#
# This is the only stage with both toolchains in one image, and that is what the
# stage is for: a candidate's claim is empty unless the original demonstrably
# passes it, so both halves have to be runnable at grading time.  Stage 2 has
# already measured how much of the port works; this asks what it got wrong.
#
# Building at run time from the copied tree, rather than shipping a frozen binary,
# is the more honest arrangement: "the original passes this" is then a claim about
# the tree the adversary actually read.
# =============================================================================
set -uo pipefail

TARGET="${SRB_TARGET:?SRB_TARGET is not set}"
ROLE="${1:?the target role was not passed as an argument}"
TOKEN="${SRB_TARGET_TOKEN:?SRB_TARGET_TOKEN is not set}"
CANDIDATE="${SRB_CANDIDATE:?SRB_CANDIDATE is not set}"
WORK="${SRB_WORK:-/tmp/srb-verification}"

# Keyed by the token, not the role: every path the candidate can see is derived
# from this one, and a path with "original" in it would answer the question the
# candidate is supposed to answer by observing behaviour.
STATE="$WORK/$TOKEN"
PRODUCT="$STATE/product/sqlite3"
mkdir -p "$STATE/product"

case "$ROLE" in
    original|submission) ;;
    *) echo "unknown target role" >&2; exit 64 ;;
esac

log() { printf '[%s] %s\n' "$ROLE" "$*" >&2; }

# --- 1. Build the tree, once per tree per stage -------------------------------
# Six rounds, each writing as many candidates as the clock allows, share this.  A SQLite
# amalgamation build is ninety seconds; paying it per candidate run would spend
# the stage's hour on the compiler rather than on finding defects, and the tree
# does not change in between.  The marker records a *completed* build, so a
# product left behind by an interrupted one is never reused.
if [ ! -f "$STATE/.built" ]; then
    log "building the tree (the first candidate pays for this)"
    rm -f "$PRODUCT"
    rm -rf "$STATE/src"
    cp -a "$TARGET" "$STATE/src" || { log "cannot copy the tree"; exit 70; }
    SRC="$STATE/src"

    # Generated sources and build output are removed from the copy first, by name,
    # at the tree root -- the same list stage 2 removes and for the same reason: a
    # checked-in sqlite3.c would let a submission ship a pre-generated amalgamation
    # and never exercise the generator work.  Building here from a tree stage 2 had
    # already scrubbed differently would also mean stage 3 was testing a different
    # artefact than the one that scored.  Nothing in this list is a source file.
    for name in \
        sqlite3.c sqlite3.h shell.c sqlite3ext.h \
        sqlite3.wasm sqlite3 sqlite3.o libsqlite3.a libsqlite3.so \
        parse.c parse.h parse.out opcodes.c opcodes.h \
        keywordhash.h fts5.c fts5.h lemon mkkeywordhash \
        config.log config.status Makefile sqlite3.pc \
        tsrc bld build .build
    do
        rm -rf "$SRC/$name"
    done

    if [ "$ROLE" = original ]; then
        /tests/verification/data/scripts/build-native-oracle.sh "$SRC" "$PRODUCT" \
            >>"$STATE/build.log" 2>&1
    else
        # The submission's own build script, run from the submission's own tree
        # with nothing added to the environment beyond what stage 2 gives it.
        ( cd "$SRC" && [ -x ./build-wasi.sh ] && ./build-wasi.sh ) \
            >>"$STATE/build.log" 2>&1
    fi
    STATUS=$?

    if [ "$STATUS" -ne 0 ]; then
        log "the tree does not build (exit $STATUS); see $STATE/build.log"
        tail -40 "$STATE/build.log" >&2
        # A submission that will not build is stage 2's finding, not stage 3's.
        # 71 is in the fault range the adjudicator reads (sysexits 64-78), so the
        # candidate is invalid on whichever tree this happened to.  Requiring a PASS
        # on the original does not cover this on its own -- a submission that will
        # not build passes there, and the round would read the pair as a divergence.
        exit 71
    fi

    if [ "$ROLE" = submission ]; then
        # build-wasi.sh is required to leave the module at the tree root under
        # this name -- instruction.md says so and stage 2 looks for it there.
        if [ -f "$SRC/sqlite3.wasm" ]; then
            cp "$SRC/sqlite3.wasm" "$PRODUCT"
        fi
    fi

    if [ ! -s "$PRODUCT" ]; then
        log "the build produced no product"
        tail -40 "$STATE/build.log" >&2
        exit 72
    fi
    chmod +x "$PRODUCT"

    # The spec the candidate's helper reads.  Written after the product exists,
    # so a half-built state cannot be mistaken for a usable one.
    KIND=native
    [ "$ROLE" = submission ] && KIND=wasm
    cat >"$STATE/spec.json" <<JSON
{
  "kind": "$KIND",
  "product": "$PRODUCT",
  "wasmtime": "/opt/wasmtime/wasmtime",
  "native_3311": "/opt/native-3311/sqlite3",
  "default_timeout": 60.0
}
JSON
    touch "$STATE/.built"
    log "built $(wc -c <"$PRODUCT") bytes"
fi

# --- 2. Hand the candidate the product, and nothing else ----------------------
export SRB_SQLITE_SPEC="$STATE/spec.json"
export SRB_SCRATCH="$STATE/candidates/${SRB_CANDIDATE_NAME:-candidate}"

# A fresh scratch per (candidate, tree): a database the candidate wrote while
# testing one tree must not still be sitting there when it runs against the other.
rm -rf "$SRB_SCRATCH"; mkdir -p "$SRB_SCRATCH"

# Everything naming a tree, a role or a token is dropped before pytest starts.
#
#   SRB_TARGET, SRB_WORK   hold paths into the tree under test
#   SRB_TARGET_TOKEN       is opaque in itself, but the adversary knows which token
#                          it gave which tree, so a candidate handed it can be told
#                          what to conclude from it
#   the other three        the harness does not set them; an interactive shell or a
#                          stage-2 environment might, and an inherited value is
#                          inherited all the way down
#
# What this achieves, stated honestly, is that nothing *accidentally* tells a
# candidate which tree it is on.  It is not a wall: a determined candidate can walk
# /proc up to an ancestor and read an environment block that predates these unsets,
# and on this task it would not even need to -- one tree builds an ELF and the other
# builds a wasm module, and both prompts say so.
#
# Hiding the identity is therefore not the mechanism.  The mechanism is the rule: a
# candidate whose assertion depends on knowing which tree it is on meets every
# mechanical condition for a break -- passes on one, fails on the other, three times
# running -- while establishing nothing about the migration, and is rejected on
# sight.  Removing the accidental routes just means a candidate has to reach for it
# deliberately, which is a thing an adjudicator can see.
unset SRB_TARGET SRB_WORK SRB_TARGET_TOKEN
unset SRB_TARGET_ROLE SRB_TARGET_NAME SRB_ORIGINAL

# A label for failure messages, so the two halves of one comparison stay legible in
# a transcript.  Random per invocation rather than per tree: it distinguishes two
# blocks of output without being something a candidate could key on.
SRB_RUN_LABEL="$(head -c4 /dev/urandom | od -An -tx1 | tr -d ' \n')"
export SRB_RUN_LABEL

# --- 3. Run the candidate -----------------------------------------------------
# -p no:cacheprovider because both trees share the candidates directory, and a
# .pytest_cache beside the candidate would be the one piece of state crossing
# between the two halves of a comparison.  --timeout because a hang has to become
# a finding rather than a stalled round; a candidate that means to assert on a
# hang has Output.timed_out for it, which is a shorter timeout than this one.
#
# cwd is the scratch directory, not the tree.  pytest puts cwd on sys.path, and a
# tree that happens to contain a sqlite3.py -- or a conftest.py -- would otherwise
# be imported into the candidate's own process.
cd "$SRB_SCRATCH" || exit 70

env \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=0 \
    PYTHONPATH=/tests/verification/lib \
    /opt/venv/bin/python -m pytest \
        -p no:cacheprovider -q --no-header -x --timeout=600 \
        "$CANDIDATE"
STATUS=$?
log "candidate ${SRB_CANDIDATE_NAME:-?}: pytest exit $STATUS"
exit $STATUS
