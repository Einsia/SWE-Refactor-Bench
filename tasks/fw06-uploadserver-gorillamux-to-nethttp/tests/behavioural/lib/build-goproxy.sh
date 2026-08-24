#!/bin/bash
# =============================================================================
# Build the VERIFIER's TWO offline module mirrors.
#
#   $COMPLETE  what stage 2 builds against.  Carries an archive of every module
#              State A's own go.sum verified -- the retired router included,
#              because State A imports it and stage 2 has to be able to build
#              State A.  That is not a concession: State A is the behavioural
#              oracle, so a mirror it cannot compile from is a mirror that can
#              never demonstrate the suite it backs is satisfiable.
#   $PRUNED    the agent image's mirror, reproduced: every .mod kept, every .zip
#              of a retired or banned module deleted.  Stage 2 does not build
#              against this one.  It exists so the build module can report, as
#              an unscored observation, whether the submitted tree still needs an
#              archive the agent was never given.
#
# Why the roles are this way round
# --------------------------------
# Stage 2 measures ONE thing: whether the rewrite still answers what the original
# answered.  Whether the rewrite actually happened is stage 1's question, and
# stage 1 answers it across the import graph, both manifests, vendor/, replace
# directives, the 27 alternative routers and the shape of a router that was
# copied rather than retired -- and a stage-1 gate failure scores the whole
# submission zero before stage 2 runs at all.
#
# Building stage 2 against the pruned mirror would weld those two questions
# together, and the weld shows from the one direction that matters: State A
# itself could not be graded.  The oracle would fail the suite recorded from it,
# for a reason that has nothing to do with behaviour, and a behaviour-
# preservation harness whose own baseline scores zero is not measuring
# preservation.
#
# The authoring constraint is where it belongs: the AGENT's mirror is pruned, so
# a tree that keeps importing the retired router cannot be compiled by the agent
# at all.  That is a stronger gate than any grader check, because it bites while
# the work is being done.
#
# The mirrors must both be equivalent to the agent image's: same modules, same
# versions.  Not a copy of it -- the two images are built independently, and
# building them the same way from the same inputs is what makes a drift visible
# as a build failure instead of as an honest agent losing points for no reason.
#
# Driven by go.mod/go.sum ALONE, not by a source tree.  That is a deliberate
# difference from the environment's build-goproxy.sh, which warms from the
# repository and can therefore run `go mod tidy`:
#
#   * `tests/` is the verifier's build context and holds the grader, not the
#     project.  There is no State A tree here to warm from -- and shipping one
#     would put a working gorilla/mux checkout inside the grader, which is
#     exactly what snapshot.py refuses to do for the same reason.
#
#   * `go mod download all` + `go mod graph` cover the build and test closures
#     from the lockfile without reading a single .go file.  The environment's
#     fourth pass (`tidy`) adds .mod files that tidy alone would fetch; the
#     Dockerfile closes that gap by asserting this mirror against the manifest
#     recorded from the agent image, so a missing .mod fails the build here
#     rather than failing a submission's tidy later.
#
# Two invariants hold here as they do in the environment build:
#
#   * Every version is pinned by the snapshot's own go.mod/go.sum.  Nothing here
#     resolves @latest, because the two mirrors are built at different times and
#     a floating version would put different bytes in each -- which an honest
#     agent would meet as a go.sum mismatch it cannot explain or fix.
#
#   * The retirement, where it is applied at all, is applied the same way: keep
#     every .mod, delete the .zip.  Keeping the .mod lets `go mod tidy` compute
#     the module graph while the stale require is still present, which is what
#     makes a correct migration's tidy step work offline.  Here that shape only
#     has to hold for $PRUNED, and it is checked there because $PRUNED is what
#     the agent's own mirror looks like.
#
# There is no CANDIDATES list, and its absence is the point.  fw06's State B is
# the standard library: net/http.ServeMux.  Stocking any third-party router here
# -- even one nobody asked for -- would turn the task back into fw04, where the
# answer is a different dependency rather than none.
# =============================================================================
set -euo pipefail

LOCKDIR=${1:?usage: build-verifier-goproxy.sh <lockfile-dir> [complete-out] [pruned-out]}
COMPLETE=${2:-/opt/goproxy}
PRUNED=${3:-/opt/goproxy-pruned}
RETIRED_FILE=${SRB_RETIRED_MODULES:-/opt/verifier/retired-modules.txt}
BANNED_FILE=${SRB_BANNED_ROUTERS:-/opt/verifier/banned-routers.txt}

