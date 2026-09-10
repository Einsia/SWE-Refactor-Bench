#!/usr/bin/env bash
# Build-time self-check: does the scan collect, and did it collect enough?
#
# Run from the stage 1 Dockerfile against the empty /opt/workspace mount point.
# Collection executes every import in every module and every `parametrize` list,
# which is where a scan actually breaks: a typo in a constant, a helper that moved
# out of `srbscan`, a module that shadows a fixture name.
#
# It matters that this fails the *build* and not the review.  A scan that raises
# on import produces no findings, and in the rendered prompt no findings looks
# exactly like a clean tree -- the reviewer is told nothing and has no way to know
# it was told nothing.  The floor is the second half of the same argument: a scan
# that collected one check per module has also gone quiet, and quietly.
#
# The count is stable before either tree is mounted because the parametrized cases
# come from data/retired-stack.txt and from constants in the modules, not from the
# submission.  That is deliberate -- a scan whose case list grew with the tree would
# tell the reviewer different things about different submissions, and the count in
# the floor below would mean nothing.
#
# It follows that this script does *not* check the modules can read a tree.  Every
# check here is differential against /opt/original, and `srbscan._require_tree`
# raises INFRASTRUCTURE FAULT when that mount is empty -- which is the state at
# build time.  So collection is the whole of what can be verified without a tarball
# in the context, and the mount being wrong at grading time is reported by the run
# rather than by the build.
set -euo pipefail

MIN="${1:-90}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# The image's `python`; `python3` so the same script runs by hand on a machine
# that only has the versioned name.
PY="$(command -v python || command -v python3)"

cd "$HERE"
out=$(SRB_REPO="${SRB_REPO:-/opt/workspace}" \
      SRB_ORIGINAL="${SRB_ORIGINAL:-/opt/original}" \
      PYTHONPATH="$HERE/lib:${PYTHONPATH:-}" \
      "$PY" -m pytest -c pytest.ini -p srbscan --collect-only -q modules 2>&1) || {
    echo "$out"
    echo "collect-check: the scan does not collect" >&2
    exit 1
}

echo "$out"
total=$(printf '%s\n' "$out" | sed -n 's/.*: \([0-9]\{1,\}\)$/\1/p' \
        | awk '{s += $1} END {print s + 0}')

if [ "$total" -lt "$MIN" ]; then
    echo "collect-check: the scan collected $total checks, expected at least $MIN" >&2
    exit 1
fi
echo "collect-check: ok, $total checks collected"
