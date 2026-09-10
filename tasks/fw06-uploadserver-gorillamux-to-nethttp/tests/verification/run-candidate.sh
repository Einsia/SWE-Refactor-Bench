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
# to know and not the candidate's.
#
# Exit 0 means the candidate passed against this tree; non-zero means it failed.
# That is the whole contract, and it is why a broken candidate reads as "does not
# pass on the original" rather than as a defect in the submission.
#
# The role argument selects the module mirror -- the original needs an archive of
# the retired router and the submission must not have one -- and stops here.  It is
# not forwarded to the candidate, whose whole claim to be measuring the migration
# rests on not knowing which tree it is talking to.
#
# Both trees are built by `go build` from their own go.mod, and served on loopback
# under the same four flag profiles stage 2 drives.  Neither is built by a command
# invented here: the package path comes from the tree's own module graph.
# =============================================================================
set -uo pipefail

TARGET="${SRB_TARGET:?SRB_TARGET is not set}"
# Passed as this script's last argument, never in the environment: a child process
# inherits the environment automatically and inherits argv never, so the role
# cannot reach the candidate by being forgotten about.
ROLE="${1:?the target role was not passed as an argument}"
TOKEN="${SRB_TARGET_TOKEN:?SRB_TARGET_TOKEN is not set}"
CANDIDATE="${SRB_CANDIDATE:?SRB_CANDIDATE is not set}"
WORK="${SRB_WORK:-/tmp/srb-verification}"

# Keyed by the token, not the role: $STATE is reachable from the candidate through
# the paths it is handed, and a directory named "original" would answer the
# question the candidate is supposed to answer by observing behaviour.
STATE="$WORK/$TOKEN"
BIN="$STATE/server"
mkdir -p "$STATE"

case "$ROLE" in
    # Complete: carries an archive of the retired router, because State A imports
    # it and must build.
    original)   PROXY="file:///opt/goproxy-baseline" ;;
    # Pruned: serves the retired router's .mod so `go mod tidy` can still resolve
    # a version, and no .zip, so a surviving *import* cannot compile.  This is the
    # same mirror stage 2 uses, and the reason a fake port fails to build here.
    submission) PROXY="file:///opt/goproxy" ;;
    *) echo "unknown target role" >&2; exit 64 ;;
esac

# Belt and braces.  The harness does not set these, but this script may be run by
# hand or from a shell that has them, and an inherited value is inherited all the
# way down to the candidate.
unset SRB_TARGET_ROLE SRB_TARGET_NAME SRB_ORIGINAL

log() { printf '[%s] %s\n' "$TOKEN" "$*" >&2; }

# An infrastructure fault: this tree could not be built or booted, so no candidate
# can be run against it.  Marked, not converted into a verdict, and the reason is
# worth being precise about.
#
# The adjudicator's only signal is the exit status, and `discriminates` is
# `orig.passed and not sub.passed`.  A fault on the submission side is therefore
# indistinguishable from a real divergence, and a PERSISTENT one reproduces across
# all three reruns and gets upheld -- ten points taken off a submission for a
# defect it does not have.  The obvious fix, failing both roles, inverts the error
# into six survivals and pays the full 60 for nothing.  Both directions are
# wrong, and neither can be chosen from inside this script, which sees one tree.
#
# So the fault is made loud instead: a fixed prefix on stderr, which the adjudicator
# records verbatim in the round transcript, and a file under $SRB_WORK, which is
# shared across roles and rounds and outlives them.  score.py reads those files and
# prints a warning above the verdict naming every one; it does not change the score,
# because a fault can be evidence in either direction and choosing one from here
# would be guessing.  What it does is make the guess a reviewer's to make, with the
# tree, the exit code and the reason in front of them.
#
# Nothing here leaks the role -- stderr never reaches the candidate, and the
# adversary is already told both outcomes by try_test.
fault() {
    local code="$1"; shift
    log "SRB-INFRA-FAULT: $*"
    mkdir -p "$WORK/faults" 2>/dev/null
    printf '%s\texit %s\t%s\n' "$TOKEN" "$code" "$*" \
        >>"$WORK/faults/$TOKEN.$$" 2>/dev/null
    exit "$code"
}

