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
# The role argument selects the module mirror -- the two trees genuinely cannot be
# built from the same one, because the original requires Gin and the delivery
# mirror carries no Gin zip -- and stops here.  It is not forwarded to the
# candidate, whose whole claim to be measuring the migration rests on not knowing
# which tree it is talking to.
#
# Both trees are built by the same command and started through the same binary
# path, because unlike a Python service there is nothing to choose: `go build` on
# ./cmd/chartmuseum produces one executable and the container runs it with no
# arguments.  So the entry point is not a list of candidates to try, as it is for
# an interpreted stack -- it is one compile and one exec, and a tree that will not
# compile cannot be attacked either way.
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

# Keyed by the token, not the role: $STATE is reachable from the candidate
# through the paths it is handed, and a directory named "original" would answer
# the question the candidate is supposed to answer by observing behaviour.
STATE="$WORK/$TOKEN"
BIN="$STATE/chartmuseum"
LOG="$STATE/server.log"
mkdir -p "$STATE"

case "$ROLE" in
    original)   PROXY=/opt/goproxy-baseline ;;
    submission) PROXY=/opt/goproxy ;;
    *) echo "unknown target role" >&2; exit 64 ;;
esac

# One thing this choice does NOT do, because it is the belief a reader forms here:
# choosing the mirror does not by itself constrain what the build can resolve.
# GOMODCACHE is not set below, so both builds share the container default, and
# CandidateRunner runs the original first (verification.py: execute(original) then
# execute(submission)).  The original's build unpacks the complete closure -- Gin
# included, because State A requires it -- and Go then compiles the submission
# against the already-extracted copy without consulting the pruned mirror at all.
# Measured in this image: with one shared cache the delivery mirror "resolved" Gin;
# with a fresh cache per build it refuses it, which is what the stage Dockerfile's
# probe asserts.
#
# Shared deliberately, and the alternative is worse in a way that has nothing to do
# with this stage.  Stage 2 here builds against the COMPLETE mirror and its
# retired-dependency checks are weight 0 -- "is Gin gone" is stage 1's question by
# design -- so a submission that kept the framework reaches this stage either way.
# Isolating the cache would only change what happens next: its build fails, exit 71
# below, which is read as an infrastructure fault and makes the candidate invalid.
# Sharing lets the build succeed and behave like the original, so the round reports
# no behavioural divergence -- accurate, since nothing was migrated.  Neither
# outcome is a measurement of the port, and both are reachable only once stage 1 has
# already missed a submission that kept the retired framework, which is a pass/fail
# gate whose failure zeroes every stage.  So this is not the place it is repaired.
# The stage Dockerfile's header carries the same reasoning; its probe asserts the
# mirror is correctly BUILT, with a fresh cache per build, which is a different
# claim from the mirror constraining this build.

# Belt and braces.  The harness does not set these, but this script may be run
# by hand or from a shell that has them, and an inherited value is inherited
# all the way down to the candidate.
unset SRB_TARGET_ROLE SRB_TARGET_NAME SRB_ORIGINAL

log() { printf '[%s] %s\n' "$ROLE" "$*" >&2; }

# --- 1. Build the tree, once per tree per stage --------------------------------
# Six rounds share this.  Recompiling for each of ~120 candidate runs would
# spend the stage's budget on the Go linker rather than on finding defects, and
# the tree does not change between them.  The marker file records a completed
# build: a binary that exists but was interrupted must not be reused.
if [ ! -f "$STATE/.built" ]; then
    log "compiling the server (first candidate pays for this)"
    rm -f "$BIN"

    # A submission may arrive carrying build output.  Deleting it is not
    # housekeeping: a committed _dist/chartmuseum would otherwise be a binary
    # nobody watched being built, and the container's own Dockerfile copies from
    # exactly that path.
    rm -rf "$TARGET/_dist" "$TARGET/bin" "$TARGET/testbin" 2>/dev/null

    # GOFLAGS=-mod=mod, not -mod=readonly: a submission whose go.mod is one tidy
    # short of correct should be measured on its behaviour, not rejected here for
    # a manifest nit that stage 2 already scored.
    #
    # GOPRIVATE and friends are cleared for the reason the mirror build
    # documents: GOPRIVATE='*' switches Go to direct VCS mode and silently
    # bypasses a file:// proxy, which surfaces as a GitHub 403 inside a
    # container that is supposed to be offline.
    (
        cd "$TARGET" || exit 73
        exec env \
            GOTOOLCHAIN=local \
            GOFLAGS=-mod=mod \
            CGO_ENABLED=0 \
            GOOS=linux \
            GOPROXY="file://$PROXY" \
            GOSUMDB=off \
            GOPRIVATE= \
            GONOSUMDB= \
            GONOSUMCHECK= \
            go build -o "$BIN" ./cmd/chartmuseum
    ) >>"$STATE/build.log" 2>&1 || {
        log "the tree does not compile; see $STATE/build.log"
        tail -25 "$STATE/build.log" >&2
        # A submission that will not build is stage 2's finding, not stage 3's.
        # 71 is in the fault range the adjudicator reads (sysexits 64-78), so the
        # candidate is invalid on whichever tree this happened to: nothing was
        # learned about the submission either way.  Requiring a PASS on the original
        # does not cover this on its own -- a submission that will not build passes
        # there, and the round would read the pair as a divergence.  See the header:
        # it is the case that decided the shared module cache above.
        exit 71
    }
    test -x "$BIN" || { log "go build reported success but produced no binary"; exit 70; }
    touch "$STATE/.built"
fi