export GOTOOLCHAIN=local
export GOFLAGS=-mod=mod
export CGO_ENABLED=0
# A throwaway cache: an unpacked module in GOMODCACHE is usable without the
# proxy, so warming into the real one would defeat the ban it is meant to
# enforce. Same reasoning as the agent image, for the same reason.
export GOMODCACHE=/tmp/verifier-warm-modcache
rm -rf "$GOMODCACHE"
# GOPRIVATE='*' forces direct VCS mode and bypasses a file:// proxy entirely.
unset GOPRIVATE GONOSUMDB GONOSUMCHECK || true

say() { printf '\n=== %s\n' "$*"; }

read_list() { sed -e 's/#.*//' -e '/^[[:space:]]*$/d' "$1" | tr -d '[:blank:]'; }

# Warm from a COPY of the lockfiles. `go mod download all` can rewrite go.sum,
# and those edits must not reach the files whose digests the verifier compares
# the submission against.
WARM=/tmp/verifier-warm
rm -rf "$WARM" && mkdir -p "$WARM"
cp "$LOCKDIR/go.mod" "$LOCKDIR/go.sum" "$WARM/"
cd "$WARM"

export GOPROXY="${WARM_GOPROXY:-https://proxy.golang.org,direct}"
export GOSUMDB="${WARM_GOSUMDB:-sum.golang.org}"

say "1/3 go mod download (build closure)"
go mod download          || { echo "pass 1 (download) failed" >&2; exit 1; }

say "2/3 go mod download all (test closure)"
go mod download all      || { echo "pass 2 (download all) failed" >&2; exit 1; }

say "3/3 go mod graph (.mod for every node of the pruned graph)"
go mod graph  >/dev/null  || { echo "pass 3 (graph) failed" >&2; exit 1; }

# Every module version go.sum names, fetched by name. `download all` walks the
# import graph and can stop short of a module that only tidy would reach; go.sum
# is the complete list of what State A's own resolution ever verified, so asking
# for each entry directly is what makes this mirror a superset of any tidy a
# correct submission can run.
say "go.sum sweep (every recorded version, by name)"
missed=0
while read -r mod ver _rest; do
  [ -n "${mod:-}" ] || continue
  case "$ver" in
    */go.mod) ver="${ver%/go.mod}" ;;
  esac
  case "$ver" in
    v*) : ;;
    *)  continue ;;
  esac
  go mod download "$mod@$ver" >/dev/null 2>&1 || {
    echo "  note: $mod@$ver did not download (may be replaced or excluded)"
    missed=$((missed + 1))
  }
done < "$LOCKDIR/go.sum"
echo "  sweep complete ($missed entry/entries skipped)"

say "assembling $COMPLETE from the warmed cache"
CACHE="$GOMODCACHE/cache/download"
if [ ! -d "$CACHE" ]; then
  echo "FAIL: nothing was warmed into $CACHE" >&2
  exit 1
fi
mkdir -p "$COMPLETE"
cp -a "$CACHE/." "$COMPLETE/"

# The files the proxy protocol serves are .info, .mod, .zip and list; everything
# else here is the module cache's own bookkeeping, which a file:// proxy ignores.
# `-name lock` matched only a file called exactly "lock" and left every
# `<version>.lock` in place -- measured, 14 of them shipped in the image.
find "$COMPLETE" \( -name 'lock' -o -name '*.lock' \) -delete 2>/dev/null || true
find "$COMPLETE" -name '*.partial' -delete 2>/dev/null || true
find "$COMPLETE" -name '*.tmp' -delete 2>/dev/null || true
find "$COMPLETE" -name 'sumdb' -type d -prune -exec rm -rf {} + 2>/dev/null || true

# ---------------------------------------------------------------------------
# The copy, BEFORE any pruning.  Same shape as stage 3's script and for the same
# reason: two independent warm passes against a live proxy can fetch different
# bytes for a version that was re-published, and the two mirrors would then
# disagree about a dependency neither tree chose.  One warm, one copy, one prune.
# ---------------------------------------------------------------------------
say "copying the complete mirror to $PRUNED (before any pruning)"
rm -rf "$PRUNED"
mkdir -p "$PRUNED"
cp -a "$COMPLETE/." "$PRUNED/"

