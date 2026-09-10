#!/usr/bin/env bash
# =============================================================================
# Run one verification candidate against one tree.
#
# Invoked by swerefactor.verification.CandidateRunner, once per (candidate, tree),
# with cwd set to the tree and:
#
#   SRB_CANDIDATE       absolute path to the candidate's pytest file
#   SRB_CANDIDATE_NAME  its short name
#   SRB_TARGET          the tree to test  (a copy: safe to install into)
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
# The role argument selects the registry mirror and stops here.  The two trees
# genuinely cannot be installed from the same registry: State A needs express,
# body-parser, compression, cors, morgan, method-override, express-urlrewrite and
# errorhandler, and the submission is forbidden all of them.  So the original
# installs from the baseline mirror and the submission from the delivery mirror --
# the same one stage 2 uses, which has no packument for any of the 54 retired
# names and answers 404.  A submission cannot pass here by reaching for something
# only the original's registry has, and stage 2 has already measured whether it
# installs against the delivery mirror alone.
#
# Everything else is deliberately identical between the two trees:
#
#   * the same seed database and the same rewrite-rule file, both the grader's
#     copies.  A submission that edited its own db.seed.json would otherwise
#     change every candidate's answer for a reason that is not the migration;
#   * static file mtimes pinned to one epoch on both trees, because Express
#     derives a static file's ETag and Last-Modified from size and mtime, and two
#     separate checkouts disagree about mtime for filesystem reasons.  Pinning
#     rather than excluding: the static surface stays attackable, and only the
#     clock stops being a difference;
#   * the same Python interpreter and the same pytest, built once into the image.
#     A candidate running under two different pytests would not be comparing two
#     services.
#
# Both trees are started through a launcher the repository itself publishes, never
# through a command invented here, and through the same ordered list stage 2 uses:
# serve.sh first, then the executable package.json declares in "bin".  Insisting
# on serve.sh by name would have measured the name of a file -- and worse, would
# have made a tree stage 2 could drive unattackable here.  If nothing on that list
# answers, the project publishes no working way to start itself, and no candidate
# can run either way.
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

# Keyed by the token, not the role: $STATE has to be assumed reachable from the
# candidate, and a directory named "original" would answer the question the
# candidate is supposed to answer by observing behaviour.
STATE="$WORK/$TOKEN"
TREE="$STATE/tree"
LOG="$STATE/server.log"
mkdir -p "$STATE"

# The mtime every static file on both trees is given.  Fixed, not derived from
# either checkout: the point is that the two trees agree.  2024-01-01T00:00:00Z,
# the same instant the mirror's packuments carry.
STATIC_EPOCH=1704067200

case "$ROLE" in
    original)   MIRROR=/opt/npm-registry-baseline; NPM_CACHE=/opt/npm-cache-baseline ;;
    submission) MIRROR=/opt/npm-registry;          NPM_CACHE=/opt/npm-cache ;;
    *) echo "unknown target role" >&2; exit 64 ;;
esac

# Belt and braces.  The harness does not set these, but this script may be run by
# hand or from a shell that has them, and an inherited value is inherited all the
# way down to the candidate.
unset SRB_TARGET_ROLE SRB_TARGET_NAME SRB_ORIGINAL

log() { printf '[%s] %s\n' "$ROLE" "$*" >&2; }

