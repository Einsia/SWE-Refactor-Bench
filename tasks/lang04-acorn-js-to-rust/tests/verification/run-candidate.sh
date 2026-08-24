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
# script's to know and not the candidate's.  The role is passed as argv because
# a child process inherits the environment automatically and inherits argv
# never, so the role cannot reach the candidate by being forgotten about.  A
# candidate that reads the role meets every mechanical condition for a break
# while establishing nothing about the migration.
#
# Exit 0 means the candidate passed against this tree; non-zero means it failed.
# That is the whole contract, and it is why a broken candidate reads as "does
# not pass on the original" rather than as a defect in the submission.
#
# WHAT "THE SAME INTERFACE" MEANS HERE, AND WHERE IT STOPS
#
# lang01's stage 3 has it easy: both trees are CMake projects, so one published
# argv configures, builds and installs either of them.  This task is not that
# shape.  The submission is built with the argv instruction.md publishes --
#
#     make build && make install PREFIX=<prefix>
#
# -- and State A cannot be, because State A is the JavaScript: it has no
# Makefile, and `acorn-probe` does not exist anywhere in it.  The line protocol
# is specified BY instruction.md, and `lib/reference.js` is the harness's
# implementation of it over upstream's own bundles.  So the original side is
# assembled here instead: rollup builds acorn, acorn-walk and acorn-loose from
# the tree's own src/ with the pinned bundler, upstream's suite is run against
# the result, and two wrappers present it under the two contract names.
#
# Both prefixes are then presented identically.  `bin/acorn` and
# `bin/acorn-probe` are byte-for-byte the same two wrapper scripts in both,
# execing whatever `libexec/` holds, so `file bin/acorn` answers the same thing
# on both trees.  That removes the cheapest tell without pretending to remove
# every one.
#
# It does not remove every one.  One of these trees is node and the other is a
# compiled binary, and a candidate with a subprocess and an hour can find that
# out -- by timing the first request, by reading libexec, by looking in /proc.
# That is why probe.toml's scope denies identification of the tree under test
# however it is learned, and why the adjudicator throws out a candidate that
# rests on it.  The line to defend is not "the candidate cannot tell"; it is
# "telling earns nothing".  Ask the artifact a question about JavaScript.
# =============================================================================
set -uo pipefail

TARGET="${SRB_TARGET:?SRB_TARGET is not set}"
ROLE="${1:?the target role was not passed as an argument}"
TOKEN="${SRB_TARGET_TOKEN:?SRB_TARGET_TOKEN is not set}"
CANDIDATE="${SRB_CANDIDATE:?SRB_CANDIDATE is not set}"
WORK="${SRB_WORK:-/tmp/srb-verification}"

# Keyed by the token, not the role: the candidate is handed $PREFIX, and a path
# with "original" in it would answer by inspection the question it is meant to
# answer by observing behaviour.  SRB_TARGET is token-keyed by the harness for
# the same reason, which is why the wrappers below may name it.
STATE="$WORK/$TOKEN"
PREFIX="$STATE/prefix"
STAGED="$STATE/staged"
mkdir -p "$STATE"

case "$ROLE" in
    original|submission) ;;
    *) echo "unknown target role" >&2; exit 64 ;;
esac

# Belt and braces.  The harness scrubs these, but this script may be run by hand
# or from a shell that has them, and an inherited value is inherited all the way
# down to the candidate.
unset SRB_TARGET_ROLE SRB_TARGET_NAME SRB_ORIGINAL

log() { printf '[%s] %s\n' "$ROLE" "$*" >&2; }

# Exit codes, so a failure in here is never mistaken for a finding:
#   70  the ORIGINAL could not be assembled -- a harness fault.  State A is
#       fixed and authenticated, so this means the image is wrong, not that
#       anything was learned about the submission.
#   71  the submission does not build.  Stage 2's finding, not stage 3's.
#   72  the submission builds but ships no usable entry point.
# All three are in the fault range the adjudicator reads (sysexits 64-78), so a
# candidate that ends this way is invalid rather than a finding.  Requiring a PASS
# on the original does not cover 71 and 72 on its own: a submission that will not
# build passes on the original, and the round would read the pair as a divergence.
die_original() { log "$1"; tail -40 "$STATE/build.log" >&2 2>/dev/null; exit 70; }

