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
# `swerefactor.pytest_module` is loaded here as well as in run-scan.sh, and it has
# to be: pytest.ini sets --strict-markers, and the four `srb_*` markers are
# registered by that plugin's pytest_configure rather than written into the ini.
# Collecting without it makes every @pytest.mark.srb_skip_ok an unknown-marker
# error -- and this suite needs that marker on every conditional check, because
# pytest_module rewrites an unmarked skip into a `fail` that would reach the
# reviewer's digest as a finding asserting the opposite of what happened.
# Registering the markers in pytest.ini instead would be a second copy of the
# plugin's own list, free to drift from it.  The plugin is inert under
# --collect-only: nothing executes, and its recorder writes nothing when
# SRB_RESULT is unset, which it is at build time.
set -euo pipefail

MIN="${1:-60}"
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
