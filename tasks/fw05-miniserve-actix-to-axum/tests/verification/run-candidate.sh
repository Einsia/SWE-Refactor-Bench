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
# script's to know and not the candidate's.
#
# Exit 0 means the candidate passed against this tree; non-zero means it failed.
# That is the whole contract, and it is why a broken candidate reads as "does not
# pass on the original" rather than as a defect in the submission.
#
# The role argument selects the registry -- the two trees genuinely cannot be
# built from the same one, because the original requires actix-web and the
# delivery registry has no actix payload and no actix index entry -- and stops
# here.  It is not forwarded to the candidate, whose whole claim to be measuring
# the migration rests on not knowing which tree it is talking to.
#
# Unlike the other tasks in this benchmark, this script does not boot a server.
# It builds the tree, then hands the candidate a launcher: miniserve has no
# single configuration to boot, its surface being flags, and the corpus stage 2
# grades needs 57 distinct command lines to reach 612 cases.  A pre-booted
# server would put the multipart parser, the auth extractor, the TLS acceptor
# and the archive routes permanently out of reach -- which is to say it would
# hide most of what the retired framework used to supply.  lib/srbtarget.py
# argues this at length; the input format it accepts is stage 2's input format.
# =============================================================================
set -uo pipefail

TARGET="${SRB_TARGET:?SRB_TARGET is not set}"
# Passed as this script's last argument, never in the environment: a child
# process inherits the environment automatically and inherits argv never, so the
# role cannot reach the candidate by being forgotten about.
ROLE="${1:?the target role was not passed as an argument}"
TOKEN="${SRB_TARGET_TOKEN:?SRB_TARGET_TOKEN is not set}"
CANDIDATE="${SRB_CANDIDATE:?SRB_CANDIDATE is not set}"
WORK="${SRB_WORK:-/tmp/srb-verification}"

# Keyed by the token, not the role: $STATE is reachable from the candidate
# through the paths it is handed, and a directory named "original" would answer
# the question the candidate is supposed to answer by observing behaviour.
STATE="$WORK/$TOKEN"
# The binary is given a name that says nothing.  Not "miniserve" -- both trees
# produce a binary of that name, so the name carries no information either way,
# but a path ending in the project's own name invites reading it, and this one
# does not.  The bytes are still there for a candidate determined to identify its
# opponent; probe.toml's scope rejects that on sight, and the adjudicator reads
# the candidate's source.
BIN="$STATE/artifact/srv"
mkdir -p "$STATE/artifact"

# Each role gets its own CARGO_HOME and its own target directory, and the two are
# a pair.  Cargo's source replacement is global -- it lives in
# $CARGO_HOME/config.toml and names exactly one local-registry path -- so two
# registries mean two homes; there is no per-invocation flag that redirects a
# local-registry source.
#
# They cannot be crossed over.  A fingerprint records the absolute path of every
# source file it was compiled from, and those paths are under
# $CARGO_HOME/registry/src/, so a cache warmed under one home is worthless under
# another: cargo would silently rebuild the whole closure and the first candidate
# would spend twenty minutes in the linker.  This is also why the target
# directories are used in place rather than copied.
case "$ROLE" in
    original)
        CARGOHOME=/opt/cargo-baseline
        TARGETDIR=/opt/srb/warm-baseline
        ;;
    submission)
        CARGOHOME=/opt/cargo-delivery
        TARGETDIR=/opt/srb/warm-target
        ;;
    *) echo "unknown target role" >&2; exit 64 ;;
esac

# Belt and braces.  The harness does not set these, but this script may be run
# by hand or from a shell that has them, and an inherited value is inherited all
# the way down to the candidate.
unset SRB_TARGET_ROLE SRB_TARGET_NAME SRB_ORIGINAL

log() { printf '[%s] %s\n' "$ROLE" "$*" >&2; }

