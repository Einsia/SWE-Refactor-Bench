#!/bin/bash
# =============================================================================
# Build the stage image's TWO offline Go module mirrors.
#
#   $COMPLETE  what every module in this stage builds against.  Carries an
#              archive for every module State A's go.sum verified -- the retired
#              Gin stack included, because State A imports it and this stage has
#              to be able to build State A.  That is not a concession.  State A
#              is the behavioural oracle every expectation here was recorded
#              from, so a mirror it cannot compile from is a mirror that can
#              never demonstrate the suite it backs is satisfiable.
#   $PRUNED    the agent image's mirror, reproduced: every .mod kept, every .zip
#              of a retired module deleted.  Nothing is built against it.  It
#              exists so the build module can report, as an unscored
#              observation, whether the submitted tree still needs an archive
#              the agent was never given.
#
# Why the roles are this way round, when $COMPLETE did not exist before
# ---------------------------------------------------------------------
# Stage 2 measures ONE thing: whether the rewrite still answers what the original
# answered.  Whether the rewrite actually happened is stage 1's question, and
# stage 1 answers it across the import graph, both manifests, vendor/ and the
# replace directives -- and a stage-1 gate failure scores the whole submission
# zero before stage 2 runs at all.
#
# Building stage 2 against the pruned mirror welded those two questions together,
# and the weld was visible from the one direction that matters: State A itself
# could not be graded.  Measured: State A failed `build` against the pruned mirror,
# because it could not resolve Gin, and passed every scored check against a
# complete one.  A failing scored check pays the whole stage nothing, so the pruned
# mirror costs the stage outright and leaves thirteen modules to fail on a tree
# with no binary -- and a behaviour-preservation harness whose own baseline cannot
# compile is not measuring preservation.
#
# The authoring constraint is untouched and is where it belongs: the AGENT's
# mirror is still pruned, so a tree that keeps importing Gin cannot be compiled
# or tested by the agent while the work is being done.  That is a stronger gate
# than any grader check.
#
# Both mirrors stock exactly what the environment image stocked -- no more, no
# less:
#
#   * everything State A's go.mod/go.sum closes over, so a submission that
#     kept a dependency can still build;
#   * every State B candidate the environment offered, so a submission that
#     moved to chi (or mux, or httprouter) can still build;
#   * NO .zip for any retired module in $PRUNED, which is what makes it the
#     agent's mirror rather than a copy of $COMPLETE.
#
# Why this is a separate script from the environment's build-goproxy.sh: that one
# warms from the repository's source tree (it runs `go mod tidy`, which needs to
# see imports).  A stage build context has no source tree -- tests/ contains the
# grader, not the project -- so warming here is driven by go.mod/go.sum alone.
# `go mod download all` plus `go mod graph` covers the build and test closures
# from the lockfile without reading a single .go file.
#
# The two scripts must agree on three inputs, and all three are copied into this
# build context rather than re-derived:
#
#   go.mod / go.sum        State A's, byte-identical to the tarball's
#   retired-modules.txt    the same ban list
#   CANDIDATES             the same pinned versions, in the same order
#
# A drift between them shows up as a submission that resolved fine for the agent
# and fails to resolve for the stage, naming a module the agent never touched.
# The stage Dockerfile therefore digests the first two against the values recorded
# at construction time before this script is ever run.
# =============================================================================
set -euo pipefail

LOCKDIR=${1:?usage: build-goproxy.sh <lockfile-dir> [complete-out] [pruned-out]}
COMPLETE=${2:-/opt/goproxy}
PRUNED=${3:-/opt/goproxy-pruned}
RETIRED_FILE=${SRB_RETIRED_MODULES:-/opt/srb/retired-modules.txt}

export GOTOOLCHAIN=local
export GOFLAGS=-mod=mod
export CGO_ENABLED=0
# Same reason as the environment build: GOPRIVATE='*' silently switches Go to
# direct VCS mode and bypasses a file:// proxy, which surfaces later as GitHub
# 403s inside a container that is supposed to be offline.
unset GOPRIVATE GONOSUMDB GONOSUMCHECK || true

# Warm through a throwaway cache. Anything left unpacked in a GOMODCACHE the
# stage later uses would let a Gin-keeping submission compile from the cache
# without ever consulting the pruned proxy -- the ban is enforced by the proxy,
# and only bites when the module is not already extracted.
export GOMODCACHE=/tmp/srb-warm-modcache
rm -rf "$GOMODCACHE"

