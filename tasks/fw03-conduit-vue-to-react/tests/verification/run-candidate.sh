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
# The role selects how the artifact is obtained, and stops here. It is not
# forwarded to the candidate, whose whole claim to be measuring the migration
# rests on not knowing which tree it is talking to.
#
# The two roles are obtained differently because they are different applications:
#
#   submission  built from the tree, with the target toolchain, exactly the way
#               stage 2 built it: scrub any delivered build output, install from
#               the offline cache, `npm run build`.
#
#   original    unpacked from data/oracle-dist.tgz, which is State A's own build
#               output from the same source the agent received, checksummed here
#               and byte-identical to the copy stage 2 measures against.
#
# Building State A from source here was the alternative and was rejected: its
# toolchain (@vue/cli-service 3, webpack 4, node-sass 4.12 against a different
# Node ABI) cannot be installed from the target's offline cache, so it would need
# a second cache and a second Node in this image to produce a bundle that is
# already known, shipped and verified. What the stage needs is both artifacts
# present and drivable, which this gives.
#
# Neither role's artifact location reaches the candidate as anything it can
# identify: both are unpacked under a directory named for the opaque token, both
# are handed to the driver as argv, and the driver does not echo it back.
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

SUITE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Keyed by the token, not the role: $STATE is reachable from the candidate
# through the paths it is handed, and a directory named "original" would answer
# the question the candidate is supposed to answer by observing behaviour.
STATE="$WORK/$TOKEN"
DIST="$STATE/dist"
mkdir -p "$STATE"

# Belt and braces. The harness does not set these, but this script may be run by
# hand or from a shell that has them, and an inherited value is inherited all the
# way down to the candidate.
unset SRB_TARGET_ROLE SRB_TARGET_NAME SRB_ORIGINAL

log() { printf '[%s] %s\n' "$ROLE" "$*" >&2; }

# --- 1. Obtain the artifact, once per tree per stage --------------------------
# Six rounds share this. Rebuilding for each of ~120 candidate runs would
# spend the stage's budget on npm rather than on finding defects, and the tree
# does not change between them. The marker records a *completed* build: a dist
# that exists because a build was interrupted must not be reused.
if [ ! -f "$STATE/.built" ]; then
    rm -rf "$DIST"
    mkdir -p "$DIST"

    case "$ROLE" in
    original)
        log "materialising the reference build"
        ( cd "$SUITE_DIR/data" && sha256sum -c oracle-dist.tgz.sha256 ) >>"$STATE/build.log" 2>&1 || {
            log "the shipped reference build does not match its checksum"; exit 70; }
        # --strip-components=1 drops the tarball's own dist-stateA/ prefix, so the
        # files land directly in $DIST. Both roles must produce the same layout at
        # the same depth: a reference side served out of a subdirectory named
        # dist-stateA would answer, through nothing but a path, the question a
        # candidate is supposed to answer by observing behaviour.
        tar xzf "$SUITE_DIR/data/oracle-dist.tgz" -C "$DIST" --strip-components=1 \
            >>"$STATE/build.log" 2>&1 || {
            log "could not unpack the reference build"; exit 70; }
        ;;
    submission)
        log "building the tree (first candidate pays for this)"
        # A submission may arrive carrying build output, a node_modules, or a
        # lockfile from another package manager. Stage 2 scrubs a copy for the
        # same reasons: a delivered dist/ would make a tree that cannot build
        # look like one that can, and a foreign lockfile would let a resolver
        # reach for a version the offline cache never saw.
        # The same list stage 2's build module scrubs, including `.output` and
        # `.git`, and the same four foreign lockfiles. Kept identical on purpose:
        # if the two stages disagreed about what a clean tree is, one of them
        # would be building something the other never saw.
        ( cd "$TARGET" && rm -rf node_modules dist build out .vite .cache \
            .parcel-cache .next .nuxt .turbo coverage .output .git \
            yarn.lock pnpm-lock.yaml bun.lockb bun.lock ) >/dev/null 2>&1

        if [ -f "$TARGET/package-lock.json" ]; then
            INSTALL=(npm ci --offline --no-audit --no-fund)
        else
            INSTALL=(npm install --offline --no-audit --no-fund)
        fi
        ( cd "$TARGET" && "${INSTALL[@]}" ) >>"$STATE/build.log" 2>&1 || {
            log "the tree does not install; see $STATE/build.log"
            # A submission that will not install or build is stage 2's finding,
            # not stage 3's. 71 is in the fault range the adjudicator reads
            # (sysexits 64-78), so the candidate is invalid on whichever tree this
            # happened to. Requiring a PASS on the original does not cover this on
            # its own -- a submission that will not build passes there, and the
            # round would read the pair as a divergence.
            tail -20 "$STATE/build.log" >&2
            exit 71
        }
        ( cd "$TARGET" && npm run build ) >>"$STATE/build.log" 2>&1 || {
            log "the tree does not build; see $STATE/build.log"
            tail -20 "$STATE/build.log" >&2
            exit 71
        }

        # Where the build output landed, located by the harness's own helper
        # rather than by a list restated here. Stage 2 and the agent's own
        # tooling call the same function, so a tree stage 2 was willing to drive
        # is drivable here too -- a second list would eventually disagree with
        # the first, and a submission that configured `build.outDir` would then
        # pass stage 2 and be unattackable for a reason that is not about its
        # behaviour.
        FOUND="$(node "$SUITE_DIR/lib/harness/find-dist.mjs" "$TARGET" 2>>"$STATE/build.log")"
        if [ -z "$FOUND" ] || [ ! -f "$FOUND/index.html" ]; then
            log "the build produced no directory containing index.html"
            exit 71
        fi
        cp -a "$FOUND/." "$DIST/" || { log "could not stage the build output"; exit 70; }
        ;;
    *)
        echo "unknown target role" >&2
        exit 64
        ;;
    esac

    [ -f "$DIST/index.html" ] || { log "no index.html in the staged artifact"; exit 71; }
    touch "$STATE/.built"