# --- 1. Stage, install and build the tree, once per tree per stage -------------
# Six rounds share this.  Reinstalling 1064 packages and re-running babel for
# each of ~120 candidate runs would spend the stage's budget on npm rather than on
# finding defects, and the tree does not change between them.  The marker file
# records a *completed* install: a node_modules that exists but was interrupted
# must not be reused.
if [ ! -f "$STATE/.installed" ]; then
    log "staging, installing and building the tree (first candidate pays for this)"
    rm -rf "$TREE"
    mkdir -p "$TREE"

    # A copy, and not the tree the adversary reads.  Two reasons, and the second
    # is the one that matters: an install writes node_modules and a build writes
    # lib/, and doing either in the tree the model has open would change what it
    # sees between one try_test and the next.
    #
    # The copy drops everything a build produces, for the same reason stage 2's
    # `install` module does: a vendored node_modules could carry express past a
    # registry that refuses to serve it, and that is the one cheat this stage
    # would otherwise not see, because a mounted Express app answers every
    # request correctly.
    if ! tar -C "$TARGET" \
            --exclude='./node_modules' \
            --exclude='./.npm' \
            --exclude='./.cache' \
            --exclude='./.yarn' \
            --exclude='./.pnpm-store' \
            --exclude='./.git' \
            --exclude='./.nyc_output' \
            --exclude='./coverage' \
            --exclude='*.tgz' \
            -cf - . 2>>"$STATE/install.log" | tar -C "$TREE" -xf - ; then
        log "could not stage the tree; see $STATE/install.log"
        exit 70
    fi

    if [ ! -f "$TREE/package.json" ]; then
        log "no package.json in the tree: there is no Node package here to install"
        exit 71
    fi

    # `npm ci` when there is a lockfile -- the reproducible path, and the stricter
    # of the two: it refuses a lockfile that disagrees with package.json rather
    # than quietly fixing it.  Same rule stage 2 applies.
    if [ -f "$TREE/package-lock.json" ]; then NPM_VERB=ci; else NPM_VERB=install; fi
    log "npm $NPM_VERB against $(basename "$MIRROR")"
    if ! ( cd "$TREE" && env \
                SRB_REGISTRY_ROOT="$MIRROR" \
                npm_config_cache="$NPM_CACHE" \
                SRB_NPM_LOGLEVEL=warn \
                /usr/local/bin/srb-npm "$NPM_VERB" ) >>"$STATE/install.log" 2>&1; then
        log "the tree does not install; see $STATE/install.log"
        tail -30 "$STATE/install.log" >&2
        # A submission that will not install is stage 2's finding, not stage 3's.
        # 71 is in the fault range the adjudicator reads (sysexits 64-78), so the
        # candidate is invalid on whichever tree this happened to: nothing was
        # learned about the submission either way.  Requiring a PASS on the original
        # does not cover this on its own -- a submission that will not install
        # passes there, and the round would read the pair as a divergence.
        exit 71
    fi

    # The build, if the package declares one.  Not fatal: a submission may boot
    # from sources it committed rather than from a build product, and whether it
    # boots is decided below by starting it.
    if ( cd "$TREE" && node -e \
            'process.exit(((require("./package.json").scripts)||{}).build ? 0 : 1)' \
        ) 2>/dev/null; then
        log "npm run build"
        ( cd "$TREE" && npm run build --loglevel warn ) \
            >>"$STATE/build.log" 2>&1 \
            || log "the build script failed; see $STATE/build.log (not fatal)"
    fi

    # Every static file on both trees gets one mtime.  Express answers
    # W/"<size-hex>-<mtime-ms-hex>" and a Last-Modified from the same stat, so
    # without this the two trees would differ on every conditional request to a
    # static file for a reason that has nothing to do with either framework.
    for root in public altpublic __fixtures__; do
        [ -d "$TREE/$root" ] && find "$TREE/$root" -type f \
            -exec touch -h -d "@$STATIC_EPOCH" {} + 2>/dev/null
    done

    touch "$STATE/.installed"
fi

# --- 2. Start the service through its own entry point -------------------------
# A fresh server on a fresh port for every candidate run.  The install is cached
# because it is expensive and a test cannot affect it; the server is not, because
# a test can: json-server mutates its database in place, and a candidate that
# POSTs a row would otherwise change the answer the next candidate gets, so one
# candidate's finding would depend on which candidate ran before it.  Starting it
# costs a second.
PORT="$(/opt/venv-candidate/bin/python - <<'PY'
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY
)"
BASE_URL="http://127.0.0.1:$PORT"

server_up() {
    /opt/venv-candidate/bin/python - "$BASE_URL" <<'PY' >/dev/null 2>&1
import sys, urllib.error, urllib.request
# Readiness is "something is answering HTTP here", not "it answers correctly": a
# submission whose /posts is broken is a finding for a candidate to make, not a
# reason to declare the server down and run nothing.
try:
    urllib.request.urlopen(sys.argv[1] + "/posts", timeout=2)
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
    # Negative pid: the whole process group.  serve.sh execs node, but a
    # submission's launcher may fork, and an orphan still holding a port would
    # leak into the next candidate.
    kill -TERM "-$SERVER_PID" 2>/dev/null || kill -TERM "$SERVER_PID" 2>/dev/null
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        kill -0 "-$SERVER_PID" 2>/dev/null || return 0
        sleep 0.2
    done
    kill -KILL "-$SERVER_PID" 2>/dev/null || kill -KILL "$SERVER_PID" 2>/dev/null
}
trap cleanup EXIT INT TERM