say() { printf '\n=== %s\n' "$*"; }

WARM=/tmp/srb-warm
rm -rf "$WARM" && mkdir -p "$WARM"
cp "$LOCKDIR/go.mod" "$LOCKDIR/go.sum" "$WARM/"
cd "$WARM"

say "1/3 go mod download (build closure)"
go mod download

say "2/3 go mod download all (test closure)"
go mod download all

say "3/3 go mod graph (pruned graph .mod files)"
go mod graph > /dev/null

# ---------------------------------------------------------------------------
# The same pinned State B candidates the environment offers. Kept in the same
# order and with the same versions as environment/scripts/build-goproxy.sh.
# ---------------------------------------------------------------------------
say "State B candidate modules"
CANDIDATES="
github.com/go-chi/chi/v5@v5.0.12
github.com/go-chi/chi/v5@v5.1.0
github.com/go-chi/chi/v5@v5.3.1
github.com/go-chi/cors@v1.2.2
github.com/gorilla/mux@v1.8.0
github.com/gorilla/mux@v1.8.1
github.com/gorilla/handlers@v1.5.1
github.com/gorilla/handlers@v1.5.2
github.com/julienschmidt/httprouter@v1.3.0
github.com/google/uuid@v1.3.0
github.com/google/uuid@v1.6.0
"
SCRATCH=/tmp/srb-scratch
rm -rf "$SCRATCH" && mkdir -p "$SCRATCH" && cd "$SCRATCH"
cat > go.mod <<'EOF'
module swerefactor.local/goproxy-scratch

go 1.17

require github.com/go-chi/chi/v5 v5.3.1
EOF
for m in $CANDIDATES; do
  go mod download "$m"
done
go mod graph > /dev/null

# ---------------------------------------------------------------------------
# Every module version go.sum names, fetched by name.  `download all` walks the
# import graph and can stop short of a module that only `tidy` would reach;
# go.sum is the complete list of what State A's own resolution ever verified, so
# asking for each entry directly is what makes $COMPLETE a superset of any tidy
# a correct submission can run.  Copied from fw06's script, which documents it.
# ---------------------------------------------------------------------------
# Batched, then per-module only for a batch that failed.  chartmuseum's go.sum
# names 1859 distinct module@version, and one `go mod download` process each is
# ~30 minutes of build time for a list the earlier passes have mostly warmed
# already.  `go mod download` takes many arguments and fails as a unit, so the
# fallback is what keeps the per-entry note: a batch that fails is retried one
# name at a time, and only there does a name get reported.
say "go.sum sweep (every recorded version, by name)"
cd "$WARM"
SWEEP=/tmp/srb-sweep-list
awk '{ v=$2; sub(/\/go\.mod$/, "", v); if (v ~ /^v/) print $1 "@" v }' \
    "$LOCKDIR/go.sum" | sort -u > "$SWEEP"
total=$(wc -l < "$SWEEP")
echo "  $total distinct module@version"
missed=0
batch=0
while IFS= read -r chunk; do
  batch=$((batch + 1))
  # shellcheck disable=SC2086  # word splitting is the point: one arg per module
  if ! go mod download $chunk >/dev/null 2>&1; then
    for one in $chunk; do
      go mod download "$one" >/dev/null 2>&1 || {
        echo "  note: $one did not download (may be replaced or excluded)"
        missed=$((missed + 1))
      }
    done
  fi
done < <(xargs -n 100 < "$SWEEP")
rm -f "$SWEEP"
echo "  sweep complete: $batch batch(es), $missed entry/entries skipped"

# ---------------------------------------------------------------------------
# Materialise $COMPLETE, copy it to $PRUNED, then retire by omission in $PRUNED
# alone.  The copy happens BEFORE any pruning and for the reason fw06's script
# gives: two independent warm passes against a live proxy can fetch different
# bytes for a re-published version, and the two mirrors would then disagree
# about a dependency neither tree chose.  One warm, one copy, one prune.
# ---------------------------------------------------------------------------
CACHE="$(go env GOMODCACHE)/cache/download"
say "complete proxy -> $COMPLETE"
mkdir -p "$COMPLETE" && cp -a "$CACHE/." "$COMPLETE/"

