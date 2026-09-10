#!/usr/bin/env bash
# Image-build-time proof that State A builds, installs and passes its own test
# suite completely offline. Runs on a scratch copy; /workspace/repo is left
# untouched so every trial starts from an identical clean tree.
#
# This is a check on the *environment*, not on a submission. It answers "can the
# image we are about to hand somebody build the thing we are asking them to
# migrate", which is a question about our own delivery, and it answers it before
# an agent's six hours are spent discovering the answer is no. It knows nothing
# about the grading ladder and leaves nothing behind that does: the script itself
# is deleted after it runs, and the only file it keeps is its own build log --
# which records what State A's build did, and State A is the oracle the agent is
# invited to read anyway.
set -euo pipefail

SCRATCH=/tmp/state-a
LOG=/opt/state-a-build.log
export DO_NOT_UPDATE_CONFIG_SCRIPTS=1

echo "== validating State A builds offline (scratch copy)" | tee "$LOG"
rm -rf "$SCRATCH"
mkdir -p "$SCRATCH/src"
tar -c -C /workspace/repo --exclude=.git --exclude=.gitignore . | tar -x -C "$SCRATCH/src"

cd "$SCRATCH/src"
./autogen.sh -s          >>"$LOG" 2>&1
mkdir -p "$SCRATCH/build"
cd "$SCRATCH/build"
"$SCRATCH/src/configure" --prefix="$SCRATCH/inst" >>"$LOG" 2>&1
make -j"$(nproc)"        >>"$LOG" 2>&1
make install             >>"$LOG" 2>&1
make check -j"$(nproc)"  >>"$LOG" 2>&1

# Record what State A produced, for the record only (no goldens are left in the
# image: the verifier carries its own copy).
{
  echo "== State A validated"
  ls -l "$SCRATCH/inst/lib" | sed 's/^/  /'
  grep -E '^# (TOTAL|PASS|FAIL)' "$LOG" | sed 's/^/  /'
} | tee -a "$LOG"

# Remove every build product so the agent image ships no prebuilt library.
rm -rf "$SCRATCH"
echo "== scratch removed; /workspace/repo untouched" | tee -a "$LOG"

# Fail loudly if the trial tree got polluted.
cd /workspace/repo
if [ -n "$(git status --porcelain)" ]; then
  echo "FATAL: /workspace/repo was modified by the State A validation:" >&2
  git status --porcelain >&2
  exit 1
fi
echo "== /workspace/repo is clean at tag state-a"
