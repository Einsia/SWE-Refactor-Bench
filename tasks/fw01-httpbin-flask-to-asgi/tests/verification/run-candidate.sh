#!/usr/bin/env bash
# =============================================================================
# Run one verification candidate against one tree.
#
# Invoked by swerefactor.verification.CandidateRunner, once per (candidate, tree),
# with cwd set to the tree and:
#
#   SRB_CANDIDATE       absolute path to the candidate's pytest file
#   SRB_CANDIDATE_NAME  its short name
#   SRB_TARGET          the tree to test  (a copy: safe to install from)
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
# The role argument selects the wheelhouse -- the two trees genuinely cannot be
# installed the same way -- and stops here.  It is not forwarded to the
# candidate, whose whole claim to be measuring the migration rests on not
# knowing which tree it is talking to.
#
# The two trees are installed and served differently, because they are different
# applications: the original needs Flask, Werkzeug, flasgger, gunicorn and gevent,
# and the submission is forbidden all of them.  Each gets the wheelhouse it
# declares and nothing more, so a submission cannot pass by depending on
# something only the original's environment has.
#
# Both are started through an entry point the repository itself publishes, never
# through a command invented here, and through the same ordered list stage 2 uses:
# httpbin.bash, then a console script from the built distribution, then the
# Procfile's web: line.  Insisting on httpbin.bash by name would have measured the
# name of a file -- and worse, would have made a tree stage 2 could drive
# unattackable here.  If nothing on that list answers, the project publishes no
# working way to start itself, and no candidate can run either way.
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
VENV="$STATE/venv"
LOG="$STATE/server.log"
mkdir -p "$STATE"

case "$ROLE" in
    original)   WHEELS=/opt/wheelhouse-baseline; PINS=/opt/pins/pinned-baseline.txt ;;
    submission) WHEELS=/opt/wheelhouse;          PINS=/opt/pins/pinned-target.txt ;;
    *) echo "unknown target role" >&2; exit 64 ;;
esac

# Belt and braces.  The harness does not set these, but this script may be run
# by hand or from a shell that has them, and an inherited value is inherited
# all the way down to the candidate.
unset SRB_TARGET_ROLE SRB_TARGET_NAME SRB_ORIGINAL

log() { printf '[%s] %s\n' "$ROLE" "$*" >&2; }

# --- 1. Install the tree, once per tree per stage -----------------------------
# Six rounds share this.  Rebuilding a venv for each of ~120 candidate runs
# would spend the stage's budget on pip rather than on finding defects, and the
# tree does not change between them.  The marker file records a completed
# install: a venv that exists but was interrupted must not be reused.
if [ ! -f "$STATE/.installed" ]; then
    log "building the test environment (first candidate pays for this)"
    rm -rf "$VENV"
    python -m venv "$VENV" >>"$STATE/install.log" 2>&1 || {
        log "could not create a virtualenv; see $STATE/install.log"; exit 70; }

    # A submission may arrive carrying its own build output.  Stage 2 scrubs a
    # copy for the same reason: an *.egg-info left behind can make a broken
    # pyproject.toml install cleanly, and a wheel smuggled into the tree could
    # carry a retired dependency.
    find "$TARGET" -depth \
        \( -name '__pycache__' -o -name '*.egg-info' -o -name '.pytest_cache' \
           -o -name '.venv' -o -name 'venv' -o -name '.tox' -o -name 'build' \
           -o -name 'dist' \) -prune -exec rm -rf {} + 2>/dev/null
    find "$TARGET" \( -name '*.pyc' -o -name '*.whl' \) -delete 2>/dev/null

    {
        "$VENV/bin/pip" install --no-cache-dir --no-index \
            --find-links "$WHEELS" -r "$PINS" \
        && "$VENV/bin/pip" install --no-cache-dir --no-index --no-deps \
            --find-links "$WHEELS" "$TARGET"
    } >>"$STATE/install.log" 2>&1 || {
        log "the tree does not install; see $STATE/install.log"
        # A submission that will not install is stage 2's finding, not stage 3's.
        # 71 is in the fault range the adjudicator reads (sysexits 64-78), so the
        # candidate is invalid on whichever tree this happened to: nothing was
        # learned about the submission either way.  Requiring a PASS on the original
        # does not cover this on its own -- a submission that will not install
        # passes there, and the round would read the pair as a divergence.
        exit 71
    }
    # pytest, pytest-timeout and httpx are what a candidate may import.  They go
    # into both venvs at the same versions regardless of what either tree
    # declares: they are the test's dependencies, not the service's, and a
    # candidate has to behave identically on both trees for its result to mean
    # anything.  Installed last and pinned, so they win over a baseline pin of an
    # older pytest without disturbing the service's own closure.
    "$VENV/bin/pip" install --no-cache-dir --no-index \
        --find-links /opt/wheelhouse-candidate \
        -r /opt/pins/pinned-candidate.txt >>"$STATE/install.log" 2>&1 || {
        log "could not install the candidate's test dependencies"; exit 70; }
    touch "$STATE/.installed"
fi

# --- 2. Start the service through its own entry point -------------------------
# A fresh server on a fresh port for every candidate run.  The venv is cached
# because building it is expensive and it cannot be affected by a test; the
# server is not, because it can: a candidate that leaves a connection open, fills
# a cache, or crashes a worker would otherwise change the answer the next
# candidate gets, and one candidate's finding would depend on which candidate ran
# before it.  Starting it costs a second or two.
PORT="$("$VENV/bin/python" - <<'PY'
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY
)"
BASE_URL="http://127.0.0.1:$PORT"

