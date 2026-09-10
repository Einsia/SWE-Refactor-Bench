#!/usr/bin/env bash
# Build-time self-check: does the scan collect, and did it collect enough?
#
# Run from the stage 1 Dockerfile against the empty /opt/original and
# /opt/workspace mount points.  Collection executes every import in every module
# and every `parametrize` list, which is where a scan breaks: a typo in an
# `srbscan` constant, a helper that moved, a module that shadows a fixture name.
#
# It matters that this fails the *build* rather than the review.  A scan that
# raises on import produces no findings, and in the rendered prompt no findings
# looks exactly like a clean tree -- the reviewer is told nothing and has no way to
# tell that it was told nothing.  The floor is the second half of the same
# argument: a scan that collects one check per module has also gone quiet, and
# quietly.
#
# Phase 2 exists because this task's file comparison derives its parametrize list
# from the reference tree mounted at /opt/original rather than from a frozen
# manifest of State A's checksums.  That is the better design -- one fewer
# duplicated input, and the comparison is against the tree the reviewer is reading
# rather than against a file asserting what that tree used to hold -- but it moves
# the collected count onto a mount that does not exist at build time.  The floor
# alone cannot see the failure: an empty mount collects the module's three
# unparametrized checks plus one placeholder for the empty list, a plausible
# number that measures nothing.  So phase 2 points SRB_ORIGINAL at a synthetic
# tree with a known number of files in it and asserts the count tracks.
set -euo pipefail

MIN="${1:-12}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# The image's `python`; `python3` so the same script runs by hand on a machine
# that only has the versioned name.
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

# --- Phase 1: every module imports, against the real (empty) mount points ------
empty_out=$(collect "${SRB_ORIGINAL:-/opt/original}")
echo "$empty_out"
total=$(total_of "$empty_out")

if [ "$total" -lt "$MIN" ]; then
    echo "collect-check: the scan collected $total checks, expected at least $MIN" >&2
    exit 1
fi

# --- Phase 2: the file comparison tracks the tree it is given -------------------
# Six files in the shapes State A actually has -- a core source, a header, a build
# file with no suffix, a test script, a version stamp, and one binary -- plus one
# the walk must drop.  If the derivation works, the comparison module collects its
# unparametrized checks plus exactly these six.
#
# The binary belongs in the comparable set, not the ignored one.  Counting it as a
# file the walk must filter assumes the comparison walks the tree the way the
# *text* scan does.  It does not, and it should not: `EXEMPT_SUFFIXES` exists so
# that a search for a byte-order conditional does not open a PDF, whereas the
# change set's job is to say which of State A's files are no longer what State A
# shipped -- and State A ships two PDFs, so a walk that skipped binaries would be
# unable to report a replaced one.  `rel_files` filters `EXEMPT_DIRS` only, by
# design.
synth="$(mktemp -d)"
trap 'rm -rf "$synth"' EXIT
mkdir -p "$synth/tests" "$synth/examples" "$synth/.git"
echo 'int main(void){return 0;}' > "$synth/quickjs.c"
echo '#define X 1'               > "$synth/cutils.h"
echo 'all:'                      > "$synth/Makefile"
echo 'print("hi")'               > "$synth/tests/test_language.js"
echo '2020-11-08'                > "$synth/VERSION"
# A binary, standing in for State A's two PDFs: comparable, and counted.
: > "$synth/examples/hello.o"
# The one thing the walk must drop.  A VCS directory reported file by file would
# bury every real finding under its own objects, so `EXEMPT_DIRS` prunes it and
# `delivered-state` reports its existence once instead.
echo 'ref: refs/heads/main'      > "$synth/.git/HEAD"

synth_out=$(collect "$synth")
before=$(count_of "$empty_out" "changed-files")
after=$(count_of "$synth_out" "changed-files")

if [ -z "$before" ] || [ -z "$after" ]; then
    echo "$synth_out"
    echo "collect-check: could not read the file comparison's collected count" >&2
    exit 1
fi

# An empty parametrize list collects one placeholder item, so the six files
# replace it: after - before == 5.
if [ "$((after - before))" -ne 5 ]; then
    echo "$synth_out"
    echo "collect-check: the file comparison collected $before checks against an" \
         "empty reference tree and $after against a synthetic one holding six" \
         "comparable files (and one it must ignore); expected $((before + 5))." \
         "Its parametrize list is not deriving from SRB_ORIGINAL, which means that" \
         "against a missing mount it would report a clean tree rather than nothing" \
         "at all -- and a clean tree is the answer that costs a correct submission" \
         "nothing and a cheating one nothing either" >&2
    exit 1
fi

echo "collect-check: ok, $total checks collected, and the file comparison tracks" \
     "its reference tree ($before -> $after for six added files)"
