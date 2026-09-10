#!/bin/bash
# =============================================================================
# Build the two offline Go module proxies for fw04.
#
#   /opt/goproxy-baseline  the complete State A closure, Gin included.  Used
#                          once, to build the read-only State A oracle, then
#                          deleted from the image.
#   /opt/goproxy           the delivery mirror.  Everything a Gin-free port may
#                          legitimately need, and NO .zip for any of the three
#                          retired modules.  This is also the verifier's mirror.
#
# Runs at image build time, where the network IS available.  The trial and the
# verifier both run with network_mode = "no-network", so this proxy is the only
# module source that exists for them.
#
# Warming has to be done FOUR ways, not one.  Measured, not assumed:
#   go mod download        build-closure zips
#   go mod download all    test-closure zips
#   go mod graph           .mod for every node of the pruned module graph
#   go mod tidy            .mod for the go1.16 compatibility graph as well
# Under this repo's `go 1.17` directive, `go mod tidy` verifies go1.16
# reproducibility, which walks the UNPRUNED graph and needs .mod files no build
# ever fetches.  Warming only the first two leaves `go mod tidy` failing offline
# on github.com/google/uuid@v1.2.0 -- an error naming a module unrelated to
# anything the agent changed.
#
# The ban is enforced by omission: delete the retired modules' .zip but KEEP
# their .mod.  Dropping both would make `go mod tidy` fail even for a CORRECT
# migration, because tidy needs the .mod for graph computation while the stale
# `require` is still in go.mod.  Keeping the .mod still makes compilation
# impossible, and once the last import is gone `go mod tidy` succeeds offline
# and drops all three requires by itself.
# =============================================================================
set -euo pipefail

REPO=${1:?usage: build-goproxy.sh <repo-dir>}
RETIRED_FILE=${SRB_RETIRED_MODULES:-/opt/srb/retired-modules.txt}
BASELINE=/opt/goproxy-baseline
DELIVERY=/opt/goproxy

export GOTOOLCHAIN=local
export GOFLAGS=-mod=mod
export CGO_ENABLED=0

# Warm through a THROWAWAY module cache.
#
# This matters more than it looks.  A proxy ban only bites when the module is
# not already unpacked in GOMODCACHE: `go build` is perfectly happy to use an
# extracted github.com/gin-gonic/gin@v1.8.1 without consulting the proxy at all.
# Warming into the agent's real GOMODCACHE would therefore hand the agent a
# working Gin and quietly void the entire constraint.  The agent's cache is
# populated later, from the pruned delivery proxy, where Gin cannot appear.
export GOMODCACHE=/tmp/warm-modcache
rm -rf "$GOMODCACHE"
# GOPRIVATE must stay empty.  GOPRIVATE='*' forces direct VCS mode and silently
# bypasses a file:// proxy entirely; the symptom is a burst of GitHub 403s in a
# container that is meant to be offline.
unset GOPRIVATE GONOSUMDB GONOSUMCHECK || true

say() { printf '\n=== %s\n' "$*"; }

WARM=/tmp/warm
rm -rf "$WARM" && cp -a "$REPO" "$WARM" && chmod -R u+w "$WARM"
cd "$WARM"

say "1/4 go mod download (build closure)"
go mod download

say "2/4 go mod download all (test closure)"
go mod download all

say "3/4 go mod graph (pruned graph .mod files)"
go mod graph > /dev/null

say "4/4 go mod tidy (go1.16 compat graph .mod files)"
go mod tidy
# tidy edits go.mod/go.sum; the snapshot's own copies are authoritative.
cp "$REPO/go.mod" "$REPO/go.sum" "$WARM/"

# ---------------------------------------------------------------------------
# State B candidates.  A Gin-free port need not be a chi port, so warm the
# plausible destinations rather than blessing one.  chi v5.3.1 declares
# `go 1.23`; v5.0.12 and v5.1.0 declare `go 1.14`.  Both paths are stocked, so
# the agent may either bump the go directive or pin an older chi -- a real
# decision with a real consequence, not a trap.
#
# EVERY version here is pinned, and none may be `@latest`.  This script builds
# the mirror for BOTH images: the environment the agent works in, and the
# verifier that grades it.  With `@latest`, the two builds resolve independently
# -- so an agent that pinned the gorilla/mux the environment happened to stock
# would have its go.sum fail to resolve in a verifier whose mirror was built a
# week later against a newer release.  The failure would name a module the agent
# never chose, and would look like a broken submission rather than a drifting
# mirror.  Pinning makes the two mirrors a function of this file alone.
# ---------------------------------------------------------------------------
say "State B candidate modules"
SCRATCH=/tmp/scratch
rm -rf "$SCRATCH" && mkdir -p "$SCRATCH" && cd "$SCRATCH"
# chi is pinned by an explicit `require`, not left to `go mod tidy`. At this
# point in the build GOPROXY is still the network, so a tidy that had to resolve
# `latest` would pull whatever chi released most recently and add an unpinned
# version to the mirror -- the same drift the pinned candidate list above exists
# to prevent.
cat > go.mod <<'EOF'
module swerefactor.local/scratch

go 1.17

require github.com/go-chi/chi/v5 v5.3.1
EOF
cat > main.go <<'EOF'
package main

import (
	"net/http"

	"github.com/go-chi/chi/v5"
	"github.com/go-chi/chi/v5/middleware"
)

func main() {
	r := chi.NewRouter()
	r.Use(middleware.Recoverer, middleware.RequestID, middleware.RealIP)
	_ = http.ListenAndServe(":0", r)
}
EOF
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
for m in $CANDIDATES; do
  # No `|| echo warn`: a candidate that cannot be fetched must fail the build.
  # Warning and continuing would produce an image whose mirror silently lacks a
  # destination the instruction offers, and the agent would discover it as an
  # unexplainable ENOTCACHED halfway through the migration.
  go mod download "$m"
done
go mod tidy
go build -o /tmp/chi-smoke . && echo "  chi smoke build: OK"
go mod graph > /dev/null

# ---------------------------------------------------------------------------
# Materialise the two proxies.
# ---------------------------------------------------------------------------
CACHE="$(go env GOMODCACHE)/cache/download"

say "baseline proxy -> $BASELINE"
mkdir -p "$BASELINE" && cp -a "$CACHE/." "$BASELINE/"
test -f "$BASELINE/github.com/gin-gonic/gin/@v/v1.8.1.zip" \
  || { echo "FAIL: baseline proxy has no gin zip" >&2; exit 1; }

say "delivery proxy -> $DELIVERY"
mkdir -p "$DELIVERY" && cp -a "$CACHE/." "$DELIVERY/"

say "retire by omission (.zip deleted, .mod kept)"
while read -r mod; do
  [ -n "$mod" ] || continue
  case "$mod" in \#*) continue ;; esac
  # module paths are case-encoded in the proxy layout (!upper); these three are
  # all-lowercase, so a direct path is correct.
  d="$DELIVERY/$mod/@v"
  if [ ! -d "$d" ]; then
    echo "FAIL: retired module $mod is not in the proxy at all" >&2
    echo "      (a typo here would silently un-ban it)" >&2
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
# Anything left here would be an unpacked Gin the agent could build against.
rm -rf /tmp/warm-modcache "$WARM" "$SCRATCH"

say "sizes"
du -sh "$BASELINE" "$DELIVERY"
echo "goproxy build complete"
