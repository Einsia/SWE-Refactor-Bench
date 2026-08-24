#!/bin/bash
# =============================================================================
# Warm the agent's module cache -- from the DELIVERY proxy only.
#
# Why this is a separate step, run after the baseline proxy is deleted:
#
#   A proxy ban only bites when the module is not already unpacked in
#   GOMODCACHE.  `go build` will use an extracted github.com/gin-gonic/gin@v1.8.1
#   without consulting any proxy at all, so a cache warmed while Gin was still
#   obtainable would silently hand the agent a working Gin and void the task's
#   central constraint.  Everything earlier in the build therefore warms through
#   a throwaway cache and discards it; this step is the only one that writes the
#   cache the agent inherits, and by then the only module source in the image is
#   /opt/goproxy, which has no .zip for any retired module.
#
# What gets warmed: every module version that HAS a .zip in the delivery proxy.
# That is the full `all` closure of State A minus the retired three, plus the
# State B candidates.  The agent's first `go build` and `go test ./...` then
# unpack from a local cache instead of walking the proxy, and -- more usefully --
# a proxy that cannot satisfy a Gin-free build is caught here, at build time,
# instead of in a network-less trial container.
# =============================================================================
set -euo pipefail

DELIVERY=${SRB_GOPROXY_ROOT:-/opt/goproxy}
RETIRED_FILE=${SRB_RETIRED_MODULES:-/opt/srb/retired-modules.txt}
SCRATCH=/tmp/warm-agent

export GOTOOLCHAIN=local
export GOFLAGS=-mod=mod
export GOPROXY="file://$DELIVERY"
export GOSUMDB=off
export CGO_ENABLED=0
# GOPRIVATE='*' forces direct VCS mode and bypasses a file:// proxy; it must stay
# empty or this whole step would quietly go to the network.
unset GOPRIVATE || true

say() { printf '\n=== %s\n' "$*"; }

test -d "$DELIVERY" || { echo "FAIL: no delivery proxy at $DELIVERY" >&2; exit 1; }
test ! -e /opt/goproxy-baseline \
  || { echo "FAIL: the baseline proxy still exists; warming now would cache Gin" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Enumerate module@version for every zip in the proxy.
#
# The proxy layout case-encodes uppercase letters as !lower (github.com/Azure ->
# github.com/!azure), in both the path and the version.  Decode both, or the
# download requests name modules that do not exist.
# ---------------------------------------------------------------------------
say "enumerate the delivery proxy"
python3 - "$DELIVERY" > "$SCRATCH.list" <<'PY'
import os, re, sys

root = sys.argv[1]

def decode(s):
    return re.sub(r'!([a-z])', lambda m: m.group(1).upper(), s)

out = []
for dirpath, _dirnames, filenames in os.walk(root):
    if os.path.basename(dirpath) != '@v':
        continue
    mod = decode(os.path.relpath(os.path.dirname(dirpath), root))
    for fn in filenames:
        if fn.endswith('.zip'):
            out.append('%s@%s' % (mod, decode(fn[:-4])))
for line in sorted(out):
    print(line)
PY

total=$(wc -l < "$SCRATCH.list")
echo "  $total module versions have a zip"
test "$total" -gt 100 || { echo "FAIL: implausibly small proxy" >&2; exit 1; }

# A retired module appearing here would mean the retire loop failed to prune it.
say "assert no retired module is downloadable"
while read -r mod; do
  [ -n "$mod" ] || continue
  case "$mod" in \#*) continue ;; esac
  if grep -q "^$mod@" "$SCRATCH.list"; then
    echo "FAIL: retired module $mod still has a zip in $DELIVERY" >&2
    exit 1
  fi
  printf '  %-45s no zip: ok\n' "$mod"
done < "$RETIRED_FILE"

# ---------------------------------------------------------------------------
# Download them.  A scratch module, because `go mod download m@v` needs a main
# module to record sums into and must not touch /workspace/repo.
# ---------------------------------------------------------------------------
say "populate $(go env GOMODCACHE)"
rm -rf "$SCRATCH" && mkdir -p "$SCRATCH" && cd "$SCRATCH"
printf 'module swerefactor.local/warm\n\ngo 1.17\n' > go.mod

# Batched: `xargs -n 60` with no command prints 60 arguments per line, so each
# read gets one batch.  Batching keeps argv small and lets a failure name a
# specific batch instead of disappearing.
failed=0
while read -r batch; do
  # shellcheck disable=SC2086
  go mod download $batch || { echo "  (warn: a module in this batch failed)"; failed=$((failed + 1)); }
done < <(xargs -n 60 < "$SCRATCH.list")

echo "  batches with a failure: $failed"

# ---------------------------------------------------------------------------
# The two properties that matter.
# ---------------------------------------------------------------------------
say "assert Gin is NOT unpacked in the agent's cache"
MODCACHE=$(go env GOMODCACHE)
while read -r mod; do
  [ -n "$mod" ] || continue
  case "$mod" in \#*) continue ;; esac
  # An extracted module lives at $GOMODCACHE/<path>@<version>/ -- that directory,
  # not the proxy, is what `go build` actually reads.  All three retired paths are
  # lowercase, so the unencoded glob is exact.
  hits=$(compgen -G "$MODCACHE/$mod@*" || true)
  if [ -n "$hits" ]; then
    echo "FAIL: $mod is unpacked in $MODCACHE -- the ban would not hold" >&2
    echo "$hits" >&2
    exit 1
  fi
  printf '  %-45s not unpacked: ok\n' "$mod"
done < "$RETIRED_FILE"

say "assert a Gin-free router IS obtainable offline"
mkdir -p "$SCRATCH/smoke" && cd "$SCRATCH/smoke"
printf 'module swerefactor.local/smoke\n\ngo 1.17\n' > go.mod
cat > main.go <<'EOF'
package main

import (
	"net/http"

	"github.com/go-chi/chi/v5"
	"github.com/go-chi/chi/v5/middleware"
)

func main() {
	r := chi.NewRouter()
	r.Use(middleware.Recoverer)
	r.Get("/health", func(w http.ResponseWriter, _ *http.Request) {
		w.Write([]byte(`{"healthy":true}`))
	})
	_ = http.ListenAndServe(":0", r)
}
EOF
go mod tidy
go build -o /tmp/chi-offline-smoke .
echo "  chi builds from $DELIVERY with no network: OK"

cd /
rm -rf "$SCRATCH" "$SCRATCH.list" /tmp/chi-offline-smoke

say "sizes"
du -sh "$MODCACHE" "$DELIVERY"
echo "module cache warmed"
