#!/usr/bin/env bash
# Build-time self-check: does the scan collect, and did it collect enough?
#
# Run from the stage 1 Dockerfile, against the empty /opt/workspace mount point.
# Collection is enough to execute every import in every module and every
# `parametrize` list, which is where a scan breaks: a typo in a constant, a
# missing `srbscan` helper, a module that shadows a fixture name.
#
# It matters that this fails the *build* rather than the review.  A scan that
# raises on import produces no findings, and in the rendered prompt no findings
# looks exactly like a clean tree: the reviewer is told nothing and cannot tell
# that it was told nothing.  The floor below is the second half of the same
# argument -- a scan that collects one check per module has also gone quiet, and
# quietly.
#
# The plugin list has to be the one `run-scan.sh` uses, not a subset of it.  The
# scan loads `-p srbscan` and `-p swerefactor.pytest_module`, and the second is what
# registers `srb_skip_ok`, `srb_weight` and `srb_group`.  Loading only the first
# would put this gate under `--strict-markers` without the marker vocabulary the
# scan honours, so every module using one fails the build with a message naming
# the marker and never mentioning the absent registry -- fail-closed, so nothing
# is mis-scored, but a real marker and a typo produce the same "not found in
# `markers`" line and the gate can no longer tell them apart.
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
      "$PY" -m pytest -c pytest.ini -p srbscan -p swerefactor.pytest_module \
            --collect-only -q modules 2>&1) || {
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
