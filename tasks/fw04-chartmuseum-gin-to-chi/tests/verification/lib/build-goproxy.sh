#!/bin/bash
# =============================================================================
# Build stage 3's TWO offline Go module mirrors.
#
# This is the only stage that has to build both programs, so it is the only one
# that needs two mirrors, and the difference between them is the whole reason
# the stage works:
#
#   $BASELINE   complete.  Carries every .zip, Gin's included, and is the only
#               mirror in the benchmark that does.  It exists to build the
#               ORIGINAL tree -- which requires Gin and always will.
#   $DELIVERY   the pruned mirror, byte-for-byte what stage 2 hands a
#               submission: no .zip for any retired module.  It builds the
#               SUBMISSION and nothing else.
#
# Two mirrors sounds like a hole in the dependency gate.  It is not, and the
# reason is worth stating precisely because getting it wrong would quietly make
# the whole task unenforceable:
#
#   * The gate is not "Gin's bytes exist nowhere in the image."  It is "the
#     submission cannot resolve Gin."  A submission is built with GOPROXY
#     pointed at $DELIVERY, in a container with no network, so npm-style
#     ENOTCACHED is what rejects it -- Go's own resolver, not a check anyone
#     wrote and therefore nothing anyone can talk their way past.
#   * $BASELINE is used with cwd inside the original tree and never with cwd
#     inside the submission's.  run-candidate.sh selects it from the role
#     argument, which arrives in argv rather than in the environment, so it is
#     not something a candidate can inherit.
#   * Stage 2 has already measured whether the submission builds against the
#     pruned mirror alone.  A submission that only builds here would have
#     scored zero on stage 2's first module and never have reached this stage.
#
# What this image adds is the ability to ask the original a question at grading
# time.  That is the point of the stage: a candidate has to pass on the original
# before its failure on the submission means anything, and "passes on the
# original" is not checkable without an original that runs.
#
# Warming is driven by go.mod/go.sum alone, not by a source tree -- a stage build
# context has no repository in it.  `go mod download all` plus `go mod graph`
# covers the build and test closures from the lockfile without reading a .go
# file.  The three inputs below must agree with the environment image's, and all
# three are copied into this build context rather than re-derived:
#
#   go.mod / go.sum        State A's, byte-identical to the tarball's
#   retired-modules.txt    the same ban list
#   CANDIDATES             the same pinned versions, in the same order
#
# A drift shows up as a submission that resolved fine for the agent and fails to
# resolve for the stage, naming a module the agent never touched.  The stage
# Dockerfile therefore digests the first two against the values recorded at
# construction time before this script is ever run.
# =============================================================================
set -euo pipefail

LOCKDIR=${1:?usage: build-goproxy.sh <lockfile-dir> <delivery-out> [baseline-out]}
DELIVERY=${2:-/opt/goproxy}
BASELINE=${3:-}
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
# Materialise both mirrors from the one warmed cache, then prune only the
# delivery copy.
#
# Copy order is load-bearing: the baseline is taken BEFORE the retire loop runs,
# because that loop is destructive and there is no second warm to recover from.
# Doing it the other way round -- prune, then try to restore Gin into the
# baseline -- would need a second network fetch in a build that is supposed to be
# reproducible from one.
# ---------------------------------------------------------------------------
CACHE="$(go env GOMODCACHE)/cache/download"

if [ -n "$BASELINE" ]; then
  say "baseline proxy -> $BASELINE  (complete: every zip, Gin included)"
  mkdir -p "$BASELINE" && cp -a "$CACHE/." "$BASELINE/"
  # Assert the property this mirror exists for.  A baseline that had somehow
  # lost Gin's zip would fail to build the original, every candidate would
  # report "does not pass on the original", and the stage would award all
  # sixty points to every submission while looking like it had worked.
  while read -r mod; do
    [ -n "$mod" ] || continue
    case "$mod" in \#*) continue ;; esac
    n=$(find "$BASELINE/$mod/@v" -name '*.zip' 2>/dev/null | wc -l)
    if [ "$n" -eq 0 ]; then
      echo "FAIL: baseline mirror has no .zip for $mod" >&2
      echo "      The original cannot be built, so nothing can be compared." >&2
      exit 1
    fi
    printf '  %-45s .zip=%s (kept)\n' "$mod" "$n"
  done < "$RETIRED_FILE"
fi

say "delivery proxy -> $DELIVERY"
mkdir -p "$DELIVERY" && cp -a "$CACHE/." "$DELIVERY/"

say "retire by omission (.zip deleted, .mod kept) -- delivery mirror only"
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

say "discard the warming cache"
rm -rf "$GOMODCACHE" "$WARM" "$SCRATCH"

# The one invariant that matters after both copies exist: the two mirrors differ
# in exactly the retired modules' zips and nowhere else.  If the delivery mirror
# were missing something the baseline has, a correct submission would fail to
# build for a reason that has nothing to do with its port -- and the failure
# would read as a defect in the submission.
if [ -n "$BASELINE" ]; then
  say "the two mirrors differ only in the retired zips"
  only_baseline=$(cd "$BASELINE" && find . -name '*.zip' | sort > /tmp/srb-b.txt; \
                  cd "$DELIVERY" && find . -name '*.zip' | sort > /tmp/srb-d.txt; \
                  comm -23 /tmp/srb-b.txt /tmp/srb-d.txt)
  unexpected=""
  while read -r z; do
    [ -n "$z" ] || continue
    keep=""
    while read -r mod; do
      [ -n "$mod" ] || continue
      case "$mod" in \#*) continue ;; esac
      case "$z" in "./$mod/@v/"*) keep=1 ;; esac
    done < "$RETIRED_FILE"
    [ -n "$keep" ] || unexpected="$unexpected $z"
  done <<EOF
$only_baseline
EOF
  if [ -n "$unexpected" ]; then
    echo "FAIL: the delivery mirror is missing zips that are not retired:" >&2
    for z in $unexpected; do echo "  $z" >&2; done
    exit 1
  fi
  n=$(printf '%s\n' "$only_baseline" | grep -c . || true)
  echo "  $n zip(s) present in baseline and absent from delivery, all retired"
  rm -f /tmp/srb-b.txt /tmp/srb-d.txt
fi

say "sizes"
du -sh "$DELIVERY"
[ -n "$BASELINE" ] && du -sh "$BASELINE"
echo "stage goproxy build complete"
