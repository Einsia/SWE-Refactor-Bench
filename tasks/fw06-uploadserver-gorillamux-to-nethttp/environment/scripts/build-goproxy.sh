#!/bin/bash
# =============================================================================
# Build the two offline Go module proxies for fw06.
#
#   /opt/goproxy-baseline  the complete State A closure, gorilla/mux included.
#                          Used once, to build the read-only State A oracle,
#                          then deleted from the image.
#   /opt/goproxy           the delivery mirror.  Everything a mux-free port may
#                          legitimately need, and NO .zip for gorilla/mux --
#                          nor for any other third-party router.  This is also
#                          the verifier's mirror.
#
# Runs at image build time, where the network IS available.  The trial and the
# verifier both run with network_mode = "no-network", so this proxy is the only
# module source that exists for them.
#
# Warming has to be done FOUR ways, not one.  Measured, not assumed:
#   go mod download        build-closure zips
#   go mod download all    test-closure zips
#   go mod graph           .mod for every node of the pruned module graph
#   go mod tidy            .mod files tidy wants that no build ever fetches
# Warming only the first pass leaves `go mod tidy` failing offline on a module
# unrelated to anything the agent touched.
#
# THE MIRROR STOCKS NO ROUTER.  This is the deliberate difference from fw04,
# which retires Gin and stocks chi/mux/httprouter because a Gin-free port still
# needs a router.  fw06 retires the router *category*: Go 1.22 gave
# net/http.ServeMux method patterns ("POST /upload") and wildcards
# ("/files/{path...}"), which is everything this repository's routes need.
# So there is no State B candidate list here, and its absence is not an
# oversight -- adding one would be offering a destination the task forbids.
# =============================================================================
set -euo pipefail

REPO=${1:?usage: build-goproxy.sh <repo-dir>}
RETIRED_FILE=${SRB_RETIRED_MODULES:-/opt/srb/retired-modules.txt}
BANNED_FILE=${SRB_BANNED_ROUTERS:-/opt/srb/banned-routers.txt}
BASELINE=/opt/goproxy-baseline
DELIVERY=/opt/goproxy

export GOTOOLCHAIN=local
export GOFLAGS=-mod=mod
export CGO_ENABLED=0

# Warm through a THROWAWAY module cache.
#
# This matters more than it looks.  A proxy ban only bites when the module is
# not already unpacked in GOMODCACHE: `go build` is perfectly happy to use an
# extracted github.com/gorilla/mux@v1.8.1 without consulting the proxy at all.
# Warming into the agent's real GOMODCACHE would therefore hand the agent a
# working mux and quietly void the entire constraint.  The agent's cache is
# populated later, from the pruned delivery proxy, where mux cannot appear.
export GOMODCACHE=/tmp/warm-modcache
rm -rf "$GOMODCACHE"
# GOPRIVATE must stay empty.  GOPRIVATE='*' forces direct VCS mode and silently
# bypasses a file:// proxy entirely; the symptom is a burst of GitHub 403s in a
# container that is meant to be offline.
unset GOPRIVATE GONOSUMDB GONOSUMCHECK || true

say() { printf '\n=== %s\n' "$*"; }

# Warm from a COPY, and restore the snapshot's own go.mod/go.sum afterwards.
# `go mod tidy` rewrites both files, and `go mod download all` resolves the test
# closure -- which for this repository appends six go.sum lines
# (golang.org/x/mod, x/sync, x/tools) that no build of it ever needs.  Letting
# those edits reach the snapshot would ship a State A whose go.sum does not
# match the upstream commit it claims to be, and the contamination survives into
# every trial.
WARM=/tmp/warm
rm -rf "$WARM" && cp -a "$REPO" "$WARM" && chmod -R u+w "$WARM"
cd "$WARM"

say "1/4 go mod download (build closure)"
go mod download

say "2/4 go mod download all (test closure)"
go mod download all

say "3/4 go mod graph (pruned graph .mod files)"
go mod graph > /dev/null

say "4/4 go mod tidy (.mod files tidy wants that no build fetches)"
go mod tidy

# ---------------------------------------------------------------------------
# Materialise the two proxies.
# ---------------------------------------------------------------------------
CACHE="$(go env GOMODCACHE)/cache/download"

say "baseline proxy -> $BASELINE"
mkdir -p "$BASELINE" && cp -a "$CACHE/." "$BASELINE/"
test -f "$BASELINE/github.com/gorilla/mux/@v/v1.8.1.zip" \
  || { echo "FAIL: baseline proxy has no gorilla/mux zip" >&2; exit 1; }

say "delivery proxy -> $DELIVERY"
mkdir -p "$DELIVERY" && cp -a "$CACHE/." "$DELIVERY/"