# --- 1. Build the tree, once per tree per stage -------------------------------
# Six rounds share this.  Rebuilding for each of ~120 candidate runs would
# spend the stage's budget on the compiler rather than on finding defects, and the
# tree does not change between them.  The marker file records a COMPLETED build: a
# binary that exists because an interrupted build left one must not be reused.
if [ ! -f "$STATE/.built" ]; then
    log "building the tree (the first candidate of a round pays for this)"
    rm -f "$BIN"

    # A submission may arrive carrying its own build output.  A vendor/ directory
    # would be consulted ahead of the mirror and could carry the retired router
    # straight past the dependency gate, which is stage 2's gate to enforce and
    # not something stage 3 should quietly undo.
    rm -rf "$TARGET/vendor"

    # Which package holds main is read from the tree, not assumed.  A submission
    # is free to move it; stage 2 resolves it the same way, and a tree stage 2 was
    # willing to build must be buildable here or it would become unattackable for
    # a reason that is not about its behaviour.
    MAIN_PKG="$(
        cd "$TARGET" && env GOFLAGS=-mod=mod GOPROXY="$PROXY" GOSUMDB=off \
            GOTOOLCHAIN=local GOCACHE="$STATE/gocache" GOMODCACHE="$STATE/gomod" \
            go list -f '{{if eq .Name "main"}}{{.ImportPath}}{{end}}' ./... \
            2>>"$STATE/build.log" | head -1
    )"
    if [ -z "$MAIN_PKG" ]; then
        # Not a finding: a tree that cannot be built cannot be attacked either.
        # 71 is in the fault range the adjudicator reads (sysexits 64-78), so the
        # candidate is invalid whichever tree this was.  The `faults` marker below
        # is the older half of the same defence and stays: it records WHICH tree,
        # for the score report, where the adjudicator only needs to know that one
        # of them could not be tested.
        fault 71 "no package named main in the tree; see $STATE/build.log"
    fi

    if ! (
        cd "$TARGET" && env GOFLAGS=-mod=mod GOPROXY="$PROXY" GOSUMDB=off \
            GONOSUMDB='*' GOPRIVATE='' GONOSUMCHECK=1 GOTOOLCHAIN=local \
            GOCACHE="$STATE/gocache" GOMODCACHE="$STATE/gomod" \
            go build -o "$BIN" "$MAIN_PKG"
    ) >>"$STATE/build.log" 2>&1; then
        tail -20 "$STATE/build.log" >&2
        # A submission that will not build is stage 2's measurement, recorded there
        # as a zero.  Here it only means no candidate can run.
        fault 71 "the tree does not build; see $STATE/build.log"
    fi
    [ -x "$BIN" ] || fault 71 "build reported success but produced no binary"
    touch "$STATE/.built"
fi

# --- 2. Boot one server per flag profile -------------------------------------
# Four, because this server's behaviour is flag-dependent and a candidate confined
# to the default configuration could attack neither -enable_auth nor
# -max_upload_size, which is a third of the surface.
#
# The four sets are stage 2's `corpus.PROFILES`, verbatim, and inventing a fifth
# here is a mistake with a specific and silent cost.  A draft of this file carried
# a `-read_only` profile; there is no such flag -- read-only is a token class under
# -enable_auth, not a flag -- and `flag.ExitOnError` means that server exits at
# startup, `wait_ready` fails, this script exits 74, and EVERY candidate fails on
# both trees.  The adjudicator would then uphold nothing, all six rounds would
# record a survival, and the submission would collect the full 60 points without
# being attacked once.  So: profiles come from stage 2's list, and a flag that is
# not in the original's FlagSet does not get one.
#
# Rebuilt for every candidate run, unlike the binary.  Uploads mutate a docroot,
# and a shared one would make a candidate's result depend on which candidate ran
# before it -- and worse, on the ORDER, which differs between the two trees only
# by accident.  Booting four Go servers costs well under a second.
RUN="$STATE/run.$$"
rm -rf "$RUN"; mkdir -p "$RUN"

FIXTURES="${SRB_FIXTURE_ROOT:-/opt/fixtures/docroot}"
FIXED_MTIME="${SRB_FIXTURE_MTIME:-2026-01-01 00:00:00 UTC}"

PIDS=()
cleanup() {
    for pid in ${PIDS+"${PIDS[@]}"}; do
        [ -n "$pid" ] || continue
        # Negative pid: the whole process group, so a server that forked leaves
        # nothing holding a port across candidates.
        kill -TERM "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null
    done
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        local alive=0
        for pid in ${PIDS+"${PIDS[@]}"}; do
            kill -0 "$pid" 2>/dev/null && alive=1
        done
        [ "$alive" = 0 ] && return 0
        sleep 0.2
    done
    for pid in ${PIDS+"${PIDS[@]}"}; do
        kill -KILL "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null
    done
}
trap cleanup EXIT INT TERM

