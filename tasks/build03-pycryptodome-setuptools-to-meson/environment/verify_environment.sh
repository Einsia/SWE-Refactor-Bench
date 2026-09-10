#!/usr/bin/env bash
# State A self-check, run once at image build time and then deleted.
#
# Proves four things about the image, so that a failure during a trial is the
# agent's doing and not the environment's:
#
#   1. State A builds a wheel with its own setuptools build, offline;
#   2. the wheel installs and the compiled artefacts load through ctypes;
#   3. a representative slice of the project's own self-test suite passes;
#   4. /workspace/repo is left exactly as the agent will receive it -- unbuilt.
#
# (1)-(3) happen entirely under /tmp, and the install goes to a throwaway
# --target directory rather than into site-packages. Building in place would hand
# the agent a tree that already contains .abi3.so files and an egg-info
# directory; installing into site-packages would leave 41 prebuilt shared objects
# sitting in the image. Both are leaks, and the second is the kind that is easy to
# miss because nothing about the repository looks different afterwards.
#
# Nothing here says anything about grading. It builds State A the way State A
# documents and fails the image build if that does not work.
set -eu

REPO=/workspace/repo
WORK=$(mktemp -d /tmp/stateA-XXXXXX)
trap 'rm -rf "$WORK"' EXIT

echo "=== 1. offline wheel build with setuptools"
cp -a "$REPO" "$WORK/b"
cd "$WORK/b"
rm -rf .git
python3 -m build --wheel --no-isolation --outdir "$WORK/dist" > "$WORK/build.log" 2>&1 || {
  echo "  FAILED: python -m build --wheel" >&2; tail -40 "$WORK/build.log" >&2; exit 1; }
WHEEL=$(ls "$WORK"/dist/*.whl)
echo "  $(basename "$WHEEL")  OK"

# The compile graph the migration has to reproduce. Recorded here as a tripwire:
# if a base-image change alters the unit count, the ground truth is stale and
# this image build is the right place to find that out.
NSO=$(python3 - "$WHEEL" <<'PY'
import sys, zipfile
print(sum(1 for n in zipfile.ZipFile(sys.argv[1]).namelist()
          if n.endswith(".abi3.so")))
PY
)
echo "  .abi3.so in wheel: $NSO"
test "$NSO" -eq 41 || { echo "  FAILED: expected 41 shared objects, got $NSO" >&2; exit 1; }

echo "=== 2. install and load the compiled artefacts"
SITE="$WORK/site"
cd /tmp
python3 -m pip install --no-index --no-deps --target "$SITE" "$WHEEL" \
    > "$WORK/install.log" 2>&1 || {
  echo "  FAILED: pip install --target" >&2; tail -30 "$WORK/install.log" >&2; exit 1; }
export PYTHONPATH="$SITE"
python3 - <<'PY'
import os
from Crypto.Cipher import AES
from Crypto.Hash import SHA256
import Crypto
# One AES-ECB block against a known answer: proves the ctypes library loaded and
# the cipher works, not merely that the file exists. The expected ciphertext comes
# from outside this project, so the check is not comparing the build against
# itself.
ct = AES.new(bytes(range(16)), AES.MODE_ECB).encrypt(bytes(16))
assert ct.hex() == "c6a13b37878f5b826f4f8162a1c8d879", ct.hex()
assert SHA256.new(b"abc").hexdigest().startswith("ba7816bf")
print("  Crypto from %s  OK" % os.path.dirname(Crypto.__file__))
PY

echo "=== 3. self-test slice"
# Three sub-packages rather than all 39,245 ids: enough to prove the suite runs
# and the native code is wired up, without adding minutes to every image build.
for mod in Cipher Hash PublicKey; do
  python3 -c "
import unittest, Crypto.SelfTest.$mod as m
s = unittest.TestSuite(m.get_tests(config={'slow_tests': False}))
r = unittest.TextTestRunner(verbosity=0).run(s)
assert r.wasSuccessful(), '$mod: %d failures, %d errors' % (len(r.failures), len(r.errors))
print('  SelfTest.$mod: %d tests OK' % r.testsRun)
"
done
unset PYTHONPATH

echo "=== 4. /workspace/repo untouched, and nothing installed"
cd "$REPO"
for p in build dist builddir .mesonpy-* *.egg-info; do
  # shellcheck disable=SC2086
  test ! -e $p 2>/dev/null || { echo "  FAILED: $p exists in $REPO" >&2; exit 1; }
done
if find . -name '*.abi3.so' -o -name '*.o' | grep -q .; then
  echo "  FAILED: compiled output present in $REPO" >&2; exit 1
fi
test -f setup.py && test -f compiler_opt.py
# The image must not ship a built copy of the library the agent is asked to build.
python3 -c "
import importlib.util, sys
sys.exit(1 if importlib.util.find_spec('Crypto') else 0)
" || { echo "  FAILED: Crypto is importable from the image itself" >&2; exit 1; }
echo "  clean"

echo "STATE A VERIFIED"
