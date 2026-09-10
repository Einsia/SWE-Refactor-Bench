#!/bin/bash
# =============================================================================
# Warm the agent's module cache -- from the DELIVERY proxy only.
#
# Why this is a separate step, run after the baseline proxy is deleted:
#
#   A proxy ban only bites when the module is not already unpacked in
#   GOMODCACHE.  `go build` will use an extracted github.com/gorilla/mux@v1.8.1
#   without consulting any proxy at all, so a cache warmed while mux was still
#   obtainable would silently hand the agent a working mux and void the task's
#   central constraint.  Everything earlier in the build therefore warms through
#   a throwaway cache and discards it; this step is the only one that writes the
#   cache the agent inherits, and by then the only module source in the image is
#   /opt/goproxy, which has no .zip for any retired module.
#
# What gets warmed: every module version that HAS a .zip in the delivery proxy.
# For fw06 that is State A's `all` closure minus gorilla/mux -- seven modules,
# small enough to pin by name below.
# =============================================================================
set -euo pipefail

DELIVERY=${SRB_GOPROXY_ROOT:-/opt/goproxy}
RETIRED_FILE=${SRB_RETIRED_MODULES:-/opt/srb/retired-modules.txt}
BANNED_FILE=${SRB_BANNED_ROUTERS:-/opt/srb/banned-routers.txt}
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
  || { echo "FAIL: the baseline proxy still exists; warming now would cache mux" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Enumerate module@version for every zip in the proxy.
#
# The proxy layout case-encodes uppercase letters as !lower (github.com/Azure ->
# github.com/!azure), in both the path and the version.  Decode both, or the
# download requests name modules that do not exist.  None of fw06's modules
# actually contain an uppercase letter, but the decoder stays: it costs nothing
# and its absence would be a latent bug the day a dependency changes.
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

# ---------------------------------------------------------------------------
# Pin the exact expected set, not a threshold.
#
# fw04 asserts `> 100` because its closure is a hundred-odd modules and
# enumerating them would be noise.  fw06's is seven, so the exact set is
# cheap -- and strictly better: a threshold passes when a module silently
# appears or disappears, an exact set does not.  If this list needs editing,
# something about State A's dependency closure changed and that deserves to be
# noticed deliberately rather than absorbed.
#
# x/mod, x/sync and x/tools are here because `go mod download all` resolves the
# TEST closure, which reaches afero's own test dependencies.  No build of this
# repository needs them; they are in the mirror so that `go mod download all`
# and `go mod tidy` also work offline for the agent.
# ---------------------------------------------------------------------------
say "assert the delivery proxy's zip set is exactly what fw06 expects"
cat > "$SCRATCH.expected" <<'EOF'
dario.cat/mergo@v1.0.2
github.com/google/uuid@v1.6.0
github.com/spf13/afero@v1.15.0
golang.org/x/mod@v0.35.0
golang.org/x/sync@v0.20.0
golang.org/x/text@v0.37.0
golang.org/x/tools@v0.44.0
EOF
if ! diff -u "$SCRATCH.expected" "$SCRATCH.list"; then
  echo "FAIL: the delivery proxy's module set drifted from the pinned expectation" >&2
  echo "      (left = expected, right = actual; update this list only on purpose)" >&2
  exit 1
fi
echo "  7 module versions, exactly as pinned"

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

say "assert no third-party router is downloadable"
while read -r mod; do
  [ -n "$mod" ] || continue
  case "$mod" in \#*) continue ;; esac
  if grep -q "^$mod" "$SCRATCH.list"; then
    echo "FAIL: banned router $mod has a zip in $DELIVERY" >&2
    exit 1
  fi
done < "$BANNED_FILE"
echo "  no banned router has a zip"

# ---------------------------------------------------------------------------
# Download them.  A scratch module, because `go mod download m@v` needs a main
# module to record sums into and must not touch /workspace/repo.
# ---------------------------------------------------------------------------
say "populate $(go env GOMODCACHE)"
rm -rf "$SCRATCH" && mkdir -p "$SCRATCH" && cd "$SCRATCH"
printf 'module swerefactor.local/warm\n\ngo 1.25.0\n' > go.mod

# shellcheck disable=SC2046
go mod download $(cat "$SCRATCH.list")

# ---------------------------------------------------------------------------
# The two properties that matter.
# ---------------------------------------------------------------------------
say "assert gorilla/mux is NOT unpacked in the agent's cache"
MODCACHE=$(go env GOMODCACHE)
while read -r mod; do
  [ -n "$mod" ] || continue
  case "$mod" in \#*) continue ;; esac
  # An extracted module lives at $GOMODCACHE/<path>@<version>/ -- that directory,
  # not the proxy, is what `go build` actually reads.
  hits=$(compgen -G "$MODCACHE/$mod@*" || true)
  if [ -n "$hits" ]; then
    echo "FAIL: $mod is unpacked in $MODCACHE -- the ban would not hold" >&2
    echo "$hits" >&2
    exit 1
  fi
  printf '  %-45s not unpacked: ok\n' "$mod"
done < "$RETIRED_FILE"

# ---------------------------------------------------------------------------
# Prove the destination is reachable offline.
#
# fw04's equivalent smoke-builds chi.  fw06's destination is the standard
# library, which needs no module at all -- so the thing worth proving is
# different: that the TOOLCHAIN in this image actually supports the Go 1.22
# pattern syntax the instruction tells the agent to use.  A toolchain too old
# would accept "POST /upload" as a literal path and route nothing, silently.
# ---------------------------------------------------------------------------
say "assert the toolchain routes Go 1.22 method patterns"
mkdir -p "$SCRATCH/smoke" && cd "$SCRATCH/smoke"
printf 'module swerefactor.local/smoke\n\ngo 1.25.0\n' > go.mod
cat > main_test.go <<'EOF'
package main

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

// Not a compile check -- a behaviour check.  A pre-1.22 toolchain treats
// "POST /upload" as a literal path and would 404 this request instead of
// routing it, and would return "" for the wildcard.
func TestPatternRouting(t *testing.T) {
	r := http.NewServeMux()
	r.HandleFunc("POST /upload", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusCreated)
	})
	r.HandleFunc("GET /files/{path...}", func(w http.ResponseWriter, req *http.Request) {
		_, _ = w.Write([]byte(req.PathValue("path")))
	})

	w := httptest.NewRecorder()
	r.ServeHTTP(w, httptest.NewRequest("POST", "/upload", nil))
	if w.Code != http.StatusCreated {
		t.Fatalf("method pattern did not route: got %d, want 201", w.Code)
	}

	w = httptest.NewRecorder()
	r.ServeHTTP(w, httptest.NewRequest("GET", "/files/a/b.txt", nil))
	if got := w.Body.String(); got != "a/b.txt" {
		t.Fatalf("{path...} wildcard did not capture: got %q, want %q", got, "a/b.txt")
	}

	// And the method filter has to actually filter.
	w = httptest.NewRecorder()
	r.ServeHTTP(w, httptest.NewRequest("DELETE", "/upload", nil))
	if w.Code != http.StatusMethodNotAllowed {
		t.Fatalf("method pattern did not reject DELETE: got %d, want 405", w.Code)
	}
}
EOF
go test ./... 2>&1 | sed 's/^/  /'
echo "  toolchain supports method patterns and {path...}: OK"

cd /
rm -rf "$SCRATCH" "$SCRATCH.list" "$SCRATCH.expected"

say "sizes"
du -sh "$MODCACHE" "$DELIVERY"
echo "module cache warmed"
