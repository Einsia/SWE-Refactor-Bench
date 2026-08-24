#!/usr/bin/env bash
# Build-time self-check for the scan suite.  Invoked from the stage-1 Dockerfile
# as `bash lib/collect-check.sh <floor>`.
#
# The failure this exists to catch: a scan that raises on import produces no
# findings, and in the rendered prompt no findings looks exactly like a clean
# tree.  The reviewer would read "the mechanical scan reported nothing" and pass a
# submission nobody looked at.  So the image refuses to build unless the modules
# collect, and collect a plausible number of checks.
#
# Three phases, in increasing specificity:
#
#   1. Against *empty* mounts.  Collection must succeed with no reference tree and
#      no submission -- that is the state the image is built in, and a module that
#      needs a populated mount to import is a module that dies in production the
#      first time a mount is misconfigured.  Total must clear the floor.
#
#   2. Against a *synthetic* State A.  `sources` derives its parametrize list from
#      whatever is mounted at /opt/original, so its collected count is the one
#      number here that must move when the mount does.  If it does not move, the
#      list has been frozen into the file and the 583-file comparison is grading a
#      constant.
#
#   3. Against a *synthetic* submission.  `buildsystem` reads the delivered tree
#      inside its test bodies, never at parametrize time, so its count must NOT
#      move.  This is what stops a hostile or empty submission from reducing the
#      number of checks the reviewer is shown.
set -euo pipefail

FLOOR="${1:-60}"
SUITE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODULES="$SUITE_DIR/modules"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

# _count_in <logfile> -- how many checks that log says were collected.
#
# Two formats, because the number has to be right on whichever pytest is
# installed: 8.x prints one `path::test[id]` line per check under `-q
# --collect-only`, 9.x prints one `path: <n>` line per file.  The image pins
# 8.3.4, so the first branch is what runs in production; the second is what keeps
# this script honest when it is run by hand on a newer pytest, which is exactly
# when a silently-zero count would be believed.
_count_in() {
    local n
    n="$(grep -c '::' "$1" || true)"
    if [ "${n:-0}" -gt 0 ]; then
        echo "$n"
        return
    fi
    awk -F': ' '/^[^ =].*: [0-9]+$/ { s += $NF } END { print s + 0 }' "$1"
}

