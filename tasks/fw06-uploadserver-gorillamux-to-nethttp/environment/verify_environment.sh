#!/usr/bin/env bash
# =============================================================================
# Image-build-time verification of the finished environment.
#
# Runs as the last layer, and is deleted in the same layer, so the agent never
# receives it.  It knows nothing about the grading ladder and reports nothing to
# anyone: it either lets the build finish or it fails it.
#
# WHAT THIS DOES NOT DO, AND WHY.  It does not prove that a migration's
# dependency closure resolves offline, or that ServeMux's 1.22+ pattern syntax
# compiles against the delivery mirror.  build-goproxy.sh already does both, at
# layer 12, and does them better than a check here could: its rehearsal uses
# State A's real go.mod and go.sum, runs `go mod tidy` against the actual require
# list, and additionally asserts that tidy drops the gorilla/mux requirement on
# its own.  A second, weaker copy of that proof here would be duplication that
# reads like thoroughness.  A first draft of this file was exactly that, and it
# failed for a reason worth recording: it omitted State A's `// indirect` pin on
# golang.org/x/text, so version selection chose afero's own minimum v0.28.0,
# which the mirror does not carry.  The mirror was right and the check was wrong.
#
# WHAT IS LEFT FOR THIS FILE is everything that is only true, or only checkable,
# at the END of the build.  build-goproxy.sh runs at layer 12 and fourteen layers
# follow it: an oracle gets built and frozen, a proxy gets deleted, a module
# cache gets warmed, a git repo gets created, and two probes compile things.  Any
# of those can leave the image in a state no earlier assertion covers.
#
#   1. /workspace/repo is byte-identical to the frozen snapshot, and git-clean at
#      exactly one commit.  Several later layers read from it and two copy it;
#      nothing until now has confirmed none of them wrote to it.
#   2. The oracle survived `chmod -R a-w` with its execute bit, still runs, and
#      still serves the frozen fixture bytes.  It is the agent's entire feedback
#      loop, and a read-only directory is easy to make unusable rather than
#      merely unwritable.
#   3. The module cache the agent inherits holds no retired module and no banned
#      router -- re-checked here because the two probes that ran after the
#      warming step both compile Go, and a probe that forgot to override
#      GOMODCACHE would have extracted a router into it.
#   4. None of the artefacts this environment deliberately does not ship are
#      present.  This is the assertion with the most history behind it: an
#      earlier revision of this task shipped the verifier's graded case list, a
#      differential tool that replayed it and printed IDENTICAL, and a static
#      analyser that returned the same verdict one of the audit gates
#      returns.  Together they let an agent read the suite's size, its
#      partitioning and its rationale, and check whether it would pass before
#      submitting.  They are gone.  A list of paths that must not exist is how
#      they stay gone, because the next person to add a helper here will not have
#      read the commit that removed them.
#   5. The agent's tool surface is exactly what was intended: oracle-serve, and
#      nothing else on PATH that this task installed.
# =============================================================================
set -euo pipefail

fail() { echo "FATAL: $*" >&2; exit 1; }
log()  { echo "== $*"; }

# -----------------------------------------------------------------------------
# 1. The trial tree is the frozen snapshot, untouched.
#
# Compared against the snapshot tarball rather than against a recorded constant:
# the tarball is already digest-verified at layer 6, so extracting it again to a
# scratch path and diffing gives an independent answer instead of a restatement.
# -----------------------------------------------------------------------------
log "checking /workspace/repo against the frozen snapshot"
SCRATCH=$(mktemp -d)
trap 'rm -rf "$SCRATCH"' EXIT

tar xzf /opt/snapshot/original.tar.gz -C "$SCRATCH"
test -d "$SCRATCH/repo" || fail "the snapshot does not contain a repo/ directory"

# .git is ours, added at layer 22, and is not in the snapshot.
if ! diff -r --exclude=.git "$SCRATCH/repo" /workspace/repo > "$SCRATCH/tree.diff" 2>&1; then
  echo "--- differences ---" >&2
  head -40 "$SCRATCH/tree.diff" >&2
  fail "/workspace/repo differs from the frozen snapshot.  Some layer wrote to the
       tree the agent is handed, so trials would not all start from State A --
       and whichever layer did it also invalidated the digest that makes 'the
       original passed this' a statement about the submission."
fi

cd /workspace/repo
[ "$(git rev-list --count HEAD)" = "1" ] \
  || fail "expected exactly one baseline commit, found $(git rev-list --count HEAD)"
[ -z "$(git status --porcelain)" ] \
  || { git status --porcelain >&2; fail "/workspace/repo is dirty"; }
[ -z "$(git remote)" ] || fail "the baseline repo has a remote"
[ -z "$(git tag)"    ] || fail "the baseline repo has tags"
log "trial tree matches the snapshot; git is clean at one commit"

# -----------------------------------------------------------------------------
# 2. The oracle works, and is read-only.
# -----------------------------------------------------------------------------
log "checking the oracle"
BIN=${SRB_ORACLE:-/opt/oracle/simple-upload-server}
test -x "$BIN" || fail "no oracle binary at $BIN"

