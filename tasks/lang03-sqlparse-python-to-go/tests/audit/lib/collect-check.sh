#!/usr/bin/env bash
# Build-time self-check: does the scan collect, and did it collect enough?
#
# Run from the stage 1 Dockerfile, against the empty /opt/workspace mount point.
# Collection is enough to execute every import in every module and every
# `parametrize` list, which is where a scan breaks: a typo in a constant, a missing
# `srbscan` helper, a module that shadows a fixture name.
#
# It matters that this fails the *build* rather than the review.  A scan that raises
# on import produces no findings, and in the rendered prompt no findings looks
# exactly like a clean tree: the reviewer is told nothing and cannot tell that it was
# told nothing.  The floor below is the second half of the same argument -- a scan
# that collects one check per module has also gone quiet, and quietly.
#
# Phase 2 exists because two of this task's lists are derived rather than frozen.
# The closure module parametrizes over State A's implementation modules, read from
# the tree mounted at /opt/original, and the rest of the contract-derived lists come
# from data/source-contract.json.  The contract is in the image, so the floor sees
# it; the mount is not, so the floor cannot see it at all.  Against an empty mount
# the module list collapses to one placeholder, which is a plausible-looking number
# that measures nothing -- exactly the failure this file exists to prevent, one level
# up.  So phase 2 points SRB_ORIGINAL at a synthetic tree with a known number of
# modules in it and asserts the count follows.
#
# Both plugins are loaded, the same two `run-scan.sh` loads, and that is not
# incidental: `pytest.ini` sets `--strict-markers`, and the markers the modules use
# are registered by `swerefactor.pytest_module` rather than in the ini file.  Collecting
# with only `srbscan` would fail on an unknown marker -- which is what happened, and
# is the better outcome -- but a self-check that loads a configuration the scan never
# runs under is measuring something else, whichever way it comes out.
set -euo pipefail

MIN="${1:-90}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# The image's `python`; `python3` so the same script runs by hand on a machine that
# only has the versioned name.
PY="$(command -v python || command -v python3)"

cd "$HERE"

# Collect and echo, or fail loudly with pytest's own output.  $1 is the tree to
# present as State A.
collect() {
    local original="$1" out
    out=$(SRB_REPO="${SRB_REPO:-/opt/workspace}" \
          SRB_ORIGINAL="$original" \
          PYTHONPATH="$HERE/lib:${PYTHONPATH:-}" \
          "$PY" -m pytest -c pytest.ini \
                -p srbscan -p swerefactor.pytest_module \
                --collect-only -q modules 2>&1) || {
        echo "$out"
        echo "collect-check: the scan does not collect" >&2
        exit 1
    }
    printf '%s\n' "$out"
}

# The per-file counts pytest prints in -q mode, summed.
total_of() {
    printf '%s\n' "$1" | sed -n 's/.*: \([0-9]\{1,\}\)$/\1/p' \
        | awk '{s += $1} END {print s + 0}'
}

# One module's count, by path fragment.
count_of() {
    printf '%s\n' "$1" | sed -n "s|.*$2.*: \([0-9]\{1,\}\)$|\1|p" | head -1
}

# --- Phase 1: every module imports, against the real (empty) mount points ------
empty_out=$(collect "${SRB_ORIGINAL:-/opt/original}")
echo "$empty_out"
total=$(total_of "$empty_out")

if [ "$total" -lt "$MIN" ]; then
    echo "collect-check: the scan collected $total checks, expected at least $MIN" >&2
    exit 1
fi

# --- Phase 2: the per-module list tracks the tree it is given -------------------
# A tree that resolves as an sqlparse checkout -- srbscan's ROOT_MARKERS decide
# that, and a synthetic tree missing them would be walked one level off and prove
# nothing -- holding four modules under sqlparse/, one of them nested.  Two files
# that are not modules are there to catch a walk that stopped filtering by suffix.
synth="$(mktemp -d)"
trap 'rm -rf "$synth"' EXIT
mkdir -p "$synth/sqlparse/engine"
for marker in LICENSE AUTHORS CHANGELOG CONTRIBUTING.md SECURITY.md; do
    echo "synthetic" > "$synth/$marker"
done
echo 'x = 1'          > "$synth/sqlparse/__init__.py"
echo 'x = 2'          > "$synth/sqlparse/lexer.py"
echo 'x = 3'          > "$synth/sqlparse/sql.py"
echo 'x = 4'          > "$synth/sqlparse/engine/grouping.py"
echo 'def f(): ...'   > "$synth/sqlparse/typed.pyi"
echo 'not a module'   > "$synth/sqlparse/notes.txt"

synth_out=$(collect "$synth")
before=$(count_of "$empty_out" "closure")
after=$(count_of "$synth_out" "closure")

if [ -z "$before" ] || [ -z "$after" ]; then
    echo "$synth_out"
    echo "collect-check: could not read the closure module's collected count" >&2
    exit 1
fi

# An empty parametrize list collects one placeholder item, so the four modules
# replace it: after - before == 3.
if [ "$((after - before))" -ne 3 ]; then
    echo "$synth_out"
    echo "collect-check: the closure module collected $before checks against an" \
         "empty reference tree and $after against a synthetic one holding four" \
         "Python modules; expected $((before + 3)).  Its parametrize list is not" \
         "deriving from SRB_ORIGINAL, which means that against a missing mount it" \
         "would report every module deleted instead of reporting nothing at all" >&2
    exit 1
fi

echo "collect-check: ok, $total checks collected, and the per-module list tracks" \
     "its reference tree ($before -> $after for four added modules)"
