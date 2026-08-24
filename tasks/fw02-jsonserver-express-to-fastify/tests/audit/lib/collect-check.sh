#!/usr/bin/env bash
# Build-time self-check: does the scan collect, and did it collect enough?
#
# Run from the stage 1 Dockerfile against the empty /opt/workspace mount point.
# Collection executes every import in every module and every parametrize list,
# which is where a scan of this kind breaks: a typo in a constant, a regex that
# does not compile, a helper renamed in srbscan but not in a module.
#
# It matters that this fails the *build* rather than the review.  A module that
# raises on import produces no findings, and in the rendered prompt no findings
# looks exactly like a clean tree: the reviewer is told nothing and has no way to
# tell that it was told nothing.  The floor below is the second half of the same
# argument -- a suite that collects four checks has gone quiet just as
# effectively, and just as invisibly.
#
# This is not the gates self-check the environment image deliberately does not
# ship.  It predicts nothing about the verdict; it asserts that the advisory scan
# runs at all.
set -euo pipefail

MIN="${1:-90}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
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