# The mode bits `chmod -R a-w` is supposed to have produced.  Tested as bits
# rather than with `[ -w ]`, which asks access(W_OK) and answers "yes" for uid 0
# on any regular file no matter what the mode says -- a first draft used it and
# failed this check on a correctly frozen binary.
#
# And the bits are all this claims.  The trial runs as root, so an agent that
# wants to overwrite the oracle can chmod it first, and no permission in a root
# container prevents that.  What the read-only mode buys is protection from
# accident: a stray `go build -o $SRB_ORACLE` or a copy into /opt/oracle fails
# loudly instead of silently replacing the reference the agent is comparing
# against.  It is not a security boundary and does not need to be -- grading
# never reads this file.  The verifier is a separate image that builds State A
# from its own frozen snapshot and its own mirror, so a corrupted oracle costs
# the agent its feedback loop and costs the grade nothing.
mode=$(stat -c '%A' "$BIN")
case "$mode" in
  *w*) fail "the oracle binary is mode $mode; chmod -R a-w should have cleared
       every write bit.  An accidental overwrite would silently replace the
       reference the agent compares against." ;;
esac
[ -x "$BIN" ] || fail "the oracle binary is not executable (mode $mode)"

oracle-serve start --port 8897 >/dev/null 2>&1 \
  || fail "oracle-serve could not start the frozen oracle"
code=$(curl -s -o "$SCRATCH/a.txt" -w '%{http_code}' http://127.0.0.1:8897/files/a.txt || echo 000)
opt=$(curl -s -o /dev/null -w '%{http_code}' -X OPTIONS http://127.0.0.1:8897/upload || echo 000)
oracle-serve stop --port 8897 >/dev/null 2>&1 || true

[ "$code" = "200" ] || fail "the oracle did not serve /files/a.txt (got ${code})"
[ "$opt"  = "204" ] || fail "the oracle did not answer OPTIONS /upload with 204 (got ${opt})"
cmp -s "$SCRATCH/a.txt" /opt/fixtures/docroot/a.txt \
  || fail "the oracle served bytes that are not the frozen fixture's"
log "the oracle serves the frozen fixtures and is read-only"

# The build-time probe leaves its run directory behind -- oracle-serve's stop
# removes the pidfile only, deliberately, so an agent can still read the log of a
# server it has stopped.  Harmless (a re-seeded copy of a tree the agent already
# has at /opt/fixtures/docroot) but it is stale state in a shipped image, and
# `start` would silently re-seed it anyway.
rm -rf /tmp/oracle
log "removed the build-time oracle run directory"

# -----------------------------------------------------------------------------
# 3. The inherited module cache carries no router.
# -----------------------------------------------------------------------------
log "checking the module cache the agent inherits"
for f in "${SRB_RETIRED_MODULES:-/opt/srb/retired-modules.txt}" \
         "${SRB_BANNED_ROUTERS:-/opt/srb/banned-routers.txt}"; do
  test -f "$f" || fail "missing module list $f"
  while read -r mod; do
    [ -n "$mod" ] || continue
    if [ -e "/opt/go/pkg/mod/$mod" ] || \
       [ -n "$(find "/opt/go/pkg/mod/cache/download/$mod" -name '*.zip' 2>/dev/null)" ]; then
      fail "$mod is extracted in the module cache the agent inherits.  A build
           that does not override GOMODCACHE compiles against the cache without
           consulting GOPROXY at all, so the retirement would not be in effect."
    fi
  done < <(sed -e 's/#.*//' -e '/^[[:space:]]*$/d' "$f" | tr -d '[:blank:]')
done
log "no retired module or banned router is extracted in the cache"

# -----------------------------------------------------------------------------
# 4. Nothing this environment must not ship is present.
# -----------------------------------------------------------------------------
log "checking that no grader-side artefact is present"
must_not_exist=(
  # The verifier's graded case list, and the tool that replayed it against the
  # oracle and reported whether the behavioural suite would pass.
  /opt/srb/probe
  /opt/srb/sample-corpus.json
  /usr/local/bin/swerefactor-diff
  /opt/srb/scripts/swerefactor-diff
  # A self-check that told the agent its own verdict.
  /usr/local/bin/swerefactor-selfcheck
  /opt/srb/scripts/swerefactor-selfcheck
  # The static analyser behind one of the audit gates, and its digest.
  /usr/local/bin/astscan
  /opt/srb/scripts/astscan
  /opt/srb/astscan.sha256
  # The baseline proxy: the only place gorilla/mux's source ever existed here.
  /opt/goproxy-baseline
)
for p in "${must_not_exist[@]}"; do
  [ -e "$p" ] && fail "$p is present in the agent image.  This environment ships
       the oracle and nothing that reports a verdict; see the header."
done

# The same rule stated as content rather than as paths, since a renamed file
# would pass the list above.  These three strings appear in the verifier's corpus
# module and nowhere legitimate on this side.
if grep -rl --binary-files=without-match \
     -e 'full_case_count' -e 'def all_cases' -e 'prefix-not-segment' \
     /opt/srb /opt/oracle /usr/local/bin 2>/dev/null | grep -q .; then
  grep -rl --binary-files=without-match \
    -e 'full_case_count' -e 'def all_cases' -e 'prefix-not-segment' \
    /opt/srb /opt/oracle /usr/local/bin 2>/dev/null >&2
  fail "a file in the agent image carries the verifier's case list."
fi
log "no graded case list, verdict tool or gate analyser is present"

# -----------------------------------------------------------------------------
# 5. The tool surface is exactly what was intended.
# -----------------------------------------------------------------------------
log "checking the agent's tool surface"
installed=$(ls /usr/local/bin | LC_ALL=C sort | tr '\n' ' ')
[ "$installed" = "oracle-serve " ] \
  || fail "unexpected tooling on PATH: '$installed' (expected 'oracle-serve ').
       Anything added here is something the agent can run, so it needs the same
       scrutiny the removed self-check got."

bash -lc 'command -v go >/dev/null' \
  || fail "go is not on PATH in a login shell; agents do use login shells"
log "tool surface is oracle-serve, and go resolves in a login shell"

log "environment verified"
