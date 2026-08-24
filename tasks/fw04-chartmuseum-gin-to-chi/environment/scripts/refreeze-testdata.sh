#!/bin/bash
# =============================================================================
# CONSTRUCTION ONLY.  Not COPYed into any image; never runs during a build.
#
# Regenerates testdata-frozen/ by packaging and signing the charts with helm,
# then rewrites MANIFEST.sha256 and MANIFEST.aggregate.
#
# Running this INVALIDATES THE RECORDED CORPUS.  helm stamps time.Now() into
# every tar member header and into every provenance signature, so a re-freeze
# produces different bytes for the same charts, different sha256 digests, and
# therefore a different index.yaml everywhere the suite compares one.  After
# running it you must re-capture the golden corpus against the oracle and mirror
# the new frozen tree into tests/testdata-frozen/.
#
# It exists so the fixtures are reproducible as a procedure even though they are
# not reproducible as bytes: whoever inherits this task can see exactly how the
# committed artifacts were made, with which helm, from which sources, signed by
# which key.
#
# Usage, from the task directory, with the environment image already built:
#
#   docker run --rm \
#     -v "$PWD/environment/testdata-frozen:/frozen" \
#     -v "$PWD/environment/scripts:/scripts:ro" \
#     srb-fw04-env:v1 bash /scripts/refreeze-testdata.sh /workspace/repo /frozen
#
# Then: diff the manifest, mirror to tests/testdata-frozen/, re-capture.
# =============================================================================
set -euo pipefail

REPO=${1:?usage: refreeze-testdata.sh <repo-dir> <frozen-dir>}
FROZEN=${2:?usage: refreeze-testdata.sh <repo-dir> <frozen-dir>}
HELM=${HELM_BIN:-/opt/helm/helm}

test -x "$HELM" || { echo "FAIL: no helm at $HELM" >&2; exit 1; }
KEYRING="$REPO/testdata/pgp/helm-test-key.secret"
test -f "$KEYRING" || { echo "FAIL: missing test keyring" >&2; exit 1; }

# Work on a scratch copy: the point is to produce artifacts, not to mutate the
# repo the agent will be handed.
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
cp -a "$REPO/testdata" "$WORK/testdata"

export XDG_CACHE_HOME="$WORK/cache" XDG_CONFIG_HOME="$WORK/config" XDG_DATA_HOME="$WORK/data"
mkdir -p "$XDG_CACHE_HOME" "$XDG_CONFIG_HOME" "$XDG_DATA_HOME"

cd "$WORK/testdata/charts"
echo "=== packaging signed charts (helm $("$HELM" version --short 2>/dev/null || echo '?')) ==="
for d in $(find . -maxdepth 1 -mindepth 1 -type d | LC_ALL=C sort); do
  ( cd "$d" && "$HELM" package --sign --key helm-test \
        --keyring ../../pgp/helm-test-key.secret . )
done

# Extra versions the corpus needs beyond each chart's own Chart.yaml version:
#   0.2.0 -> a second version of mychart, so the metrics gauges are not 1
#   0.0.1 -> a third, so --per-chart-limit has something to evict
"$HELM" package --sign --key helm-test --keyring ../pgp/helm-test-key.secret \
    --version 0.2.0 -d mychart/ mychart/.
"$HELM" package --sign --key helm-test --keyring ../pgp/helm-test-key.secret \
    --version 0.0.1 -d mychart/ mychart/.

# Every signature must verify against the committed public key before anything
# is frozen.  A fixture that helm itself would reject is worse than no fixture:
# it would make the provenance cases test the wrong thing, silently.
echo "=== verifying signatures ==="
for t in $(find . -name '*.tgz' | LC_ALL=C sort); do
  "$HELM" verify --keyring "$WORK/testdata/pgp/helm-test-key.pub" "$t" >/dev/null \
    || { echo "FAIL: $t does not verify" >&2; exit 1; }
  echo "  verified $t"
done

# --- the path-traversal fixture must not have been repackaged ----------------
# `helm package` over badcharts/ is what upstream's setup script does and what
# would silently turn a security fixture into a well-formed chart.  Nothing above
# touches that directory; this proves it rather than trusting it.
BAD=testdata/badcharts/mybadchart/mybadchart-1.0.0.tgz
for suffix in "" ".prov"; do
  a=$(sha256sum "$REPO/$BAD$suffix" | cut -d' ' -f1)
  b=$(sha256sum "$WORK/$BAD$suffix" | cut -d' ' -f1)
  [ "$a" = "$b" ] || { echo "FAIL: $BAD$suffix was rewritten by packaging" >&2; exit 1; }
done
echo "path-traversal fixture intact"

# --- freeze the whole fixture tree ------------------------------------------
# Everything the oracle or the verifier reads at runtime, so that neither has to
# read anything out of the repository under test.  See install-testdata.sh.
echo "=== freezing to $FROZEN ==="
rm -rf "$FROZEN"; mkdir -p "$FROZEN/webtemplate/empty"
( cd "$WORK" && tar cf - testdata ) | ( cd "$FROZEN" && tar xf - )
cp -a "$REPO/pkg/chartmuseum/server/multitenant/testdata/template" \
      "$FROZEN/webtemplate/template"
: > "$FROZEN/webtemplate/empty/.keep"
test -f "$FROZEN/webtemplate/template/index.html"
test -f "$FROZEN/webtemplate/template/static/main.css"

( cd "$FROZEN" && find testdata webtemplate -type f \
    | LC_ALL=C sort | xargs sha256sum ) > "$FROZEN/MANIFEST.sha256"
( cd "$FROZEN" && sha256sum MANIFEST.sha256 | cut -d' ' -f1 ) > "$FROZEN/MANIFEST.aggregate"

cat "$FROZEN/MANIFEST.sha256"
echo "aggregate: $(cat "$FROZEN/MANIFEST.aggregate")"
echo "files: $(grep -c . "$FROZEN/MANIFEST.sha256")"
cat >&2 <<'WARN'

*** The recorded corpus is now stale. ***
    Mirror this tree into tests/testdata-frozen/, update SRB_TESTDATA_AGGREGATE
    in both Dockerfiles, and re-capture the golden corpus.
WARN