# collect <outdir> -- collect every module under the current SRB_* mounts, and
# write one `<module> <count>` line per module to <outdir>/counts.
collect() {
    local out="$1"
    mkdir -p "$out"
    : >"$out/counts"
    local mod name log
    for mod in "$MODULES"/*/; do
        [ -d "$mod" ] || continue
        name="$(basename "$mod")"
        log="$out/$name.log"
        if ! SRB_MODULE_DIR="$mod" \
             SRB_SUITE_DIR="$SUITE_DIR" \
             python -m pytest -c "$SUITE_DIR/pytest.ini" \
                 -p srbscan --collect-only -q "$mod" >"$log" 2>&1; then
            echo "collect-check: module '$name' failed to collect" >&2
            sed -n '1,60p' "$log" >&2
            exit 1
        fi
        echo "$name $(_count_in "$log")" >>"$out/counts"
    done
}

# total_of <outdir> -- every collected check across all modules.
total_of() {
    awk '{ s += $2 } END { print s + 0 }' "$1/counts"
}

# count_of <outdir> <module> -- collected checks belonging to one module.
count_of() {
    awk -v want="$2" '$1 == want { print $2 + 0; found = 1 }
                       END { if (!found) print 0 }' "$1/counts"
}

# --------------------------------------------------------------------------- #
# phase 1 -- empty mounts
# --------------------------------------------------------------------------- #

mkdir -p "$WORK/empty-original" "$WORK/empty-workspace"

export SRB_ORIGINAL="$WORK/empty-original"
export SRB_REPO="$WORK/empty-workspace"

collect "$WORK/phase1"
BEFORE_TOTAL="$(total_of "$WORK/phase1")"
BEFORE_SOURCES="$(count_of "$WORK/phase1" sources)"
BEFORE_BUILDSYSTEM="$(count_of "$WORK/phase1" buildsystem)"

echo "collect-check: phase 1 collected $BEFORE_TOTAL checks against empty mounts"
sed 's/^/collect-check:   /' "$WORK/phase1/counts"

if [ "$BEFORE_TOTAL" -lt "$FLOOR" ]; then
    echo "collect-check: only $BEFORE_TOTAL checks collected, floor is $FLOOR" >&2
    echo "collect-check: a module stopped contributing -- read the phase 1 logs" >&2
    cat "$WORK"/phase1/*.log >&2
    exit 1
fi

if [ "$BEFORE_SOURCES" -eq 0 ] || [ "$BEFORE_BUILDSYSTEM" -eq 0 ]; then
    echo "collect-check: a named module collected nothing (sources=$BEFORE_SOURCES," >&2
    echo "collect-check: buildsystem=$BEFORE_BUILDSYSTEM).  Phases 2 and 3 compare" >&2
    echo "collect-check: against these numbers, so a zero here would make them" >&2
    echo "collect-check: pass without measuring anything." >&2
    exit 1
fi

# --------------------------------------------------------------------------- #
# phase 2 -- a synthetic State A moves the mount-derived list
# --------------------------------------------------------------------------- #
#
# Four files the immutable rule claims (three prefixes and one named root file)
# and two it does not.  `sources` parametrizes over the four; an empty list
# collects a single placeholder id, so the arithmetic is 4 - 1 = 3.

FAKE="$WORK/fake-original"
mkdir -p "$FAKE/src" "$FAKE/lib/Crypto" "$FAKE/Doc"
printf 'int main(void){return 0;}\n' >"$FAKE/src/AES.c"
printf 'x = 1\n'                     >"$FAKE/lib/Crypto/__init__.py"
printf 'docs\n'                      >"$FAKE/Doc/index.rst"
printf 'readme\n'                    >"$FAKE/README.rst"
printf 'not immutable\n'             >"$FAKE/pyproject.toml"
printf 'not immutable\n'             >"$FAKE/requirements-test.txt"

export SRB_ORIGINAL="$FAKE"
collect "$WORK/phase2"
AFTER_SOURCES="$(count_of "$WORK/phase2" sources)"

DELTA=$(( AFTER_SOURCES - BEFORE_SOURCES ))
echo "collect-check: phase 2 moved 'sources' by $DELTA ($BEFORE_SOURCES -> $AFTER_SOURCES)"

if [ "$DELTA" -ne 3 ]; then
    echo "collect-check: expected 'sources' to grow by exactly 3, grew by $DELTA" >&2
    echo "collect-check: either the parametrize list is not read from the mount," >&2
    echo "collect-check: or the immutable rule no longer matches src/ lib/ Doc/" >&2
    echo "collect-check: and the six named root files." >&2
    exit 1
fi

# --------------------------------------------------------------------------- #
# phase 3 -- a synthetic submission does not move the constant-parametrized one
# --------------------------------------------------------------------------- #

FAKEWS="$WORK/fake-workspace"
mkdir -p "$FAKEWS/lib/Crypto" "$FAKEWS/subprojects"
printf 'project("x")\n'  >"$FAKEWS/meson.build"
printf '[build-system]\n' >"$FAKEWS/pyproject.toml"
printf 'x = 1\n'          >"$FAKEWS/lib/Crypto/__init__.py"

export SRB_REPO="$FAKEWS"
collect "$WORK/phase3"
AFTER_BUILDSYSTEM="$(count_of "$WORK/phase3" buildsystem)"

echo "collect-check: phase 3 'buildsystem' $BEFORE_BUILDSYSTEM -> $AFTER_BUILDSYSTEM"

if [ "$AFTER_BUILDSYSTEM" -ne "$BEFORE_BUILDSYSTEM" ]; then
    echo "collect-check: 'buildsystem' collected a different number of checks" >&2
    echo "collect-check: once a submission was mounted ($BEFORE_BUILDSYSTEM ->" >&2
    echo "collect-check: $AFTER_BUILDSYSTEM).  It reads the delivered tree at" >&2
    echo "collect-check: parametrize time, which lets an empty or hostile" >&2
    echo "collect-check: submission decide how many checks run against it." >&2
    exit 1
fi

echo "collect-check: ok"