free_port() { python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()'; }

# Readiness is OPTIONS /upload -> exactly 204, which is stage 2's probe too.  Not
# a GET: under -enable_auth an unauthenticated GET is 401, which would make the
# probe fail on a perfectly healthy server.  And exactly 204, because a 404 from a
# mis-built binary is also a response -- a probe that accepts any status proves
# only that something is listening.
wait_ready() {
    local port="$1" pid="$2"
    for _ in $(seq 1 200); do
        kill -0 "$pid" 2>/dev/null || return 1
        if [ "$(python3 - "$port" <<'PY'
import socket, sys
try:
    with socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=2) as s:
        s.sendall(b"OPTIONS /upload HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        head = b""
        while b"\r\n" not in head and len(head) < 200:
            chunk = s.recv(200)
            if not chunk:
                break
            head += chunk
    print(head.split(b"\r\n", 1)[0].split(b" ")[1].decode() if b" " in head else "")
except Exception:
    print("")
PY
)" = "204" ]; then
            return 0
        fi
        sleep 0.1
    done
    return 1
}

# profile<TAB>flags -- stage 2's corpus.PROFILES, in the same order.
PROFILE_TABLE="$STATE/profiles"
cat >"$PROFILE_TABLE" <<'EOF'
default
nocors	-enable_cors=false
auth	-enable_auth -read_only_tokens ro1 -read_write_tokens rw1
maxsize	-max_upload_size 16
EOF

SERVERS_JSON="$RUN/servers.json"
: >"$RUN/ports"
while IFS="$(printf '\t')" read -r profile flags; do
    [ -n "$profile" ] || continue
    docroot="$RUN/$profile/docroot"
    mkdir -p "$RUN/$profile"
    # Copied WITHOUT -p and then stamped.  git records no mtimes, so a checkout's
    # times are whenever the clone happened; inheriting them would make
    # Last-Modified and If-Modified-Since differ between this image and stage 2's,
    # and a candidate asserting on a fixture's timestamp would find a divergence
    # that is an artifact of the build rather than of the migration.
    cp -r "$FIXTURES" "$docroot"
    find "$docroot" -depth -exec touch -h -d "$FIXED_MTIME" {} + 2>/dev/null

    port="$(free_port)"
    # shellcheck disable=SC2086  # $flags is a deliberate word list
    ( cd "$TARGET" && exec setsid "$BIN" -document_root "$docroot" \
        -addr "127.0.0.1:$port" $flags ) </dev/null \
        >>"$RUN/$profile/server.log" 2>&1 &
    pid=$!
    PIDS+=("$pid")
    if ! wait_ready "$port" "$pid"; then
        tail -20 "$RUN/$profile/server.log" >&2
        # Same reasoning as a failed build: not a finding, just a tree no
        # candidate can be run against.
        fault 74 "the $profile server never answered OPTIONS /upload with 204"
    fi
    printf '%s\t%s\t%s\n' "$profile" "$port" "$docroot" >>"$RUN/ports"
done <"$PROFILE_TABLE"

python3 - "$RUN/ports" "$SERVERS_JSON" <<'PY'
import json, sys
rows = [l.rstrip("\n").split("\t") for l in open(sys.argv[1]) if l.strip()]
json.dump({p: {"host": "127.0.0.1", "port": int(n), "docroot": d}
           for p, n, d in rows}, open(sys.argv[2], "w"), indent=2)
PY
log "up: $(cut -f1 "$RUN/ports" | tr '\n' ' ')"

# --- 3. Run the candidate ----------------------------------------------------
# Not exec: the trap has to fire, or five servers outlive this run.
#
# cwd is a fresh empty directory, NOT the tree.  CandidateRunner sets its cwd to
# the tree, and inheriting that would put the whole source under `open("go.mod")`
# -- which answers "am I on the original?" in one line, without asking the running
# server anything.  probe.toml's scope rejects such a candidate on sight, but a
# rule enforced only by an adjudicator's judgement is a rule that gets enforced
# late; this one is cheap to enforce mechanically, so it is.  $SRB_TARGET is
# scrubbed for the same reason, since it is the tree's path written down.
#
# -p no:cacheprovider because both trees share the candidates directory, and a
# .pytest_cache beside the candidate would be the one piece of state crossing
# between the two halves of a comparison.
NEUTRAL="$RUN/cwd"
mkdir -p "$NEUTRAL"
cd "$NEUTRAL" || fault 70 "cannot enter the neutral working directory $NEUTRAL"

env -u SRB_TARGET -u SRB_TARGET_ROLE -u SRB_TARGET_NAME -u SRB_ORIGINAL \
    SRB_SERVERS="$SERVERS_JSON" \
    SRB_FIXTURE_MTIME="$FIXED_MTIME" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=0 \
    python3 -m pytest \
        -p no:cacheprovider -q --no-header -x --timeout=120 \
        -p srbcandidate \
        "$CANDIDATE"
STATUS=$?
log "candidate ${SRB_CANDIDATE_NAME:-?}: pytest exit $STATUS"
exit $STATUS
