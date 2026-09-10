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
set -euo pipefail

MIN="${1:-90}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# The image's `python`; `python3` so the same script runs by hand on a machine
# that only has the versioned name.
PY="$(command -v python || command -v python3)"

cd "$HERE"
# Both plugins, because run-scan.sh loads both: `-p srbscan -p
# swerefactor.pytest_module`.  A collection check that loads a different plugin set
# from the real scan is checking a configuration nothing runs, and it fails in the
# direction that stops a build for a healthy tree: pytest.ini sets
# --strict-markers and declares only `scan`, while the four srb_* markers are
# registered at run time by pytest_module.pytest_configure's addinivalue_line.
# So a module carrying @pytest.mark.srb_skip_ok -- which is how a check declares a
# skip the contract licenses -- collects under the scan and raises
# "'srb_skip_ok' not found in `markers` configuration option" here.  Measured: it
# took the whole stage 1 image build down at Dockerfile:157 with
# `Interrupted: 1 error during collection`, after test_closure.py:250 gained the
# marker.  Declaring the markers in pytest.ini instead would work and would put a
# copy of infra's MARKERS tuple in every task's ini, to drift from the plugin that
# owns them.  SRB_RESULT stays unset on purpose: _Recorder.flush() returns early
# on an empty path, so the plugin loads and registers without writing a result.
out=$(SRB_REPO="${SRB_REPO:-/opt/workspace}" \
      SRB_ORIGINAL="${SRB_ORIGINAL:-/opt/original}" \
      SRB_CONTRACT="${SRB_CONTRACT:-$HERE/data/source-contract.json}" \
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