say "retire by omission (.zip deleted, .mod kept)"
while read -r mod; do
  [ -n "$mod" ] || continue
  case "$mod" in \#*) continue ;; esac
  # module paths are case-encoded in the proxy layout (!upper); this one is
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

# ---------------------------------------------------------------------------
# No other router may be reachable either.  Nothing above ever fetched one, so
# this loop should find nothing -- which is the point: it is a standing check
# that a future edit to the warming steps cannot quietly stock a replacement
# router and turn the task back into fw04.
# ---------------------------------------------------------------------------
say "assert no third-party router is reachable"
leaked=0
while read -r mod; do
  [ -n "$mod" ] || continue
  case "$mod" in \#*) continue ;; esac
  if find "$DELIVERY" -path "$DELIVERY/$mod*" -name '*.zip' | grep -q .; then
    echo "FAIL: banned router $mod has a zip in the delivery proxy" >&2
    leaked=1
  fi
done < "$BANNED_FILE"
[ "$leaked" = 0 ] || exit 1
echo "  no banned router has a zip in $DELIVERY"

# ---------------------------------------------------------------------------
# Rehearse the migration's last step against the PRUNED mirror.
#
# The port ends with `go mod tidy` dropping the gorilla/mux require.  If that
# needed a network fetch the honest agent would be stuck, and we would only find
# out at grading time.  So rehearse it here -- after the pruning, with GOPROXY
# pointed at the delivery mirror and nothing else, which is exactly the agent's
# situation.
#
# The rehearsal uses a SYNTHETIC module, not a rewritten copy of the repository.
# Two reasons.  It must not depend on regex-editing pkg/server.go, which would
# make this script fail the day upstream reformats a line.  And more importantly,
# a rewritten pkg/server.go is a partial solution: `docker build` commits every
# RUN layer, so an intermediate file survives in the image's layer tarball even
# after a later `rm -rf`, and an agent who runs `docker save` can read it.  The
# synthetic module carries State A's own go.mod/go.sum -- so tidy faces the real
# require list -- but none of its routing.
#
# It doubles as the stdlib smoke build: the Go 1.22 pattern syntax the
# instruction points at has to actually compile against this mirror.
# ---------------------------------------------------------------------------
say "post-migration rehearsal + stdlib ServeMux smoke build (offline, pruned mirror)"
REHEARSE=/tmp/rehearse
rm -rf "$REHEARSE" && mkdir -p "$REHEARSE" && cd "$REHEARSE"
cp "$REPO/go.mod" "$REPO/go.sum" .
mkdir -p pkg
# Imports every State A dependency EXCEPT the router, and routes with the Go
# 1.22 syntax.  If tidy cannot resolve this offline, neither can the agent.
cat > main.go <<'EOF'
package main

import (
	"net/http"

	"dario.cat/mergo"
	"github.com/google/uuid"
	"github.com/spf13/afero"
)

func main() {
	var fs afero.Fs = afero.NewMemMapFs()
	_ = uuid.New().String()
	_ = mergo.Merge(&struct{ A string }{}, struct{ A string }{})

	r := http.NewServeMux()
	r.Handle("POST /upload", http.NotFoundHandler())
	r.Handle("OPTIONS /upload", http.NotFoundHandler())
	for _, p := range []string{"/files", "/files/{path...}"} {
		r.Handle("GET "+p, http.NotFoundHandler())
		r.Handle("HEAD "+p, http.NotFoundHandler())
		r.HandleFunc("PUT "+p, func(w http.ResponseWriter, req *http.Request) {
			_, _ = fs.Stat(req.PathValue("path"))
		})
	}
	r.Handle("/", http.NotFoundHandler())
	_ = http.ListenAndServe(":0", r)
}
EOF
env GOPROXY="file://$DELIVERY" GOFLAGS=-mod=mod GOSUMDB=off GONOSUMDB= GOPRIVATE= \
    GOMODCACHE=/tmp/rehearse-modcache \
  bash -c 'go mod tidy && go build -o /tmp/rehearse-bin .' \
  || { echo "FAIL: a router-free module does not resolve offline against the delivery mirror" >&2
       echo "      (the honest migration would dead-end here)" >&2; exit 1; }
echo "  offline tidy + build: OK"
if grep -q 'github.com/gorilla/mux' go.mod; then
  echo "FAIL: rehearsal tidy left the mux require in go.mod" >&2; exit 1
fi
echo "  tidy dropped the gorilla/mux require by itself"

say "discard the warming cache"
# Anything left here would be an unpacked mux the agent could build against.
rm -rf /tmp/warm-modcache /tmp/rehearse-modcache "$WARM" "$REHEARSE" /tmp/rehearse-bin

say "sizes"
du -sh "$BASELINE" "$DELIVERY"
echo "goproxy build complete"
