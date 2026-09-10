#!/usr/bin/env bash
# Build-time self-check: do the nine pytest modules collect, and did they collect
# the checks they are supposed to?
#
# Run from the stage 2 Dockerfile. Collection alone executes every import in every
# module and every `parametrize` list, which is where this suite breaks: a helper
# renamed in builder.py, a key that moved inside a ground-truth JSON, a module
# shadowing a fixture name from srbfixtures.
#
# The reason it fails the *build* rather than the run is arithmetic. The score is a
# weight-normalised mean of per-module pass rates, and a module that raises on
# import produces zero checks -- which `grade_behavioural` reads as rate 0.0 for that
# module. So a `data/` directory that did not make it into the image would not look
# like a broken image; it would look like ten submissions in a row that all failed
# the same way. The floor makes that a red build instead.
#
# `build` is not collected here. It is a plain script rather than a pytest module,
# it needs a Gradle build to do anything at all, and running it at image-build time
# would mean shipping a submission in a layer.
#
# Phase 2 is the other half. Every parametrize list in this suite derives from
# data/*.json -- 282 entry names, 271 class files, 1,328 test cases -- and the floor
# cannot tell a suite reading those files from one that hard-coded a plausible
# number of checks and stopped consulting them. So phase 2 builds a suite directory
# whose data/ is a *reduced* copy, and asserts the collected count falls by exactly
# the number of entries removed. A module that stopped deriving from data/ collects
# the same count from both and fails here.
set -euo pipefail

MIN="${1:-2000}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$(command -v python3 || command -v python)"

# The nine that are pytest. Named rather than globbed so a directory added without
# a suite.toml entry -- which would be collected by a glob and scored by nobody --
# does not quietly join the count.
MODULES=(tests entries derive runtime manifest version bytecode publication jpms)

# Collect against a given suite directory and echo pytest's own output. The
# `-q` on top of pytest.ini's own `-q` is what produces the one-line-per-file
# `path: N` summary this script parses.
collect() {
    local suite="$1" out paths=()
    local m
    for m in "${MODULES[@]}"; do paths+=("$suite/modules/$m"); done
    out=$(cd "$suite" \
          && SRB_SUITE_DIR="$suite" \
             SRB_REPO="${SRB_REPO:-/workspace/repo}" \
             SRB_SUITE_WORK="${SRB_SUITE_WORK:-/tmp/srb-collect-work}" \
             PYTHONPATH="$HERE/lib:${PYTHONPATH:-}" \
             "$PY" -m pytest -c "$suite/pytest.ini" -p srbfixtures \
                   --collect-only -q "${paths[@]}" 2>&1) || {
        echo "$out"
        echo "collect-check: the behavioural suite does not collect" >&2
        exit 1
    }
    printf '%s\n' "$out"
}

total_of() {
    printf '%s\n' "$1" | sed -n 's/.*: \([0-9]\{1,\}\)$/\1/p' \
        | awk '{s += $1} END {print s + 0}'
}

count_of() {
    printf '%s\n' "$1" | sed -n "s|.*/$2/.*: \([0-9]\{1,\}\)$|\1|p" | head -1
}

# --- Phase 1: every module imports and collects ------------------------------
out=$(collect "$HERE")
echo "$out"
total=$(total_of "$out")

if [ "$total" -lt "$MIN" ]; then
    echo "collect-check: the suite collected $total checks, expected at least" \
         "$MIN. A count this low means a parametrize list came back empty, which" \
         "for this suite means data/*.json is missing or has changed shape --" \
         "and at run time that would score as a submission's failure rather" \
         "than as a broken image" >&2
    exit 1
fi

# Every module must contribute something. A single module that collapsed to zero is
# invisible in a total of ~2,700 but is a whole weight class of the score gone.
for m in "${MODULES[@]}"; do
    n=$(count_of "$out" "$m")
    if [ -z "$n" ] || [ "$n" -lt 1 ]; then
        echo "$out"
        echo "collect-check: module '$m' collected no checks; its weight in" \
             "suite.toml would score zero for every submission" >&2
        exit 1
    fi
done

# --- Phase 2: the parametrize lists track data/ ------------------------------
# jars.json is the one to reduce: `entries` turns every name in `required_entries`
# into one check, so removing N names must remove N checks. Two non-empty copies
# rather than full-versus-empty, because an empty `parametrize` list collects one
# placeholder item and the delta would then be off by one for a reason that has
# nothing to do with this suite.
CUT=7
probe="$(mktemp -d)"
trap 'rm -rf "$probe"' EXIT

mkdir -p "$probe/data"
ln -s "$HERE/lib" "$probe/lib"
ln -s "$HERE/modules" "$probe/modules"
cp "$HERE/pytest.ini" "$probe/pytest.ini"
cp "$HERE"/data/*.json "$probe/data/"

"$PY" - "$probe/data/jars.json" "$CUT" <<'PYEOF'
import json, sys
path, cut = sys.argv[1], int(sys.argv[2])
jars = json.load(open(path))
removed = 0
# Drop `cut` entries from whichever artifact has the most, so the reduction is
# concentrated and the remaining artifacts are untouched.
key = max(jars, key=lambda k: len(jars[k].get("required_entries") or []))
entries = jars[key]["required_entries"]
if len(entries) <= cut:
    sys.exit("jars.json: %s has only %d entries" % (key, len(entries)))
jars[key]["required_entries"] = entries[:-cut]
removed = cut
json.dump(jars, open(path, "w"), indent=1)
print("collect-check: reduced %s by %d entries" % (key, removed))
PYEOF

reduced=$(collect "$probe")
before=$(count_of "$out" entries)
after=$(count_of "$reduced" entries)

if [ -z "$before" ] || [ -z "$after" ]; then
    echo "$reduced"
    echo "collect-check: could not read the entries module's collected count" >&2
    exit 1
fi

if [ "$((before - after))" -ne "$CUT" ]; then
    echo "$reduced"
    echo "collect-check: the entries module collected $before checks against the" \
         "real jars.json and $after against a copy with $CUT entries removed;" \
         "expected $((before - CUT)). Its parametrize list is not deriving from" \
         "data/jars.json, which means the 282 entry names it reports on are not" \
         "the ones the ground truth records" >&2
    exit 1
fi

echo "collect-check: ok, $total checks across ${#MODULES[@]} modules, and the" \
     "entry comparison tracks data/jars.json ($before -> $after)"