# The grader's data, on both trees.  A fresh working copy per run, because the
# service writes to it; the seed itself stays pristine.
SEED=/opt/srb/db.seed.json
ROUTES=/opt/srb/routes.json
DB="$STATE/db-$PORT.json"
cp -f "$SEED" "$DB" && chmod u+w "$DB"

# The entry points, most published first, matching stage 2's list: a tree stage 2
# was willing to drive must be drivable here too, or a submission whose start-up
# lives in the executable its own package.json declares would pass stage 2 and
# then be unattackable for a reason that is not about its behaviour.  Only the
# last is assembled here, and it is assembled out of the tree's own "bin" -- the
# same resolution serve.sh performs -- rather than out of a path guessed by the
# grader.
#
# Written to a file rather than an array so the reason each attempt failed can be
# logged in order; $LOG is per-tree and the two trees are compared, so "how it
# started" has to be recoverable for both.
attempts="$STATE/attempts"
: >"$attempts"
if [ -f "$TREE/serve.sh" ]; then
    chmod +x "$TREE/serve.sh" 2>/dev/null
    printf 'serve.sh\tbash ./serve.sh\n' >>"$attempts"
fi
BIN="$(cd "$TREE" && node -e 'const b=(require("./package.json")||{}).bin;
process.stdout.write(!b ? "" : (typeof b === "string" ? b : (Object.values(b)[0]||"")));' \
    2>/dev/null)"
if [ -n "$BIN" ] && [ -f "$TREE/$BIN" ]; then
    printf 'declared bin: %s\tnode %s "$JSON_SERVER_DB" --host 127.0.0.1 --port %s --routes %s\n' \
        "$BIN" "$BIN" "$PORT" "$ROUTES" >>"$attempts"
fi

STARTED=""
while IFS="$(printf '\t')" read -r how argv; do
    [ -n "$argv" ] || continue
    log "starting the service on port $PORT via $how"
    (
        cd "$TREE" || exit 73
        # setsid so cleanup can signal the group rather than one shell.
        # -c, not -lc: a login shell sources /etc/profile and would reset PATH.
        # NODE_ENV cleared: a submission's own NODE_ENV must not change behaviour
        # here when it did not in stage 2.
        exec setsid env \
            JSON_SERVER_HOST=127.0.0.1 \
            JSON_SERVER_PORT="$PORT" \
            JSON_SERVER_SEED="$SEED" \
            JSON_SERVER_DB="$DB" \
            JSON_SERVER_ROUTES="$ROUTES" \
            NODE_ENV= \
            TZ=UTC \
            LC_ALL=C.UTF-8 \
            bash -c "$argv"
    # stdin from /dev/null: this loop's stdin is the attempt list, and a server
    # that read it would eat the entries not yet tried.
    ) </dev/null >>"$LOG" 2>&1 &
    SERVER_PID=$!

    for _ in $(seq 1 100); do
        server_up && break
        # A launcher that died is not going to start answering later.
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
done <"$attempts"

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
#
# `env -i` and not `env -u`: an allow-list is the only form of this that stays
# correct when the harness adds a variable.  The candidate is given a base URL, a
# PATH, a HOME it can write to, a locale, and SRB_NETWORK -- which is the same on
# both trees and so cannot be used to tell them apart.  Everything else about how
# this run was set up, including SRB_TARGET and SRB_TARGET_TOKEN, stops here.
# probe.toml says a candidate must not learn which tree it is on; this is the half
# of that claim the runner can actually enforce, and the reason the other half is a
# deny clause is that a process with a filesystem can always look around.
#
# -p no:cacheprovider because both trees are handed the same candidate file, and a
# .pytest_cache beside it would be the one piece of state that crosses between the
# two halves of a comparison.
#
# cwd is an empty scratch directory rather than the tree: the candidate has no
# business reading the repository, and it should not be handed one by accident.
RUNDIR="$STATE/cwd"
rm -rf "$RUNDIR" && mkdir -p "$RUNDIR"
cd "$RUNDIR" || exit 73

env -i \
    PATH=/usr/local/bin:/usr/bin:/bin \
    HOME="$RUNDIR" \
    TMPDIR="$RUNDIR" \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=UTC \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=0 \
    SRB_BASE_URL="$BASE_URL" \
    SRB_NETWORK="${SRB_NETWORK:-denied}" \
    /opt/venv-candidate/bin/python -m pytest \
        -p no:cacheprovider -q --no-header -x --timeout=120 \
        "$CANDIDATE"
STATUS=$?
log "candidate ${SRB_CANDIDATE_NAME:-?}: pytest exit $STATUS"
exit $STATUS