# --- 2. Start the server ------------------------------------------------------
# A fresh server, a fresh port and a fresh storage root for every candidate run.
# The binary is cached because compiling it is expensive and a test cannot affect
# it; the server and its storage are not, because a test can: a candidate that
# uploads a chart, fills the index cache or deletes something would otherwise
# change the answer the next candidate gets, and one candidate's finding would
# depend on which candidate ran before it.
PORT="$(python3 - <<'PY'
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY
)"
BASE_URL="http://127.0.0.1:$PORT"

# One run's storage, seeded from the frozen charts so a candidate has something
# to read without having to upload first.  Copied rather than shared: two
# consecutive candidates must not see each other's writes.
RUNROOT="$STATE/run-$PORT"
rm -rf "$RUNROOT" && mkdir -p "$RUNROOT/storage"
cp -a /opt/testdata/. "$RUNROOT/storage/" 2>/dev/null || true

server_up() {
    python3 - "$BASE_URL" <<'PY' >/dev/null 2>&1
import sys, urllib.error, urllib.request
# Readiness is "something is answering HTTP here", not "it answers correctly":
# a submission whose /health is broken is a finding for a candidate to make, not
# a reason to declare the server down and run nothing.
try:
    urllib.request.urlopen(sys.argv[1] + "/health", timeout=2)
except urllib.error.HTTPError:
    pass
except Exception:
    sys.exit(1)
sys.exit(0)
PY
}

SERVER_PID=""
cleanup() {
    [ -n "$SERVER_PID" ] || return 0
    # Negative pid: the whole process group.  The server may spawn helpers, and
    # an orphan still holding a port would leak across candidates.
    kill -TERM "-$SERVER_PID" 2>/dev/null || kill -TERM "$SERVER_PID" 2>/dev/null
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        kill -0 "-$SERVER_PID" 2>/dev/null || return 0
        sleep 0.2
    done
    kill -KILL "-$SERVER_PID" 2>/dev/null || kill -KILL "$SERVER_PID" 2>/dev/null
}
trap cleanup EXIT INT TERM

# The command line, chosen to expose as much of the server as one process can.
#
# The first three flags are the launcher's in stage 2 as well, so the storage
# layout a candidate sees here is the layout the corpus was recorded against.
# The other two open surface that is off by default and would otherwise be
# unreachable: --allow-overwrite makes the upload path re-testable within one
# run, and --enable-metrics turns on the Prometheus endpoint, which is the part
# of this migration that loses the route template the retired framework used to
# supply and is therefore the part most likely to be subtly wrong.
#
# Every flag is a documented flag of the original and both trees get exactly the
# same list.  Deliberately no --depth, no --context-path, no --bearer-auth: those
# change the shape of the whole surface, one process can only have one of each,
# and a candidate that needs a different configuration is asking for a
# configuration matrix -- which is stage 2's job and is where the recorded corpus
# spends most of its cases.
#
# env -i: ChartMuseum's flags are all EnvVar-backed, so a stray CM_* or DEPTH in
# the stage's own environment would silently reconfigure one side of a
# comparison.  Stage 2's launcher scrubs the same prefixes for the same reason;
# here the environment is built from nothing instead, which is stricter and
# cheaper than enumerating what to remove.
log "starting the server on port $PORT"
(
    cd "$TARGET" || exit 73
    exec setsid env -i \
        PATH=/usr/local/bin:/usr/bin:/bin \
        HOME="$RUNROOT" \
        "$BIN" \
            --port="$PORT" \
            --storage=local \
            --storage-local-rootdir="$RUNROOT/storage" \
            --allow-overwrite \
            --enable-metrics
) </dev/null >>"$LOG" 2>&1 &
SERVER_PID=$!

for _ in $(seq 1 100); do
    server_up && break
    # A process that died is not going to start answering later.
    kill -0 "$SERVER_PID" 2>/dev/null || break
    sleep 0.3
done

if ! server_up; then
    log "the server did not answer within 30s; last of $LOG:"
    tail -20 "$LOG" >&2
    # The original failing to boot is a broken image; the submission failing to boot
    # is stage 2's measurement.  Either way no candidate can run, and 74 is in the
    # fault range the adjudicator reads (sysexits 64-78), so it is invalid rather
    # than a verdict on the tree.
    exit 74
fi
log "up on $BASE_URL"

# --- 3. Run the candidate -----------------------------------------------------
# Not exec: the trap has to fire, or the server outlives this run.
# -p no:cacheprovider because both trees share the candidates directory, and a
# .pytest_cache beside the candidate would be the one piece of state that crosses
# between the two halves of a comparison.
#
# The candidate is given a base URL, a directory of charts it may upload, and a
# scratch directory.  Not the tree, not the binary, not the role: it is a
# black-box HTTP test, and probe.toml says so.  Forwarding the role would make
# that claim false and make `os.environ["SRB_TARGET_NAME"] == "original"` a
# ten-point break.
env -u SRB_TARGET_ROLE -u SRB_TARGET_NAME -u SRB_ORIGINAL -u SRB_TARGET \
    SRB_BASE_URL="$BASE_URL" \
    SRB_CHARTS=/opt/testdata \
    SRB_TMP="$RUNROOT/tmp" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=0 \
    /opt/venv/bin/python -m pytest \
        -p no:cacheprovider -q --no-header -x --timeout=120 \
        "$CANDIDATE"
STATUS=$?
log "candidate ${SRB_CANDIDATE_NAME:-?}: pytest exit $STATUS"

# The storage root is this run's, and the next run makes its own.  Removing it
# here rather than at the next start keeps the scratch directory from growing to
# 240 seeded copies over a stage.
rm -rf "$RUNROOT"
exit $STATUS
