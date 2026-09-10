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
# to know and not the candidate's.  It arrives in argv rather than the environment
# because a child inherits the environment automatically and inherits argv never.
#
# Exit 0 means the candidate passed against this tree; non-zero means it failed.
# That is the whole contract, and it is why a broken candidate reads as "does not
# pass on the original" rather than as a defect in the submission.
#
# WHY THIS TASK'S VERSION IS NOT `tommy`'S  (a C#-to-TypeScript pair, the
# compiled-reference shape this task's design was settled against)
#
# On every other language task in this family the reference is written in a
# language the submission's image cannot run, so leaving it readable costs
# nothing: tommy's reference is C# and `dotnet` is shimmed to exit 127.  Here
# both sides are `node`.  State A's `src/jsonata.js` is a complete, working
# JSONata engine, and a submission that could read it could answer every
# candidate with one `require` -- comparing a tree against itself and passing
# perfectly.  No toolchain can be withheld to prevent that, because the
# submission needs the same runtime the reference does.
#
# So the seal is filesystem permissions rather than absence, and there are two
# things to seal, not one:
#
#   1. the published reference at $REFERENCE, mode 0700 and owned by $REF_USER,
#      so it is readable by the user that runs it and by nobody else unprivileged;
#   2. the harness's staged trees.  SRB_TARGET is work/trees/<token>, and the
#      *other* tree is its sibling for the whole run.  tommy's ancestor walk
#      only makes $BUILD's own ancestors traversable and leaves that directory
#      at 0755, which is harmless for C# and fatal here.  This script chmods it
#      0700 before anything unprivileged runs.
#
# Both probes then run as unprivileged users, one each, neither able to read the
# other's implementation:
#
#   original    node $REFERENCE, published when this image was built from the
#               pinned State A tarball.  Not rebuilt here.  Runs as $REF_USER.
#   submission  `npm run build` as its own package.json declares it, then
#               `node dist/probe.js`.  Runs as $PROBE_USER.
#
# Two users rather than root-for-the-reference: running one side privileged and
# the other not is an asymmetry in the thing being compared, and while State A's
# probe only reads stdin and writes stdout -- so the asymmetry is inert for it --
# "inert for the reference" is not the same as "inert for a port".  Both sides
# unprivileged means a difference the stage reports is a difference in the
# engine.
#
# THE FAILURE MODE THIS SCRIPT IS SHAPED AROUND
#
# A candidate counts only if it PASSES on the original.  So anything that stops
# the original from working converts every candidate into "fails on the original",
# which the adjudicator discards as INVALID -- and a round that finds nothing pays
# the submission all ten of its points.  A broken stage 3 does not fail loudly;
# it awards full marks.  Hence: the reference is published at image build time and
# verified there by check-probe.py, this script re-verifies that it answers
# `hello` before the first candidate of the round, and every way of failing to do
# so exits 70, which the round reports as an error rather than as a pass.
#
# The seal is checked in the other direction for the same reason.  A seal that
# silently did not hold would let a cheating submission pass every round, so the
# check is placed on the submission's side where a failure costs points and gets
# read, and never on the original's side where it would quietly pay them out.
# =============================================================================
set -uo pipefail

TARGET="${SRB_TARGET:?SRB_TARGET is not set}"
ROLE="${1:?the target role was not passed as an argument}"
TOKEN="${SRB_TARGET_TOKEN:?SRB_TARGET_TOKEN is not set}"
CANDIDATE="${SRB_CANDIDATE:?SRB_CANDIDATE is not set}"
WORK="${SRB_WORK:-/tmp/srb-verification}"

# The published reference, from the image build.  A fixed path, and deliberately
# not an unguessable one: hiding it would be the weaker defence, since a
# submission's probe could go looking and this file names the path anyway.  What
# keeps it out of reach is that /opt/reference is mode 0700 and owned by
# $REF_USER, while everything on the submission's side runs as $PROBE_USER.
#
# dist/probe.js because that is what State A's own `npm run build` produces --
# `tools/build.js` copying src/ -- so this path is determined by the shipped
# package rather than chosen here.
REFERENCE=/opt/reference/dist/probe.js
REFERENCE_DIR=/opt/reference

# The two unprivileged users.  Created in the Dockerfile; asserted to exist
# below, because a missing user makes `setpriv` fail and every candidate would
# then read as "fails on the submission" -- a wrong ten-point loss per round for
# an honest port, and the mirror image of the failure the header describes.
REF_USER=srbref
PROBE_USER=srbprobe