fi

# --- 2. Run the candidate ----------------------------------------------------
# Not exec, and no server started here: each srbprobe.observe() call starts its
# own API server on its own port and its own browser, then tears both down. A
# candidate that leaves a page open or a fixture mutated cannot therefore change
# the answer the next candidate gets -- one candidate's finding never depends on
# which candidate ran before it. The artifact is static files; the fixtures are
# rebuilt per scenario.
#
# -p no:cacheprovider because both trees share the candidates directory, and a
# .pytest_cache beside the candidate would be the one piece of state that crosses
# between the two halves of a comparison.
#
# The candidate is given a way to drive the application and nothing else.
# probe.toml says so; forwarding the role would make that claim false and make
# `os.environ["SRB_TARGET_NAME"] == "original"` a ten-point break.
#
# Five variables are removed rather than three. The role trio is the obvious one.
# SRB_TARGET is stripped as well because it points at the source tree, whose
# package.json names the framework in its first ten lines -- a candidate has no
# use for the tree at run time, since what it tests is the build, and the
# adversary already read both trees while writing the test. SRB_TARGET_TOKEN and
# SRB_WORK go for the same reason: neither is needed to drive an application, and
# SRB_WORK is the root both trees' state lives under.
#
# What remains reachable is the artifact's own bytes, by walking up from the path
# the driver is handed. That is not closed by construction -- two builds have to
# coexist on one filesystem for the stage to exist at all -- and it is not meant
# to be: the sibling directories are named by opaque token, so telling them apart
# requires deliberately reading inside one, which is what [scope] deny forbids and
# what the adjudicator rejects on sight. Removing the easy channels is worth doing
# anyway, because a candidate that had to walk the filesystem to fingerprint the
# tree is one whose code says so plainly.
mkdir -p "$STATE/probe"
env -u SRB_TARGET_ROLE -u SRB_TARGET_NAME -u SRB_ORIGINAL \
    -u SRB_TARGET -u SRB_TARGET_TOKEN -u SRB_WORK \
    SRB_PROBE_DIST="$DIST" \
    SRB_PROBE_WORK="$STATE/probe" \
    PYTHONPATH="$SUITE_DIR/lib" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=0 \
    TZ=UTC \
    LC_ALL=C.UTF-8 \
    python3 -m pytest \
        -p no:cacheprovider -q --no-header -x --timeout=900 \
        "$CANDIDATE"
STATUS=$?
log "candidate ${SRB_CANDIDATE_NAME:-?}: pytest exit $STATUS"
exit $STATUS
