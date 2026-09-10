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
# The role does not reach the candidate. A candidate that reads the role
# satisfies every mechanical condition for a break -- passes on the original,
# fails on the submission, three times identically -- while establishing nothing
# about the migration, so this script drops it and uses SRB_TARGET_TOKEN for
# anything a candidate can observe. infra/tests/test_verification.py asserts that
# it is gone.
#
# Unlike the other tasks' stage 3, this script does NOT build anything. The two
# trees here do not have the same build system -- the original is configured with
# `./configure`, the submission with `cmake` -- and a configuration is therefore
# something a candidate asks for by name:
#
#     t = srbsodium.tree("minimal")
#
# srbsodium then configures, builds and installs that configuration in whichever
# dialect the tree in front of it speaks, and caches the result under
# $SRB_STATE. That is the one place in this stage that knows there are two build
# systems, and it is not reachable from a candidate: what a candidate sees is an
# install prefix.
#
# The consequence for this script is that its job is preparation and nothing
# else -- pristine manifest, environment, pytest -- and the first candidate in the
# stage pays for whichever configurations the rounds actually ask for rather than
# for all of them up front.
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

case "$ROLE" in
    original|submission) ;;
    *) echo "unknown target role" >&2; exit 64 ;;
esac

# Belt and braces.  The harness does not set these, but this script may be run
# by hand or from a shell that has them, and an inherited value is inherited
# all the way down to the candidate.
unset SRB_TARGET_ROLE SRB_TARGET_NAME SRB_ORIGINAL

# Keyed by the token, not the role: the candidate is handed paths under $STATE
# and a directory named "original" would answer the question for it.
log() { printf '[%s] %s\n' "$ROLE" "$*" >&2; }

STATE="$WORK/$TOKEN"
mkdir -p "$STATE"

# --- 1. Scrub build output the submission arrived carrying --------------------
# Once per tree, before the manifest below is taken. A stale build/ with a
# CMakeCache.txt pointing at another prefix makes the first configure reuse
# decisions nobody here made, and a committed .libs/ could hold objects from a
# build this container never ran. Stage 2 scrubs its own copy for the same reason.
#
# Autotools output is scrubbed as well, which matters here in a way it does not in
# stage 2: this stage builds the ORIGINAL too, and a `configure` left over from
# some earlier run would be used in place of the one autogen.sh should produce.
#
# Every directory is named in full and none is matched by a glob. `build-*` looks
# like the obvious way to catch build-release/ and build-debug/, and it also
# catches build-aux/ -- which is not build output at all. It is where libsodium
# keeps ltmain.sh, config.guess, install-sh and depcomp, all of them delivered
# files, and deleting it makes libtoolize reinstall its own ltmain.sh over the
# one the tree shipped. The tree's m4/libtool.m4 is then a different libtool
# version from build-aux/ltmain.sh, the two refuse to work together, and the
# ORIGINAL stops building -- which would silently make every candidate in the
# stage "fail on the original" and hand the submission all 60 points.
if [ ! -f "$STATE/.scrubbed" ]; then
    find "$TARGET" -maxdepth 3 -depth -type d \
        \( -name build -o -name Build -o -name _build -o -name builddir \
           -o -name build-cmake -o -name build-release -o -name build-debug \
           -o -name cmake-build -o -name cmake-build-debug \
           -o -name cmake-build-release -o -name CMakeFiles \
           -o -name _CPack_Packages -o -name Testing \
           -o -name .libs -o -name .deps -o -name autom4te.cache \) \
        -prune -exec rm -rf {} + 2>/dev/null
    # Files: recursive, because objects and libtool wrappers live beside the
    # sources that produced them, several directories down.
    find "$TARGET" -type f \
        \( -name CMakeCache.txt -o -name cmake_install.cmake \
           -o -name CTestTestfile.cmake -o -name install_manifest.txt \
           -o -name build.ninja -o -name rules.ninja -o -name .ninja_log \
           -o -name .ninja_deps -o -name compile_commands.json \
           -o -name config.status -o -name config.log -o -name libtool \
           -o -name '*.o' -o -name '*.lo' -o -name '*.la' -o -name '*.a' \
           -o -name '*.so' -o -name '*.so.*' \) \
        -delete 2>/dev/null
    # The top-level generated configure, and nothing else Autotools generates.
    # autogen.sh exits early when it finds a configure, so a stale one is the file
    # that would be used in place of the one this stage means to generate. The
    # aclocal.m4 and Makefile.in files beside it are rewritten by the same run.
    rm -f "$TARGET/configure" 2>/dev/null
    touch "$STATE/.scrubbed"
fi

# --- 2. The pristine manifest ------------------------------------------------
# Taken once per tree, after the scrub and before any build. Tree.source_tree_dirty()
# is the difference from it, so it has to be older than every build in the stage:
# taken per configuration instead, the second configuration would see the first
# one's output as part of the delivered tree.
PRISTINE="$STATE/pristine.txt"
if [ ! -f "$PRISTINE" ]; then
    ( cd "$TARGET" && find . -type f -not -path './.git/*' -printf '%P\n' \
        | LC_ALL=C sort ) > "$PRISTINE"
    log "$(wc -l <"$PRISTINE") files delivered"
fi

# --- 3. Hand the candidate its environment -----------------------------------
export SRB_STATE="$STATE/configs"
export SRB_PRISTINE_MANIFEST="$PRISTINE"
export SRB_TARGET="$TARGET"
export SRB_TARGET_TOKEN="$TOKEN"
export SRB_SCRATCH="$STATE/candidates/${SRB_CANDIDATE_NAME:-candidate}"

# A fresh scratch per (candidate, tree): a helper compiled against the original
# must not still be sitting there when the same candidate runs against the
# submission, and an extracted archive member from one must not be mistaken for
# the other's.
rm -rf "$SRB_SCRATCH"
mkdir -p "$SRB_SCRATCH" "$SRB_STATE"

# --- 4. Run the candidate ----------------------------------------------------
# -p no:cacheprovider because both trees share the candidates directory, and a
# .pytest_cache beside the candidate would be the one piece of state that crosses
# between the two halves of a comparison.
#
# No -x: a candidate here often asserts several properties of one configuration,
# and the configuration cost minutes to build. Letting the rest of the file run
# after the first failure gives the adjudicator the whole picture for the same
# money.
#
# The timeout is per test and generous, because a test that asks for a
# configuration triggers a build of it. A candidate whose subject IS a hang has
# srbsodium's own per-command timeouts, which return rather than raise.
#
# -p srbfault is where this task's "the tree does not build" defence lives, and it
# is a plugin rather than a check in this script because the build happens inside
# the process started below: srbsodium.tree() configures, builds and installs, and
# raises BuildFailed there. The plugin turns a BuildFailed that no candidate caught
# into exit 71, so $STATUS below carries it and the adjudicator reads the tree as
# untestable rather than as a divergence. Every other task in this benchmark builds
# in its run-candidate.sh and writes `exit 71` in the failure branch directly; see
# lib/srbfault.py for why the two spellings mean the same thing.
env \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=0 \
    PYTHONPATH=/tests/verification/lib \
    /opt/venv/bin/python -m pytest \
        -p no:cacheprovider -p srbfault -q --no-header --timeout=3000 \
        "$CANDIDATE"
STATUS=$?
log "candidate ${SRB_CANDIDATE_NAME:-?}: pytest exit $STATUS"
exit $STATUS