# --- 1. Assemble the tree, once per tree per stage ---------------------------
# Six rounds share this.  Rebuilding for each of ~120 candidate runs would
# spend the stage's budget on cargo rather than on finding defects, and the tree
# does not change between them.  The marker file records a completed assembly: a
# prefix left behind by an interrupted build must not be reused.
if [ ! -f "$STATE/.installed" ]; then
    log "assembling the tree (first candidate pays for this)"
    rm -rf "$PREFIX" "$STAGED"
    mkdir -p "$PREFIX/bin" "$PREFIX/libexec" "$STAGED"

    # Either tree may arrive carrying build output.  A checked-in target/ could
    # hold a binary from a build this container never ran, and `make build`
    # would find it fresh and relink rather than compile the source being
    # graded; a dist/ left by an interrupted run of this script would let the
    # original's bundles come from somewhere other than its own src/; and
    # node_modules/ must not exist before the `cp -a` below, which would
    # otherwise copy *into* it.
    #
    # These three names and not a longer list.  `build/` is deliberately absent:
    # it is CMake's output directory, which is why lang01 scrubs it, but it is
    # also a name a Rust tree may legitimately keep sources under -- and stage 3
    # must never fail a submission that stage 2 built.  A stale `build/` can at
    # worst make `make build` fast; deleting someone's source directory cannot be
    # undone.
    find "$TARGET" -maxdepth 2 -depth \
        \( -name target -o -name dist -o -name node_modules \) \
        -type d -prune -exec rm -rf {} + 2>/dev/null

    if [ "$ROLE" = submission ]; then
        {
            make -C "$TARGET" build \
            && make -C "$TARGET" install PREFIX="$STAGED"
        } >>"$STATE/build.log" 2>&1 || {
            log "the tree does not build; see $STATE/build.log"
            tail -40 "$STATE/build.log" >&2
            exit 71
        }

        for name in acorn acorn-probe; do
            if [ ! -x "$STAGED/bin/$name" ]; then
                log "make install produced no executable bin/$name"
                exit 72
            fi
            cp -p "$STAGED/bin/$name" "$PREFIX/libexec/$name"
        done
    else
        # The original.  Same recipe as the environment image and stage 2's
        # freeze, and it is a recipe rather than a copy on purpose: what gets
        # bundled here is the tree the harness handed this script, so the
        # original side of every comparison is built from the bytes under test
        # rather than from something baked in beside them.
        #
        # /opt/buildtools/node_modules arrives already nested -- buble's own
        # acorn 6.4.2 lives under buble/, so the top-level `acorn` name is free
        # for the repository's -- because that is a property of the image, not
        # of this run.  What is per-run is the three relative symlinks: the
        # rollup configs mark `acorn` external, so acorn-loose's emitted bundle
        # requires it by bare name and node resolves that by walking up to this
        # directory.
        cp -a /opt/buildtools/node_modules "$TARGET/node_modules" \
            || die_original "could not stage the bundler into the tree"
        [ -e "$TARGET/node_modules/acorn" ] \
            && die_original "the staged node_modules still has a top-level acorn"
        for name in acorn acorn-loose acorn-walk; do
            ln -s "../$name" "$TARGET/node_modules/$name" \
                || die_original "could not link $name"
        done

        {
            cd "$TARGET" || exit 1
            for name in acorn acorn-walk acorn-loose; do
                node /opt/buildtools/node_modules/rollup/dist/bin/rollup \
                     -c "$name/rollup.config.mjs" || exit 1
            done
        } >>"$STATE/build.log" 2>&1 \
            || die_original "the original did not bundle"

        for entry in acorn/dist/acorn.js acorn-walk/dist/walk.js \
                     acorn-loose/dist/acorn-loose.js; do
            [ -s "$TARGET/$entry" ] \
                || die_original "rollup left no $entry"
        done
        # The CLI phase runs this file directly, so it has to be executable as
        # well as present.  The tarball carries its mode; the copy is what runs.
        chmod 0755 "$TARGET/acorn/bin/acorn" \
            || die_original "the original has no CLI at acorn/bin/acorn"

        # Upstream's own suite, against what was just built.  rollup will
        # happily emit a bundle from sources that parse and produce wrong trees,
        # and every candidate in this stage is measured against this bundle's
        # opinion -- so if it is not really acorn 8.14.0, six rounds produce
        # nothing and the score is wrong rather than absent.  freeze.py demands
        # the same two lines before it will record an expectation.
        if ! ( cd "$TARGET" && node test/run.js ) \
                >"$STATE/suite.log" 2>&1; then
            log "State A's own suite did not run"; tail -40 "$STATE/suite.log" >&2
            exit 70
        fi
        grep -qF 'all passed' "$STATE/suite.log" \
            || { log "State A's suite did not pass against the bundle just built"
                 tail -40 "$STATE/suite.log" >&2; exit 70; }
        grep -qF 'Total: 6763 tests run' "$STATE/suite.log" \
            || { log "State A's suite ran a different number of tests than the"
                 log "6763 instruction.md promises the agent"
                 tail -5 "$STATE/suite.log" >&2; exit 70; }

        # The two wrappers.  `exec node` over the tree's own bundles: the CLI is
        # upstream's published bin script, and the probe is the same reference.js
        # stage 2 measures its recorded cases against -- one implementation of
        # the protocol, so a divergence between the two stages is not a thing
        # that can happen.
        #
        # The heap ceiling is not tuning.  One response -- a full walk of the
        # largest bundled library -- is tens of megabytes of JSON built as a
        # single string, and the default old-space on a small container is below
        # that.  Without this the original alone dies on it, which would read as
        # a candidate that fails on both trees and quietly cost a real one.
        cat > "$PREFIX/libexec/acorn" <<EOF
