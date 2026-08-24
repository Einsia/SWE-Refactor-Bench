#!/usr/bin/env bash
# Build-time self-check: does the scan collect, and did it collect enough?
#
# Run from the stage 1 Dockerfile against the empty /opt/workspace mount point.
# Collection alone executes every import in every module and every `parametrize`
# list, which is where a scan breaks: a typo in a constant, a missing `srbscan`
# helper, a module that shadows a fixture name.
#
# It matters that this fails the *build* rather than the review.  A scan that raises
# on import produces no findings, and in the rendered prompt no findings looks
# exactly like a clean tree -- the reviewer is told nothing and cannot tell that it
# was told nothing.  The floor below is the second half of the same argument: a scan
# that collects one check per module has also gone quiet, and quietly.
#
# The parametrized lists here are derived from /opt/original, so the floor is also
# a check on the mount: with State A absent, the per-translation-unit lists collapse
# to a single placeholder and the total falls through the floor.
set -euo pipefail

MIN="${1:-80}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# The image's `python`; `python3` so the same script runs by hand on a machine that
# only has the versioned name.
PY="$(command -v python || command -v python3)"

cd "$HERE"
# Both plugins, in the order run-scan.sh loads them.  `swerefactor.pytest_module` is
# what registers the four srb_* markers, and without it every `@srb_skip_ok` in the
# suite is an unknown mark.  That is worth being exact about, because the obvious
# reading of `--strict-markers` in pytest.ini is that a mistyped marker would fail
# here: it would not.  Under --collect-only, pytest 8 reports an unregistered mark as
# a PytestUnknownMarkWarning and exits 0; the error arrives when the test runs, which
# is grading time.  So loading the plugin is not a tidiness fix for six warnings --
# it is the only reason this check sees the markers at all, and the reason it runs the
# same two plugins the real scan does rather than an approximation of them.
#
# SRB_RESULT is deliberately unset: _Recorder.flush() returns early without a path,
# so the plugin loads and writes nothing into the image.
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
