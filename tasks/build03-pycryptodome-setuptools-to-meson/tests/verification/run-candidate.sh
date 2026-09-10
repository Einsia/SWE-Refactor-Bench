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
# The role does not reach the candidate.  A candidate that reads the role satisfies
# every mechanical condition for a break -- passes on the original, fails on the
# submission, three times identically -- while establishing nothing about the
# migration, so this script drops it and uses SRB_TARGET_TOKEN for anything a
# candidate can observe.  infra/tests/test_verification.py asserts that it is gone.
#
# This script does NOT build anything.  A configuration here is a property of the
# compiler rather than of either build system, and a candidate asks for one by name:
#
#     t = srbcrypto.tree("no-aesni")
#
# srbcrypto then copies the tree, puts a gcc that rejects `-maes` on PATH, runs the
# one command that builds either tree -- `python -m build --wheel --no-isolation` --
# installs the wheel it produced, and caches the result under $SRB_STATE.  So the
# first candidate to ask for a configuration pays for it and the rest are free, and
# the stage never pays for a configuration no round asked about.
#
# The consequence for this script is that its job is preparation and nothing else:
# scrub, environment, pytest.
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

case "$ROLE" in
    original|submission) ;;
    *) echo "unknown target role" >&2; exit 64 ;;
esac

# Belt and braces.  The harness does not set these, but this script may be run by
# hand or from a shell that has them, and an inherited value is inherited all the
# way down to the candidate.
unset SRB_TARGET_ROLE SRB_TARGET_NAME SRB_ORIGINAL

# Keyed by the token, not the role: the candidate is handed paths under $STATE and a
# directory named "original" would answer the question for it.
log() { printf '[%s] %s\n' "$ROLE" "$*" >&2; }

STATE="$WORK/$TOKEN"
mkdir -p "$STATE"

# --- 1. Scrub build output the tree arrived carrying --------------------------
# Once per tree, before anything is built.
#
# This matters more in a Python task than it looks.  The original arrives unpacked
# from original.tar.gz and is clean by construction -- verified: it contains no
# directory named build, dist or *.egg-info, and no .so, .o, .a, .pyc or .whl
# anywhere.  The submission arrives as whatever the agent left in /workspace/repo,
# which may well hold the build/ and .mesonpy-* of the agent's own last build, an
# egg-info from before the migration, or a compiled object sitting beside the source
# that produced it.  Left in place, an object built by some other toolchain gets
# installed by the wheel, and a candidate comparing the two trees is comparing this
# container's gcc against whatever built that file.
#
# Every name below is either exact or a suffix/prefix that cannot collide with a
# delivered file, and the collision was checked against the real tree rather than
# assumed.  The rule is build01's, learned the hard way there: `build-*` looks like
# the obvious way to catch build-release/ and also catches build-aux/, which is
# delivered content, and deleting it stops the ORIGINAL from building -- which
# silently makes every candidate in the stage "fail on the original" and hands the
# submission all 60 points.
#
# The specific near-miss in this task is `meson*`.  meson.build, meson.options and
# meson_options.txt are the submission's build system.  A scrub that matched them
# would delete the thing under test, the submission would fail to build in every
# configuration, and the stage would read as six rounds finding nothing.  So
# nothing here matches meson except the `.mesonpy-` prefix, which is meson-python's
# own temporary directory and is not a name any source tree ships.
if [ ! -f "$STATE/.scrubbed" ]; then
    find "$TARGET" -depth -type d \
        \( -name build -o -name _build -o -name builddir -o -name build-meson \
           -o -name dist -o -name __pycache__ \
           -o -name '*.egg-info' -o -name '*.dist-info' \
           -o -name '.mesonpy-*' -o -name '.pytest_cache' \
           -o -name '.eggs' -o -name '.tox' \) \
        -prune -exec rm -rf {} + 2>/dev/null
    # Files: recursive, because a compiled object lives beside the source that
    # produced it, several directories down.
    find "$TARGET" -type f \
        \( -name '*.o' -o -name '*.obj' -o -name '*.so' -o -name '*.so.*' \
           -o -name '*.pyd' -o -name '*.a' -o -name '*.lo' -o -name '*.la' \
           -o -name '*.pyc' -o -name '*.pyo' -o -name '*.whl' \
           -o -name '*.egg' -o -name '*.gcda' -o -name '*.gcno' \) \
        -delete 2>/dev/null
    touch "$STATE/.scrubbed"
    log "scrubbed build output from the delivered tree"
fi

# --- 2. Hand the candidate its environment -----------------------------------
export SRB_STATE="$STATE/configs"
export SRB_TARGET="$TARGET"
export SRB_TARGET_TOKEN="$TOKEN"
export SRB_SCRATCH="$STATE/candidates/${SRB_CANDIDATE_NAME:-candidate}"

# A fresh scratch per (candidate, tree): a file a candidate wrote while testing one
# tree must not still be there when the same candidate runs against the other, and
# an extracted wheel member from one must not be mistaken for the other's.
rm -rf "$SRB_SCRATCH"
mkdir -p "$SRB_SCRATCH" "$SRB_STATE"

# --- 3. Run the candidate ----------------------------------------------------
# /opt/venv/bin/python, which has pytest and neither backend.  The builds run in
# $SRB_BUILD_PYTHON, which has both.  A candidate therefore cannot `import
# setuptools` in its own process, which is deliberate: it would answer the same way
# on both trees, and a candidate might read that as a fact about one of them.
#
# -p no:cacheprovider because both trees share the candidates directory, and a
# .pytest_cache beside the candidate would be the one piece of state that crosses
# between the two halves of a comparison.
#
# No -x: a candidate here often asserts several properties of one configuration,
# and the configuration cost minutes to build.  Letting the rest of the file run
# after the first failure gives the adjudicator the whole picture for the same
# money.
#
# The timeout is per test and generous, because a test that asks for a
# configuration triggers a build of it, and a test that asks for the library's own
# suite triggers 39,245 known-answer comparisons -- about eighty seconds on top of
# the build.  A candidate whose subject IS a hang has srbcrypto's own per-command
# timeouts, which return rather than raise.
#
# -p srbfault is where this task's "the tree does not build" defence lives, and it
# is a plugin rather than a check in this script because the build happens inside
# the process started below: srbcrypto.tree() builds a wheel and installs it, and
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