# The proxy protocol serves .info, .mod, .zip and list; the rest is the module
# cache's own bookkeeping, which a file:// proxy ignores.  A `<version>.lock`
# left behind is harmless but ships in the image, so it goes.
find "$COMPLETE" \( -name 'lock' -o -name '*.lock' \) -delete 2>/dev/null || true
find "$COMPLETE" -name '*.partial' -delete 2>/dev/null || true
find "$COMPLETE" -name '*.tmp' -delete 2>/dev/null || true
find "$COMPLETE" -name 'sumdb' -type d -prune -exec rm -rf {} + 2>/dev/null || true

say "copying $COMPLETE -> $PRUNED (before any pruning)"
rm -rf "$PRUNED" && mkdir -p "$PRUNED" && cp -a "$COMPLETE/." "$PRUNED/"

say "retire by omission in $PRUNED only (.zip deleted, .mod kept)"
DELIVERY="$PRUNED"
# .mod is kept for the same reason as in the environment: `go mod tidy` needs it
# to compute the module graph while a stale `require` is still present, so
# deleting it would break a CORRECT migration mid-tidy. Without the .zip nothing
# can compile against it.
while read -r mod; do
  [ -n "$mod" ] || continue
  case "$mod" in \#*) continue ;; esac
  d="$DELIVERY/$mod/@v"
  if [ ! -d "$d" ]; then
    echo "FAIL: retired module $mod is not in the stage mirror at all" >&2
    echo "      A typo here silently un-bans it: there would be no zip to" >&2
    echo "      delete, the loop would pass, and Gin would remain buildable." >&2
    exit 1
  fi
  nmod=$(ls "$d"/*.mod 2>/dev/null | wc -l)
  nzip=$(ls "$d"/*.zip 2>/dev/null | wc -l)
  rm -f "$d"/*.zip "$d"/*.ziphash
  printf '  %-45s .mod=%s .zip=%s -> 0\n' "$mod" "$nmod" "$nzip"
  if [ "$nmod" -eq 0 ]; then
    echo "FAIL: $mod had no .mod to keep; tidy would break" >&2; exit 1
  fi
done < "$RETIRED_FILE"

# ---------------------------------------------------------------------------
# The two assertions that define the two roles.  Both are cheap, and both have
# failed silently in this ladder's history, in opposite directions:
#
#   * A $COMPLETE without the archive cannot build State A.  The oracle then
#     fails the suite recorded from it, and every submission is graded against a
#     baseline nobody demonstrated was reachable.  This is what fw04 shipped:
#     a failed stage for State A, from a build module that could not resolve.
#   * A $PRUNED with the archive is not the agent's mirror, and the build
#     module's retirement note would report "no archive needed" for every tree.
# ---------------------------------------------------------------------------
say "asserting each retired module CAN be compiled from $COMPLETE and CANNOT from $PRUNED"
while read -r mod; do
  [ -n "$mod" ] || continue
  case "$mod" in \#*) continue ;; esac
  if ! find "$COMPLETE/$mod/@v" -name '*.zip' 2>/dev/null | grep -q .; then
    echo "FAIL: $mod has no .zip in $COMPLETE.  State A imports it, so State A" >&2
    echo "      cannot be built here -- and State A is the behavioural oracle" >&2
    echo "      this stage replays.  A suite its own baseline fails is not" >&2
    echo "      measuring behaviour preservation." >&2
    exit 1
  fi
  if find "$PRUNED/$mod/@v" -name '*.zip' 2>/dev/null | grep -q .; then
    echo "FAIL: $mod still has a .zip in $PRUNED, which is supposed to" >&2
    echo "      reproduce the agent's own mirror." >&2
    exit 1
  fi
  printf '  %-45s %s zip(s) in complete, 0 in pruned\n' "$mod" \
    "$(find "$COMPLETE/$mod/@v" -name '*.zip' | wc -l)"
done < "$RETIRED_FILE"

say "discard the warming cache"
rm -rf "$GOMODCACHE" "$WARM" "$SCRATCH"

say "mirror summary"
printf '  %-16s %-8s %-8s %-8s %s\n' name modules zips mods bytes
for m in "$COMPLETE" "$PRUNED"; do
  printf '  %-16s %-8s %-8s %-8s %s\n' \
    "$(basename "$m")" \
    "$(find "$m" -name '*.info' | wc -l)" \
    "$(find "$m" -name '*.zip'  | wc -l)" \
    "$(find "$m" -name '*.mod'  | wc -l)" \
    "$(du -sh "$m" | cut -f1)"
done
echo "stage goproxy build complete: $COMPLETE (builds) and $PRUNED (observed only)"
