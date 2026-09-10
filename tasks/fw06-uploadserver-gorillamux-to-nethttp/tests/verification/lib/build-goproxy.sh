#!/bin/bash
# =============================================================================
# Build stage 3's TWO offline module mirrors.
#
# This is the only image in the ladder that must build BOTH trees, so it is the
# only one that needs two mirrors:
#
#   $BASELINE  complete.  Carries an archive of the retired router, because State
#              A imports it and stage 3 has to be able to build State A.
#   $DELIVERY  pruned exactly as stage 2 prunes: every .mod kept, every .zip of a
#              retired or banned module deleted.  This is what the submission is
#              built against, so a tree that still imports the retired router
#              fails to compile here for the same reason it failed there.
#
# The order is the whole design. The complete mirror is assembled once and COPIED
# to $BASELINE before anything is deleted; the pruning then happens only to
# $DELIVERY. Building them as two independent warm passes would double the
# network time and, worse, allow them to disagree: two `go mod download` runs
# against a live proxy at different moments can fetch different bytes for a
# version that was re-published, and the two trees would then be built against
# subtly different dependencies. One warm, one copy, one prune.
#
# Everything else -- the four warming passes, the go.sum sweep, the .lock removal,
# the refusal to stock a banned router -- is stage 2's script, because the pruned
# mirror here has to be the same pruned mirror there. A submission that compiles
# in stage 2 and not in stage 3 would lose points for a difference between two
# mirrors rather than for anything it did.
# =============================================================================
set -euo pipefail

LOCKDIR=${1:?usage: build-goproxy.sh <lockfile-dir> <pruned-out> <baseline-out>}
DELIVERY=${2:-/opt/goproxy}
BASELINE=${3:-/opt/goproxy-baseline}
RETIRED_FILE=${SRB_RETIRED_MODULES:-/opt/verification/retired-modules.txt}
BANNED_FILE=${SRB_BANNED_ROUTERS:-/opt/verification/banned-routers.txt}

export GOTOOLCHAIN=local
export GOFLAGS=-mod=mod
export CGO_ENABLED=0
# A throwaway cache: an unpacked module in GOMODCACHE is usable without the proxy,
# so warming into the real one would defeat the ban it is meant to enforce.
export GOMODCACHE=/tmp/verification-warm-modcache
rm -rf "$GOMODCACHE"
# GOPRIVATE='*' forces direct VCS mode and bypasses a file:// proxy entirely.
unset GOPRIVATE GONOSUMDB GONOSUMCHECK || true

say() { printf '\n=== %s\n' "$*"; }

read_list() { sed -e 's/#.*//' -e '/^[[:space:]]*$/d' "$1" | tr -d '[:blank:]'; }

# Warm from a COPY of the lockfiles. `go mod download all` can rewrite go.sum, and
# those edits must not reach the files the two trees are built against.
WARM=/tmp/verification-warm
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
# is the complete list of what State A's own resolution ever verified.
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

say "assembling the complete mirror at $DELIVERY"
CACHE="$GOMODCACHE/cache/download"
if [ ! -d "$CACHE" ]; then
  echo "FAIL: nothing was warmed into $CACHE" >&2
  exit 1
fi
mkdir -p "$DELIVERY"
cp -a "$CACHE/." "$DELIVERY/"

# The files the proxy protocol serves are .info, .mod, .zip and list; everything
# else here is the module cache's own bookkeeping, which a file:// proxy ignores.
# `-name lock` matches only a file called exactly "lock" and would leave every
# `<version>.lock` in place -- measured on stage 2, 14 of them shipped.
find "$DELIVERY" \( -name 'lock' -o -name '*.lock' \) -delete 2>/dev/null || true
find "$DELIVERY" -name '*.partial' -delete 2>/dev/null || true
find "$DELIVERY" -name '*.tmp' -delete 2>/dev/null || true
find "$DELIVERY" -name 'sumdb' -type d -prune -exec rm -rf {} + 2>/dev/null || true

# ---------------------------------------------------------------------------
# The copy, BEFORE the prune.  This line is the difference between this script
# and stage 2's, and its position in the file is load-bearing.
# ---------------------------------------------------------------------------
say "copying the complete mirror to $BASELINE (before any pruning)"
rm -rf "$BASELINE"
mkdir -p "$BASELINE"
cp -a "$DELIVERY/." "$BASELINE/"
baseline_zips=$(find "$BASELINE" -name '*.zip' | wc -l)
echo "  baseline: $baseline_zips zip(s)"

say "applying the retirement to $DELIVERY only: drop every .zip, keep every .mod"
while read -r mod; do
  [ -n "$mod" ] || continue
  # The proxy layout !-escapes upper-case letters (Foo -> !foo). None of the
  # module paths in retired-modules.txt contain any, so the path is used as-is.
  case "$mod" in
    *[A-Z]*) echo "FAIL: $mod has upper-case letters and needs !-escaping" >&2
             exit 1 ;;
  esac
  dir="$DELIVERY/$mod/@v"
  if [ ! -d "$dir" ]; then
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

say "asserting no third-party router can be compiled from $DELIVERY"
fail=0
while read -r mod; do
  [ -n "$mod" ] || continue
  if find "$DELIVERY" -path "$DELIVERY/$mod*" -name '*.zip' 2>/dev/null | grep -q .; then
    echo "  FAIL $mod has a .zip in the pruned mirror" >&2
    fail=1
  fi
done < <(read_list "$BANNED_FILE")
[ "$fail" = 0 ] || { echo "the pruned mirror stocks a banned router" >&2; exit 1; }

# The mirror image of that assertion, and the reason this image has two mirrors at
# all.  If the baseline lost the retired router's archive, State A stops building,
# every candidate "fails on the original", the adjudicator upholds none of them,
# and six rounds report a survival that was never tested.  That failure is
# silent and pays the submission 60 points, so it is checked here where it costs a
# build instead.
say "asserting State A's router CAN be compiled from $BASELINE"
while read -r mod; do
  [ -n "$mod" ] || continue
  if ! find "$BASELINE/$mod/@v" -name '*.zip' 2>/dev/null | grep -q .; then
    echo "FAIL: $mod has no .zip in the baseline mirror. State A cannot be built, "\
         "so no candidate can pass on the original and every round would report a "\
         "survival it never earned." >&2
    exit 1
  fi
  echo "  $mod: $(find "$BASELINE/$mod/@v" -name '*.zip' | wc -l) zip(s) present"
done < <(read_list "$RETIRED_FILE")

say "mirror summary"
printf '  %-10s %-8s %-8s %-8s %s\n' name modules zips mods bytes
for m in "$DELIVERY" "$BASELINE"; do
  printf '  %-10s %-8s %-8s %-8s %s\n' \
    "$(basename "$m")" \
    "$(find "$m" -name '*.info' | wc -l)" \
    "$(find "$m" -name '*.zip' | wc -l)" \
    "$(find "$m" -name '*.mod' | wc -l)" \
    "$(du -sh "$m" | cut -f1)"
done

rm -rf "$WARM" "$GOMODCACHE"
say "both mirrors ready: pruned at $DELIVERY, complete at $BASELINE"