# Keyed by the token, not the role: the candidate is handed paths through
# $SRB_PROBE_CWD, and a path with "original" in it would answer the question the
# candidate is supposed to answer by observing behaviour.
STATE="$WORK/$TOKEN"
BUILD="$STATE/build"
mkdir -p "$STATE"

case "$ROLE" in
    original|submission) ;;
    *) echo "unknown target role" >&2; exit 64 ;;
esac

# Belt and braces.  The harness scrubs these; this script may also be run by hand
# or from a shell that has them, and an inherited value is inherited all the way
# down to the candidate.  By name rather than by glob: a `unset ${!SRB_@}` here
# would also unset the five variables this script needs.
unset SRB_TARGET_ROLE SRB_TARGET_NAME SRB_ORIGINAL

log() { printf '[%s] %s\n' "$ROLE" "$*" >&2; }

# The probe's argv, as JSON, written by json.dumps rather than by shell quoting.
# The paths here contain a per-run token and a directory the harness chose; a
# hand-built `["a", "b"]` is correct until one of them contains a quote or a
# backslash, and the failure then is a candidate that cannot start for a reason
# that looks like a broken helper library.
write_argv() {
    python3 -c 'import json,sys; sys.stdout.write(json.dumps(sys.argv[1:]))' \
        "$@" > "$STATE/argv.json"
}

# Seal the harness's staged trees.
#
# $TARGET is work/trees/<token> and the other tree is its sibling for the whole
# run.  0700 on the parent leaves it searchable and listable by root -- which is
# what does the `cp -a` below, and what pytest runs as -- and closes it to both
# unprivileged users.  Neither probe can then reach the other tree's source, which
# on this task is the difference between a graded comparison and a submission
# `require`-ing the reference implementation.
#
# Done for both roles, before either builds: the submission is the side with a
# motive, but a seal that depends on which role happens to run first is not a seal.
# Failure is not fatal here -- it is checked, as $PROBE_USER, further down where
# it can be reported on the side that gets read.
seal_staged_trees() {
    local trees
    trees="$(dirname "$TARGET")"
    # Refuse to chmod something that is plainly not the harness's staging
    # directory.  If a future harness stages trees somewhere else, this script
    # should say so rather than quietly narrowing permissions on / or on the
    # task directory.
    case "$(basename "$trees")" in
        trees) ;;
        *)  log "WARNING: $trees is not the staging directory this script expects"
            log "         (SRB_TARGET=$TARGET). Not changing its mode; the"
            log "         submission-side seal check below will fail loudly."
            return 0 ;;
    esac
    chmod 0700 "$trees" 2>/dev/null \
        || log "WARNING: could not chmod 0700 $trees"
}
seal_staged_trees