say "applying the retirement to $PRUNED only: drop every .zip, keep every .mod"
while read -r mod; do
  [ -n "$mod" ] || continue
  # The proxy layout !-escapes upper-case letters (Foo -> !foo). None of the
  # module paths in retired-modules.txt contain any, so the path is used as-is;
  # if that list ever gains a mixed-case module, this is the line to revisit.
  case "$mod" in
    *[A-Z]*) echo "FAIL: $mod has upper-case letters and needs !-escaping" >&2
             exit 1 ;;
  esac
  dir="$PRUNED/$mod/@v"
  if [ ! -d "$dir" ]; then
    # Unlike the environment build, "absent" is fatal here too: the retired
    # module's .mod is what lets a correct submission's `go mod tidy` resolve
    # the stale require it is in the middle of removing.
    echo "FAIL: retired module $mod is not in the mirror at all" >&2
    exit 1
  fi
  n_zip=$(find "$dir" -name '*.zip' | wc -l)
  find "$dir" -name '*.zip' -delete
  find "$dir" -name '*.ziphash' -delete 2>/dev/null || true
  n_mod=$(find "$dir" -name '*.mod' | wc -l)
  echo "  $mod: removed $n_zip .zip, kept $n_mod .mod"
  [ "$n_mod" -gt 0 ] || { echo "FAIL: $mod has no .mod left -- tidy will break" >&2; exit 1; }
done < <(read_list "$RETIRED_FILE")

# The retired module is on BOTH lists -- it is the router State A used and a
# banned router -- so the sweep below has to exclude it or it would fail on the
# complete mirror for holding exactly the archive that mirror exists to hold.
# Stage 1's closure scan makes the same exclusion for the same reason, under the
# name BANNED_OTHERS.
say "asserting no ALTERNATIVE router can be compiled from either mirror"
fail=0
while read -r mod; do
  [ -n "$mod" ] || continue
  retired_too=0
  while read -r r; do
    [ -n "$r" ] || continue
    [ "$mod" = "$r" ] && retired_too=1
  done < <(read_list "$RETIRED_FILE")
  [ "$retired_too" = 1 ] && continue
  for m in "$COMPLETE" "$PRUNED"; do
    if find "$m" -path "$m/$mod*" -name '*.zip' 2>/dev/null | grep -q .; then
      echo "  FAIL $mod has a .zip in $m" >&2
      fail=1
    fi
  done
done < <(read_list "$BANNED_FILE")
[ "$fail" = 0 ] || { echo "a verifier mirror stocks an alternative router" >&2; exit 1; }

# The two assertions that define the two roles.  Both are cheap and both have
# failed silently before in this ladder's history, in opposite directions:
#
#   * A $COMPLETE without the archive cannot build State A.  The oracle then
#     fails the suite recorded from it and every submission is graded against a
#     baseline nobody demonstrated was reachable.
#   * A $PRUNED with the archive is not the agent's mirror, and the build module's
#     retirement note would then report "no archive needed" for every tree.
say "asserting the retired router CAN be compiled from $COMPLETE and CANNOT from $PRUNED"
while read -r mod; do
  [ -n "$mod" ] || continue
  if ! find "$COMPLETE/$mod/@v" -name '*.zip' 2>/dev/null | grep -q .; then
    echo "FAIL: $mod has no .zip in $COMPLETE. State A imports it, so State A" >&2
    echo "      cannot be built here -- and State A is the behavioural oracle" >&2
    echo "      this whole stage replays.  A suite its own baseline fails is not" >&2
    echo "      measuring behaviour preservation." >&2
    exit 1
  fi
  if find "$PRUNED/$mod/@v" -name '*.zip' 2>/dev/null | grep -q .; then
    echo "FAIL: $mod still has a .zip in $PRUNED, which is supposed to reproduce" >&2
    echo "      the agent's own mirror." >&2
    exit 1
  fi
  echo "  $mod: $(find "$COMPLETE/$mod/@v" -name '*.zip' | wc -l) zip(s) in complete, 0 in pruned"
done < <(read_list "$RETIRED_FILE")

say "mirror summary"
printf '  %-10s %-8s %-8s %-8s %s\n' name modules zips mods bytes
for m in "$COMPLETE" "$PRUNED"; do
  printf '  %-10s %-8s %-8s %-8s %s\n' \
    "$(basename "$m")" \
    "$(find "$m" -name '*.info' | wc -l)" \
    "$(find "$m" -name '*.zip'  | wc -l)" \
    "$(find "$m" -name '*.mod'  | wc -l)" \
    "$(du -sh "$m" | cut -f1)"
done

rm -rf "$WARM" "$GOMODCACHE"
say "verifier mirrors ready: $COMPLETE (builds) and $PRUNED (observed only)"