#!/bin/sh
NODE_OPTIONS="--max-old-space-size=8192" \\
  exec /usr/local/bin/node "$TARGET/acorn/bin/acorn" "\$@"
EOF
        cat > "$PREFIX/libexec/acorn-probe" <<EOF
#!/bin/sh
ACORN_REFERENCE_REPO="$TARGET" \\
NODE_OPTIONS="--max-old-space-size=8192" \\
  exec /usr/local/bin/node /opt/assets/reference.js "\$@"
EOF
        chmod 0755 "$PREFIX/libexec/acorn" "$PREFIX/libexec/acorn-probe"
    fi

    # --- The presentation layer, identical on both trees ---------------------
    # One heredoc, on both branches' path, so the two prefixes differ under
    # libexec/ and nowhere a candidate is meant to look.
    for name in acorn acorn-probe; do
        cat > "$PREFIX/bin/$name" <<EOF
#!/bin/sh
exec "\$(dirname "\$(readlink -f "\$0")")/../libexec/$name" "\$@"
EOF
        chmod 0755 "$PREFIX/bin/$name"
    done

    # Neither tree gets to be the one that answers wrong here.  A prefix that
    # cannot answer `version` is not a tree a candidate can say anything about,
    # and finding that out now costs one line instead of a round.
    if ! printf '{"id":1,"op":"version"}\n' \
         | timeout 120 "$PREFIX/bin/acorn-probe" 2>>"$STATE/build.log" \
         | grep -q '"ok":true'; then
        log "the assembled prefix does not answer op=version"
        tail -40 "$STATE/build.log" >&2
        if [ "$ROLE" = submission ]; then exit 72; else exit 70; fi
    fi

    touch "$STATE/.installed"
    log "assembled into $PREFIX"
fi

# --- 2. Hand the candidate the two entry points ------------------------------
# srbacorn.py is on PYTHONPATH and reads these.  It is the only import a
# candidate may make beyond the standard library, and it exists because the
# protocol is specified byte for byte: json.loads discards key order, number
# formatting and absent-versus-null, which is most of what this task promises,
# so the helper hands back the raw line as well as the parsed value.
export SRB_PREFIX="$PREFIX"
export SRB_ACORN="$PREFIX/bin/acorn"
export SRB_PROBE="$PREFIX/bin/acorn-probe"
export SRB_TARGET_TOKEN="$TOKEN"
export SRB_SCRATCH="$STATE/candidates/${SRB_CANDIDATE_NAME:-candidate}"

# --- 3. Run the candidate ----------------------------------------------------
# -p no:cacheprovider because both trees share the candidates directory, and a
# .pytest_cache beside the candidate would be the one piece of state that
# crosses between the two halves of a comparison.
#
# A fresh scratch directory per (candidate, tree) for the same reason: an input
# file written while testing one tree must not still be sitting there when the
# same candidate runs against the other.
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