# --- 1. Prepare the tree, once per tree per stage -----------------------------
# Six rounds and up to 120 candidate runs share this.  The marker file records
# a *completed* preparation: a dist/ that exists because a build was interrupted
# must not be reused.
if [ ! -f "$STATE/.ready" ]; then
    log "preparing the tree (the first candidate pays for this)"
    rm -rf "$BUILD"

    if ! id -u "$REF_USER" >/dev/null 2>&1; then
        log "the unprivileged user $REF_USER does not exist in this image"
        exit 70
    fi
    if ! id -u "$PROBE_USER" >/dev/null 2>&1; then
        log "the unprivileged user $PROBE_USER does not exist in this image"
        exit 70
    fi
    if ! command -v setpriv >/dev/null 2>&1; then
        log "setpriv is not in this image; both probes must run unprivileged"
        exit 70
    fi

    if [ "$ROLE" = original ]; then
        # The reference is not built here.  It was published when the image was
        # built, from the pinned State A tarball, and check-probe.py ran real
        # candidates through it then.  Building it now would mean the sentence
        # "the original passes this test" depended on an `npm run build` inside
        # the grading run -- and a build that failed would pay the submission its
        # points, which is the one failure this stage must not have.
        if [ ! -f "$REFERENCE" ]; then
            log "the published reference is missing from the image at $REFERENCE"
            exit 70
        fi
        # State A's own src/jsonata.js is checked against the source the reference
        # was published from.  They come from the same tarball, so this compares
        # equal or something is deeply wrong -- a mounted tree that is not the
        # State A this image was built against, most likely -- and continuing
        # would grade the submission against a reference for a different original.
        if [ -f "$REFERENCE_DIR/jsonata.js.sha256" ] \
           && [ -f "$TARGET/src/jsonata.js" ]; then
            have="$(sha256sum "$TARGET/src/jsonata.js" | cut -d' ' -f1)"
            want="$(cut -d' ' -f1 < "$REFERENCE_DIR/jsonata.js.sha256")"
            if [ "$have" != "$want" ]; then
                log "the tree's src/jsonata.js is not the file the reference was built from"
                log "  tree:      $have"
                log "  reference: $want"
                exit 70
            fi
        fi
        REF_DROP=(setpriv "--reuid=$REF_USER" "--regid=$REF_USER" --init-groups --)
        install -d -m 0755 "$BUILD"
        write_argv "${REF_DROP[@]}" node "$REFERENCE"
        # cwd is the reference's own directory, so a relative `require` inside it
        # resolves the way it did when the image was built.
        echo "$REFERENCE_DIR" > "$STATE/cwd"
    else
        # A submission may arrive carrying its own dist/ and node_modules.  Both
        # are dropped for the reason stage 2 drops them: a committed dist/ that
        # the source does not produce would be graded instead of the source, and a
        # vendored node_modules is a dependency the agent's own environment did
        # not have.  Copied rather than built in place so the deletion cannot
        # touch the tree the harness staged.
        cp -a "$TARGET" "$BUILD"
        rm -rf "$BUILD/dist" "$BUILD/node_modules" "$BUILD/.git" \
               "$BUILD/.npm" "$BUILD/.cache" "$BUILD/.tsbuildinfo"

        if [ ! -f "$BUILD/package.json" ]; then
            log "no package.json: nothing declares how to build this tree"
            exit 71
        fi

        DROP=(setpriv "--reuid=$PROBE_USER" "--regid=$PROBE_USER" --init-groups --)

        # npm wants a home and a cache it can write.  Both under $STATE, both
        # owned by the build user, so nothing it does lands in root's HOME or in a
        # shared npm cache that the other tree's build would then read.
        install -d "$STATE/home" "$STATE/npmcache"
        chown -R "$PROBE_USER" "$BUILD" "$STATE/home" "$STATE/npmcache"

        # Every directory on the path down to $BUILD has to be traversable by that
        # user, not just the two this script made.  $SRB_WORK comes from the
        # harness -- `--work` on the command line, which may sit under a mode-0700
        # mkdtemp -- and one unsearchable ancestor makes node fail to open its own
        # script, which surfaces as ENOENT on a file that is plainly there.  So the
        # walk goes all the way up rather than stopping where this script's own
        # knowledge stops.
        #
        # +x only, and no -R: search permission on the directories, nothing about
        # their contents.  $BUILD is handled separately by the chown above, because
        # a build writes into it.
        #
        # This walk cannot undo the seal: $BUILD is under work/run/<token> and the
        # staged trees are under work/trees, so work/trees is not an ancestor of
        # $BUILD.  The check below is what proves that rather than this comment.
        dir="$BUILD"
        while [ "$dir" != "/" ] && [ -n "$dir" ]; do
            chmod a+x "$dir" 2>/dev/null || true
            dir="$(dirname "$dir")"
        done

        # --- The seal, checked from where it matters ------------------------
        # As $PROBE_USER, because that is who would be doing the reading.  Both
        # halves: the published reference, and the staged tree next door.
        #
        # On the submission's side deliberately.  A failure here exits non-zero,
        # every candidate in the round reads "fails on the submission", and the
        # submission loses the round's points -- which for an honest submission is
        # wrong, and is exactly why it is loud enough that a human reading a 0/60
        # with these lines in the log will find the cause. The same check on the
        # original's side would exit 70 on a stage that is working correctly for
        # every honest submission and, when it did fire, would hand a cheating
        # submission full marks in silence.  Loud and occasionally unfair beats
        # quiet and exploitable.
        seal_ok=1
        if "${DROP[@]}" sh -c "test -r '$REFERENCE'" 2>/dev/null; then
            log "STAGE FAULT: $PROBE_USER can read the published reference at $REFERENCE"
            seal_ok=0
        fi
        if "${DROP[@]}" sh -c "ls '$(dirname "$TARGET")' >/dev/null 2>&1"; then
            log "STAGE FAULT: $PROBE_USER can list the harness's staged trees at $(dirname "$TARGET")"
            seal_ok=0
        fi
        if [ "$seal_ok" -ne 1 ]; then
            log "The submission's probe could read a working implementation of the"
            log "engine it is being compared against, so this round cannot measure"
            log "anything. Refusing to run it. This is a fault in the stage, not in"
            log "the submission: fix the image or the harness and re-run."
            exit 74
        fi

        {
            "${DROP[@]}" env \
                HOME="$STATE/home" \
                npm_config_cache="$STATE/npmcache" \
                npm_config_offline=true npm_config_audit=false \
                npm_config_fund=false npm_config_update_notifier=false \
                sh -c "cd \"\$1\" && npm run build" sh "$BUILD"
        } >>"$STATE/build.log" 2>&1 || {
            log "the tree does not build; see $STATE/build.log"
            tail -30 "$STATE/build.log" >&2
            # A submission that will not build is stage 2's finding, not stage
            # 3's.  71 is in the fault range the adjudicator reads (sysexits
            # 64-78), so the candidate is invalid on whichever tree this happened
            # to.  Requiring a PASS on the original does not cover this on its own
            # -- a submission that will not build passes there, and the round would
            # read the pair as a divergence.
            exit 71
        }

        if [ ! -f "$BUILD/dist/probe.js" ]; then
            log "the build produced no dist/probe.js"
            exit 72
        fi
        # The probe runs unprivileged too, and through setpriv rather than
        # runuser: setpriv execs, so there is no process between this script's
        # child and node.  runuser forks, and the process it leaves in the middle
        # is the one that survives a timeout kill with node still running behind
        # it -- which in a 240-candidate stage is a probe still holding a pipe
        # when the next one starts.
        write_argv "${DROP[@]}" node "$BUILD/dist/probe.js"
        echo "$BUILD" > "$STATE/cwd"
    fi

    # --- The handshake, before any candidate --------------------------------
    # Both roles.  On the submission side this is the cheap version of what
    # stage 2 measured; on the original side it is the check that matters, because
    # a reference that does not answer turns every candidate into a pass for the
    # submission.  `hello` because it needs no expression: a probe that answers it
    # is running and speaking the protocol, and anything beyond that is what the
    # candidates are for.
    argv_json="$(cat "$STATE/argv.json")"
    if ! SRB_PROBE_ARGV="$argv_json" SRB_PROBE_CWD="$(cat "$STATE/cwd")" \
         SRB_SCRATCH="$STATE/handshake" \
         PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/tests/verification/lib \
         python3 /tests/verification/lib/handshake.py; then
        log "the built probe does not answer hello"
        # Distinct codes because the two mean opposite things.  70 is a broken
        # reference: a stage fault, and the one that would otherwise pay the
        # submission its points for nothing.  73 is a submission whose probe
        # does not run, which is a finding stage 2 already made.
        if [ "$ROLE" = original ]; then exit 70; else exit 73; fi
    fi

    touch "$STATE/.ready"
    log "tree ready"
