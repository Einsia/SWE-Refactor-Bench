#!/usr/bin/env bash
# Build-time self-check: does the scan collect, and did it collect enough?
#
# Run from the stage-1 Dockerfile against the empty /opt/original and /opt/workspace
# mount points.  Collection alone executes every import in every module and every
# `parametrize` list, which is where a scan breaks: a typo in a constant, a helper
# renamed in srbscan.py, a module that shadows a fixture name.
#
# It matters that this fails the *build* rather than the review.  A scan that raises
# on import produces no findings, and in the rendered prompt no findings looks
# exactly like a clean tree -- the reviewer is told nothing and has no way to know it
# was told nothing.  The floor is the second half of the same argument: a scan that
# collects four checks has also gone quiet, and quietly.
#
# Phase 2 exists because of one specific design decision.  engine_sources derives its
# parametrize list from the tree mounted at SRB_ORIGINAL rather than from a frozen
# manifest -- there is no manifest in this image, on purpose, see srbscan.py -- so the
# number of checks it collects is a function of a mount that does not exist at build
# time.  The floor cannot see that: an empty mount collects the module's five
# unparametrized checks plus one placeholder for the empty list, which is a
# plausible-looking number that measures nothing.  So phase 2 points SRB_ORIGINAL at
# a synthetic tree with a known number of files and asserts the count follows it.
# That is the property a frozen manifest would have provided for free.
set -euo pipefail

MIN="${1:-14}"
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
          "$PY" -m pytest -c pytest.ini -p srbscan --collect-only -q modules 2>&1) || {
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

# --- Phase 1: every module imports, against the real (empty) mount points --------
empty_out=$(collect "${SRB_ORIGINAL:-/opt/original}")
echo "$empty_out"
total=$(total_of "$empty_out")

if [ "$total" -lt "$MIN" ]; then
    echo "collect-check: the scan collected $total checks, expected at least $MIN" >&2
    exit 1
fi

# --- Phase 2: the file comparison tracks the tree it is given --------------------
# Four files under the directories the module walks, in shapes State A actually
# contains: a source, a header, a Tcl test and a build file.  If the derivation
# works, engine_sources collects its unparametrized checks plus exactly these four.
synth="$(mktemp -d)"
trap 'rm -rf "$synth"' EXIT
mkdir -p "$synth/src" "$synth/ext/fts5" "$synth/test" "$synth/tool"
echo 'int a(void);'   > "$synth/src/a.c"
echo '#define B 1'    > "$synth/ext/fts5/b.h"
echo 'do_test 1.0 {}' > "$synth/test/c.test"
echo 'all:'           > "$synth/tool/Makefile.in"

synth_out=$(collect "$synth")
before=$(count_of "$empty_out" "engine_sources")
after=$(count_of "$synth_out" "engine_sources")

if [ -z "$before" ] || [ -z "$after" ]; then
    echo "$synth_out"
    echo "collect-check: could not read engine_sources' collected count" >&2
    exit 1
fi

# An empty parametrize list collects one placeholder item, so the four files replace
# it: after - before == 3.
if [ "$((after - before))" -ne 3 ]; then
    echo "$synth_out"
    echo "collect-check: engine_sources collected $before checks against an empty" \
         "reference tree and $after against a synthetic one holding four files;" \
         "expected $((before + 3)).  Its parametrize list is not deriving from" \
         "SRB_ORIGINAL, which means that against a missing mount it would report a" \
         "clean tree instead of nothing at all" >&2
    exit 1
fi

echo "collect-check: ok, $total checks collected, and the file comparison tracks" \
     "its reference tree ($before -> $after for four added files)"
