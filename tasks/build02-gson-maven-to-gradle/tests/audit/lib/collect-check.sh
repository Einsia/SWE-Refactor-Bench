#!/usr/bin/env bash
# Build-time self-check: does the scan collect, and did it collect enough?
#
# Run from the stage 1 Dockerfile against the empty /opt/original and /opt/workspace
# mount points. Collection alone executes every import in every module and every
# `parametrize` list, which is where a scan breaks: a typo in a constant, a helper
# renamed in srbscan.py, a module shadowing a fixture name.
#
# It matters that this fails the *build* rather than the review. A scan that raises on
# import produces no findings, and in the rendered prompt no findings looks exactly
# like a clean tree -- the review is told nothing and cannot tell that it was told
# nothing. The floor in phase 1 is the second half of the same argument: a scan that
# collects one check per module has also gone quiet, just less obviously.
#
# Phase 2 exists because this task's source comparison derives its parametrize list
# from the tree at /opt/original rather than from a frozen manifest, so the number of
# checks it collects is a function of a mount that does not exist at build time. The
# floor cannot see that: an empty mount still collects the module's six unparametrized
# checks and one placeholder for the empty list, a plausible-looking 7 that measures
# nothing. So phase 2 points SRB_ORIGINAL at two synthetic trees of known and
# different shape and asserts the count moves by exactly the difference between them.
#
# Two non-empty trees rather than empty-versus-planted, deliberately. pytest collects
# one placeholder item for an empty `parametrize` list, so a delta measured from the
# empty mount is off by one for a reason that has nothing to do with this task, and
# the next person to add a check here would have to rediscover why. Comparing 3
# against 8 planted files removes the placeholder from the arithmetic: the delta is 5
# because five files were added, and if the derivation is broken it is 0.
set -euo pipefail

MIN="${1:-40}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# The image's `python`; `python3` so the same script runs by hand on a machine that
# only has the versioned name.
PY="$(command -v python || command -v python3)"

cd "$HERE"

# Collect and echo, or fail loudly with pytest's own output. $1 is the tree to
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

total_of() {
    printf '%s\n' "$1" | sed -n 's/.*: \([0-9]\{1,\}\)$/\1/p' \
        | awk '{s += $1} END {print s + 0}'
}

count_of() {
    printf '%s\n' "$1" | sed -n "s|.*$2.*: \([0-9]\{1,\}\)$|\1|p" | head -1
}

# A tree `srbscan._resolve` recognises, holding $1 frozen files under gson/src plus
# one of each of the other classifications, so the partition is exercised and not just
# the immutable branch.
synth_tree() {
    local n="$1" dir="$2" i
    mkdir -p "$dir/gson/src/main/java" "$dir/examples/demo"
    for i in $(seq 1 "$n"); do
        echo "class A$i {}" > "$dir/gson/src/main/java/A$i.java"
    done
    echo 'a license'      > "$dir/LICENSE"          # frozen, matched exactly
    echo 'sample'         > "$dir/examples/demo/y.txt"  # frozen, under examples/
    echo '# readme'       > "$dir/README.md"        # editable: markdown at depth 0
    echo '<project/>'     > "$dir/pom.xml"          # retired
    echo 'Bundle-Name: x' > "$dir/gson/bnd.bnd"     # editable by name
}

# --- Phase 1: every module imports, against the real (empty) mount points -------
empty_out=$(collect "${SRB_ORIGINAL:-/opt/original}")
echo "$empty_out"
total=$(total_of "$empty_out")

if [ "$total" -lt "$MIN" ]; then
    echo "collect-check: the scan collected $total checks, expected at least $MIN" >&2
    exit 1
fi

# --- Phase 2: the source comparison tracks the tree it is given -----------------
small="$(mktemp -d)"
large="$(mktemp -d)"
trap 'rm -rf "$small" "$large"' EXIT
# 3 and 8 files under gson/src, plus 2 more frozen paths in each, so the immutable
# lists are 5 and 10 and the difference is 5.
synth_tree 3 "$small"
synth_tree 8 "$large"

small_out=$(collect "$small")
large_out=$(collect "$large")
before=$(count_of "$small_out" "sources")
after=$(count_of "$large_out" "sources")

if [ -z "$before" ] || [ -z "$after" ]; then
    echo "$large_out"
    echo "collect-check: could not read the sources module's collected count" >&2
    exit 1
fi

if [ "$((after - before))" -ne 5 ]; then
    echo "$large_out"
    echo "collect-check: the source comparison collected $before checks against a" \
         "synthetic tree holding 5 frozen files and $after against one holding 10;" \
         "expected $((before + 5)). Its parametrize list is not deriving from" \
         "SRB_ORIGINAL, which means that against a missing mount it would report a" \
         "clean tree instead of nothing at all" >&2
    exit 1
fi

echo "collect-check: ok, $total checks collected against the empty mounts, and the" \
     "source comparison tracks its reference tree ($before -> $after)"