fi

# --- 2. Hand the candidate the probe -----------------------------------------
# An argv list rather than a path: both trees are `node`, but they are reached
# through different setpriv prefixes and from different directories, and
# srbjsonata drives both through the same runner.  That indirection is what makes
# "the original passes this" mean the same thing on both sides.
export SRB_PROBE_ARGV="$(cat "$STATE/argv.json")"
export SRB_PROBE_CWD="$(cat "$STATE/cwd")"
export SRB_TARGET_TOKEN="$TOKEN"
export SRB_SCRATCH="$STATE/candidates/${SRB_CANDIDATE_NAME:-candidate}"

# A fresh scratch per (candidate, tree): a file a candidate wrote while running
# against one tree must not still be there when the same candidate runs against
# the other, or the second run is reading the first run's output.
rm -rf "$SRB_SCRATCH"; mkdir -p "$SRB_SCRATCH"

# --- 3. Run the candidate ----------------------------------------------------
# -p no:cacheprovider because both trees share the candidates directory, and a
# .pytest_cache beside the candidate would be the one piece of state that crosses
# between the two halves of a comparison.
#
# The venv is what candidates run in; the harness orchestrating the stage uses
# the system python.  The process running code an adversary wrote does not share
# an interpreter with the process deciding what it proved.
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