# --- 1. Build the tree, once per tree per stage --------------------------------
# Six rounds share this.  Rebuilding for each of ~240 candidate runs would
# spend the entire stage in the Rust linker: miniserve's release profile is
# `lto = true` with `codegen-units = 1` over some 320 crates, which is minutes
# even warm.  The tree does not change between candidates, so it is built once.
#
# The marker file records a *completed* build.  A binary that exists because an
# earlier run was interrupted halfway through linking must not be reused, and
# `test -x` cannot tell the difference.
if [ ! -f "$STATE/.built" ]; then
    log "compiling the target (the first candidate pays for this)"
    rm -f "$BIN"

    # A submission may arrive carrying build output, and target/release/miniserve
    # is exactly where the oracle would have been copied to.  Deleting it is not
    # housekeeping: it is the delegation cheat's landing site, and a binary nobody
    # watched being built is not a submission.  Stage 2's build module removes the
    # same paths for the same reason.
    rm -rf "$TARGET/target/release/miniserve" "$TARGET/target/debug/miniserve" 2>/dev/null

    # Built into the role's warm target directory rather than into the tree.
    # Two reasons, and the second is the one that matters: a candidate is handed
    # no path under $TARGET, so nothing a submission committed under target/ can
    # contribute to what gets run -- and the warm cache only counts as warm if
    # it is used in place, its fingerprints naming paths that have not moved.
    #
    # The image asserts there is no release/miniserve in either warm directory,
    # and this removes one anyway: a stale binary here would be handed to a
    # candidate as though it had been built from the tree, which for the
    # submission's own directory would be indistinguishable from delegation
    # succeeding.
    rm -f "$TARGETDIR/release/miniserve" "$TARGETDIR/debug/miniserve"

    # --offline is belt and braces over a config.toml that already has
    # `net.offline = true` and a local-registry source replacement: it makes the
    # failure "no matching package named" rather than a network timeout, which is
    # the error a submission that still needs actix has to be shown.
    #
    # --locked matches how stage 2 built this submission, and stage 3 is not
    # reached unless stage 2 passed every scored check, which no submission does
    # with a `build.compiles` failure among them -- so a lockfile that does not
    # match its manifest has already been caught, and building differently here
    # would grade a different artifact from the one that was measured. For the
    # original it is simply true: its lock is the pinned baseline the registry was
    # built from.
    #
    # RUSTFLAGS is emptied for the same reason stage 2 empties it: an inherited
    # value would apply to one side of a comparison and not the other only if the
    # two builds ran in different environments, and making it explicit costs
    # nothing.
    (
        cd "$TARGET" || exit 73
        exec env \
            CARGO_HOME="$CARGOHOME" \
            CARGO_TARGET_DIR="$TARGETDIR" \
            CARGO_NET_OFFLINE=true \
            CARGO_TERM_COLOR=never \
            RUSTFLAGS= \
            cargo build --offline --locked --release
    ) >>"$STATE/build.log" 2>&1 || {
        log "the tree does not compile; see $STATE/build.log"
        tail -30 "$STATE/build.log" >&2
        # A submission that will not build is stage 2's finding, not stage 3's --
        # and stage 3 is not reached unless stage 2 passed every scored check,
        # which a tree that does not compile cannot, so this is most likely the
        # original failing in a broken image.  71 is in the fault range the
        # adjudicator reads (sysexits 64-78), so the candidate is invalid on
        # whichever tree it was.  Requiring a PASS on the original does not cover
        # the submission side on its own -- a tree that will not build passes on the
        # original, and the round would read the pair as a divergence.
        exit 71
    }

    # cargo names it after the [[bin]] in Cargo.toml. A submission free to rewrite
    # the manifest is not free to rename the binary -- `miniserve` is what users
    # invoke, so it is part of the contract -- and stage 2 scores that as a
    # required check. Here it is simply where to look.
    if [ ! -x "$TARGETDIR/release/miniserve" ]; then
        log "cargo reported success but produced no release/miniserve binary"
        ls -la "$TARGETDIR/release/" 2>/dev/null | head -20 >&2
        exit 70
    fi
    # Copied to the opaque path, and copied rather than linked: a symlink's target
    # is readable with one readlink, and $TARGETDIR is named after the role.
    cp "$TARGETDIR/release/miniserve" "$BIN" || exit 70
    chmod 0755 "$BIN"
    touch "$STATE/.built"
    log "built: $(stat -c '%s' "$BIN") bytes"
fi

# --- 2. Run the candidate -----------------------------------------------------
# A fresh scratch directory per run, and a fresh sample tree inside it for every
# boot -- srbtarget.serve() builds one per server and removes it afterwards. Not
# shared, because a candidate can write: an upload, an mkdir or an overwrite
# would otherwise change the answer the next candidate gets, and a finding would
# depend on which candidate happened to run before it.
RUNTMP="$STATE/run-$$"
rm -rf "$RUNTMP" && mkdir -p "$RUNTMP"

# The candidate is given a launcher, a reference copy of the tree, the TLS
# material and a scratch directory. Not the role, not the original, not the tree
# under test: probe.toml says it is a black-box test of an artifact, and
# forwarding the role would make that claim false and make
# `os.environ["SRB_TARGET_NAME"] == "original"` a ten-point break.
#
# -p no:cacheprovider because both trees run the same candidate file from the
# same directory, and a .pytest_cache beside it would be the one piece of state
# that crosses between the two halves of a comparison.
#
# --timeout is per test, and generous: a candidate that boots six servers in one
# test is doing something reasonable, and each boot waits for a port.
env -u SRB_TARGET_ROLE -u SRB_TARGET_NAME -u SRB_ORIGINAL -u SRB_TARGET \
    -u SRB_WORK -u SRB_CANDIDATE -u SRB_CANDIDATE_NAME \
    SRB_TARGET_BINARY="$BIN" \
    SRB_TREE_REFERENCE=/opt/srb/tree-reference \
    SRB_TREE_SPEC=/opt/srb/tree-spec.json \
    SRB_TLS_DIR=/opt/srb/tls \
    SRB_AUTH_FILE=/opt/srb/auth/auth-file.txt \
    SRB_TMP="$RUNTMP" \
    PYTHONPATH=/tests/verification/lib \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=0 \
    /opt/venv/bin/python -m pytest \
        -p no:cacheprovider -q --no-header -x --timeout=240 \
        "$CANDIDATE"
STATUS=$?
log "candidate ${SRB_CANDIDATE_NAME:-?}: pytest exit $STATUS"

# This run's scratch, removed now rather than at the next start: over a stage
# that is ~240 runs, and each one may have uploaded into a tree copy.
rm -rf "$RUNTMP"
exit $STATUS
