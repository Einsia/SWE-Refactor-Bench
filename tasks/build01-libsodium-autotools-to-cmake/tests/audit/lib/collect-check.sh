# !/usr/bin/env bash Build-time self-check: does the scan collect, and did it
# collect enough?
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
# This task's copy does one thing fw01's does not, and needs to.  Its source
# comparison derives its parametrize list from the reference tree mounted at
# /opt/original rather than from a frozen manifest, so the number of checks it
# collects is a function of a mount that does not exist at build time.  The floor
# alone cannot see that: an empty mount collects the module's six unparametrized
# checks and one placeholder for the empty list, which is a plausible-looking
# number and measures nothing.  So phase 2 below points SRB_ORIGINAL at a
# synthetic tree with a known number of sources in it and asserts the count
# tracks.
set -euo pipefail

MIN="${1:-90}"
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

# --- Phase 2: the source comparison tracks the tree it is given ----------------
# Four files with the three watched suffixes, one under test/, plus the template,
# in the two roots the module walks.  If the derivation works, the sources module
# collects its unparametrized checks plus exactly these four.
synth="$(mktemp -d)"
trap 'rm -rf "$synth"' EXIT
mkdir -p "$synth/src/libsodium/crypto_box" \
         "$synth/src/libsodium/include/sodium" \
         "$synth/test/default"
echo 'int a(void);'      > "$synth/src/libsodium/crypto_box/a.c"
echo '#define B 1'       > "$synth/src/libsodium/include/sodium/b.h"
echo '.text'             > "$synth/test/default/c.S"
echo '@VERSION@'         > "$synth/src/libsodium/include/sodium/version.h.in"
# Two files with unwatched suffixes, to catch a walk that stopped filtering.
echo 'not a source'      > "$synth/src/libsodium/README.md"
echo 'not a source'      > "$synth/test/default/d.exp"

synth_out=$(collect "$synth")
before=$(count_of "$empty_out" "sources")
after=$(count_of "$synth_out" "sources")

if [ -z "$before" ] || [ -z "$after" ]; then
    echo "$synth_out"
    echo "collect-check: could not read the sources module's collected count" >&2
    exit 1
fi

# An empty parametrize list collects one placeholder item, so the four sources
# replace it: after - before == 3.
if [ "$((after - before))" -ne 3 ]; then
    echo "$synth_out"
    echo "collect-check: the source comparison collected $before checks against an" \
         "empty reference tree and $after against a synthetic one holding four" \
         "immutable files; expected $((before + 3)).  Its parametrize list is not" \
         "deriving from SRB_ORIGINAL, which means that against a missing mount it" \
         "would report a clean tree instead of nothing at all" >&2
    exit 1
fi

echo "collect-check: ok, $total checks collected, and the source comparison" \
     "tracks its reference tree ($before -> $after for four added files)"