server_up() {
    "$VENV/bin/python" - "$BASE_URL" <<'PY' >/dev/null 2>&1
import sys, urllib.error, urllib.request
# Readiness is "something is answering HTTP here", not "it answers correctly":
# a submission whose /status/200 is broken is a finding for a candidate to make,
# not a reason to declare the server down and run nothing.
try:
    urllib.request.urlopen(sys.argv[1] + "/status/200", timeout=2)
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
    # Negative pid: the whole process group.  uvicorn and gunicorn both fork, and
    # an orphaned worker still holding a port would leak across candidates.
    kill -TERM "-$SERVER_PID" 2>/dev/null || kill -TERM "$SERVER_PID" 2>/dev/null
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        kill -0 "-$SERVER_PID" 2>/dev/null || return 0
        sleep 0.2
    done
    kill -KILL "-$SERVER_PID" 2>/dev/null || kill -KILL "$SERVER_PID" 2>/dev/null
}
trap cleanup EXIT INT TERM

# The candidates, most published first, matching stage 2's list exactly: a tree
# that stage 2 was willing to drive must be drivable here too, or a submission
# whose start-up lives in the console script its own pyproject.toml declares
# would pass stage 2 and then be unattackable for a reason that is not about its
# behaviour.  Only the last is invented here, and reaching it means the project
# publishes no working way to start itself.
#
# Written to a file rather than an array so the reason each attempt failed can be
# logged in order; $LOG is per-tree, and the two trees are compared, so "how it
# started" has to be recoverable for both.
candidates="$STATE/candidates"
: >"$candidates"
if [ -f "$TARGET/httpbin.bash" ]; then
    chmod +x "$TARGET/httpbin.bash" 2>/dev/null
    printf 'httpbin.bash\tbash ./httpbin.bash\n' >>"$candidates"
fi
# Whatever the built distribution installed as a console script.  Read from the
# venv's bin/, which is the build's own output rather than the tree's.
for name in httpbin httpbin-server serve-httpbin; do
    if [ -x "$VENV/bin/$name" ]; then
        printf 'console script: %s\t%s\n' "$name" "$VENV/bin/$name" >>"$candidates"
    fi
done
if [ -f "$TARGET/Procfile" ]; then
    web="$(sed -n 's/^[[:space:]]*web:[[:space:]]*//p' "$TARGET/Procfile" | head -1)"
    [ -n "$web" ] && printf 'Procfile web:\t%s\n' "$web" >>"$candidates"
fi
printf 'harness fallback\t%s -m uvicorn httpbin:app --host 127.0.0.1 --port %s\n' \
    "$VENV/bin/python" "$PORT" >>"$candidates"

STARTED=""
while IFS="$(printf '\t')" read -r how argv; do
    [ -n "$argv" ] || continue
    log "starting the service on port $PORT via $how"
    (
        cd "$TARGET" || exit 73
        # setsid so cleanup can signal the group rather than one shell.
        # -c, not -lc: a login shell sources /etc/profile, which would reset the
        # PATH that puts this tree's venv first.
        # HOST/PORT as well as the HTTPBIN_ pair, because stage 2 passes both and
        # a console script that reads the generic pair would otherwise bind 8080
        # here, fall through, and make the two stages disagree about the tree.
        exec setsid env \
            PATH="$VENV/bin:$PATH" \
            VIRTUAL_ENV="$VENV" \
            HTTPBIN_HOST=127.0.0.1 \
            HTTPBIN_PORT="$PORT" \
            HOST=127.0.0.1 \
            PORT="$PORT" \
            bash -c "$argv"
    # stdin from /dev/null: this loop's stdin is the candidate list, and a server
    # that read it would eat the entries not yet tried.
    ) </dev/null >>"$LOG" 2>&1 &
    SERVER_PID=$!

    for _ in $(seq 1 100); do
        server_up && break
        # A candidate that died is not going to start answering later.
        kill -0 "$SERVER_PID" 2>/dev/null || break
        sleep 0.3
    done
    if server_up; then
        STARTED="$how"
        break
    fi
    log "$how did not answer; trying the next published entry point"
    cleanup
    SERVER_PID=""
done <"$candidates"

if [ -z "$STARTED" ]; then
    log "no published entry point answered, each given 30s; last of $LOG:"
    tail -20 "$LOG" >&2
    # The original failing to boot is a broken image; the submission failing to boot
    # is stage 2's measurement.  Either way no candidate can run, and 74 is in the
    # fault range the adjudicator reads (sysexits 64-78), so it is invalid rather
    # than a verdict on the tree.
    exit 74
fi
log "up on $BASE_URL via $STARTED"

# --- 3. Run the candidate -----------------------------------------------------
# Not exec: the trap has to fire, or the server outlives this run.
# -p no:cacheprovider because both trees share the candidates directory, and a
# .pytest_cache beside the candidate would be the one piece of state that crosses
# between the two halves of a comparison.
# The candidate is given a base URL and nothing else.  It is a black-box HTTP
# test, and probe.toml says so; forwarding the role would make that claim false
# and make `os.environ["SRB_TARGET_NAME"] == "original"` a ten-point break.
env -u SRB_TARGET_ROLE -u SRB_TARGET_NAME -u SRB_ORIGINAL \
    SRB_BASE_URL="$BASE_URL" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=0 \
    "$VENV/bin/python" -m pytest \
        -p no:cacheprovider -q --no-header -x --timeout=120 \
        "$CANDIDATE"
STATUS=$?
log "candidate ${SRB_CANDIDATE_NAME:-?}: pytest exit $STATUS"
exit $STATUS
