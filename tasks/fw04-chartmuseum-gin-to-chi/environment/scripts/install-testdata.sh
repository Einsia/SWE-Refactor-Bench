#!/bin/bash
# =============================================================================
# Publish the frozen fixture tree: /opt/testdata and /opt/webtemplate.
#
# Run identically by the environment image and the verifier image, from
# byte-identical committed copies of testdata-frozen/.  It reads nothing from the
# repository under test.  Both of those properties are load-bearing.
#
# --- why the fixtures are frozen rather than built -------------------------
#
# `helm package` is not byte-reproducible.  chartutil.Save writes every archive
# member's header with time.Now(), so the inner tar carries the wall clock of the
# build that produced it.  Measured, on one machine, from one image, over one
# unchanged source tree: mychart-0.1.0.tgz came out d6155e56... in one build and
# 94004316... in another fifteen minutes later, `tar tvf` showing 16:47 against
# 17:02 on Chart.yaml, values.yaml, templates/pod.yaml and .helmignore.  The gzip
# framing was already deterministic (1f8b 0814 0000 0000 00ff, mtime zeroed); the
# tar headers were not, and normalising the .tgz's own mtime on disk cannot reach
# inside the archive.
#
# index.yaml carries the sha256 of the chart bytes and the differential suite
# compares those digests.  The environment image and the verifier image are
# separate builds, so packaging in each would guarantee a mismatch on every
# digest -- and it would surface as a failing submission rather than as a broken
# harness.
#
# The provenance files cannot be made reproducible either.  A .prov is a
# clearsigned document, and Helm signs through golang.org/x/crypto/openpgp, which
# stamps the signature with time.Now() and exposes no seam to override it.
#
# So the bytes are frozen once, at construction time, and committed.  See
# refreeze-testdata.sh for how they were produced; running it invalidates the
# recorded corpus, by design.
#
# --- why the WHOLE tree is frozen, not just the packaged charts -------------
#
# Taking chart sources, TLS material and the PGP keyring from $REPO/testdata and
# overlaying only the packaged artifacts would be fine in the environment image,
# where $REPO is the pristine snapshot, and wrong in the verifier, where $REPO is
# the submission: it would let a submission choose the certificate its own TLS
# cases are graded with, and it would let a submission that deleted testdata/ turn
# a behavioural failure into a launch failure.  Neither is exploitable for a
# better score, but both make a graded result depend on something the graded party
# controls, which is enough reason not to do it.  The fixtures come from one
# immutable place instead.
#
# --- what is in the tree ---------------------------------------------------
#
# testdata/    the snapshot's own testdata verbatim -- chart sources, the TLS
#              pairs, the PGP test keyring (upstream test material, referenced by
#              name, never echoed) -- plus the 10 packaged .tgz/.tgz.prov
#              artifacts, plus badcharts/mybadchart-1.0.0.tgz{,.prov}.
#
#              That last pair is upstream's, tracked in their git despite their
#              .gitignore, and deliberately malformed: the chart's name is
#              "../../../../charts/org2/repo2/evil", a path-traversal fixture the
#              helm client refuses to produce.  Upstream's
#              setup-test-environment.sh runs `helm package .` over badcharts
#              anyway; doing that here would either fail or, worse, quietly
#              replace a security fixture with a well-formed chart and delete a
#              test case.  It is copied, never packaged.
#
# webtemplate/ what --web-template-path points at.  template/ is the snapshot's
#              own copy from pkg/chartmuseum/server/multitenant/testdata, whose
#              index.html and static/main.css become response bytes the suite
#              compares; empty/ holds one dotfile and exercises the documented
#              fallback for a template path containing no .html.  Frozen for the
#              same reason as the charts: the agent may legitimately move or edit
#              the in-repo copy, and both sides must still render identical bytes.
# =============================================================================
set -euo pipefail

FROZEN=${1:-/opt/srb/testdata-frozen}
OUT=${2:-/opt/testdata}
WEBOUT=${3:-/opt/webtemplate}

# The mtime every published file gets.  ChartMuseum's local filesystem backend
# reports an object's mtime as the index entry's `created:` field, so leaving
# these at the unpack wall clock would put a fresh timestamp into index.yaml on
# every build.  One fixed epoch instead, and `created:` becomes a constant the
# corpus can compare rather than something it has to mask.
EPOCH="2022-07-01T00:00:00Z"

test -f "$FROZEN/MANIFEST.sha256"   || { echo "FAIL: no manifest in $FROZEN" >&2; exit 1; }
test -f "$FROZEN/MANIFEST.aggregate" || { echo "FAIL: no aggregate in $FROZEN" >&2; exit 1; }

# --- the frozen tree must be the tree the manifest describes -----------------
echo "=== verifying frozen fixtures ==="
( cd "$FROZEN" && sha256sum -c --quiet MANIFEST.sha256 ) || {
    echo "FAIL: frozen fixtures do not match their manifest" >&2; exit 1; }

# Nothing in the tree may be unlisted.  sha256sum -c only proves the listed files
# are right; without this, an extra file could ride along in one image and not
# the other and the aggregate would not notice.
listed=$(cut -d' ' -f3- "$FROZEN/MANIFEST.sha256" | LC_ALL=C sort)
present=$(cd "$FROZEN" && find testdata webtemplate -type f | LC_ALL=C sort)
if [ "$listed" != "$present" ]; then
    echo "FAIL: frozen tree and manifest list different files" >&2
    diff <(echo "$listed") <(echo "$present") >&2 || true
    exit 1
fi

want_agg=$(tr -d ' \n' < "$FROZEN/MANIFEST.aggregate")
have_agg=$(cd "$FROZEN" && sha256sum MANIFEST.sha256 | cut -d' ' -f1)
if [ "$want_agg" != "$have_agg" ]; then
    echo "FAIL: manifest aggregate mismatch" >&2
    echo "      recorded $want_agg" >&2
    echo "      computed $have_agg" >&2
    exit 1
fi

# The one value both images hard-code.  If their committed copies of
# testdata-frozen/ ever drift apart, whichever build is wrong stops here instead
# of shipping fixtures the other side has never seen.
if [ -n "${SRB_TESTDATA_AGGREGATE:-}" ] && [ "$SRB_TESTDATA_AGGREGATE" != "$have_agg" ]; then
    echo "FAIL: this image expects testdata aggregate" >&2
    echo "      $SRB_TESTDATA_AGGREGATE" >&2
    echo "      but testdata-frozen/ carries" >&2
    echo "      $have_agg" >&2
    echo "      The environment and verifier copies have drifted apart." >&2
    exit 1
fi
echo "frozen fixtures verified: $(grep -c . "$FROZEN/MANIFEST.sha256") files, aggregate ${have_agg:0:16}..."

# --- publish -----------------------------------------------------------------
echo "=== publishing to $OUT and $WEBOUT ==="
rm -rf "$OUT" "$WEBOUT"
mkdir -p "$OUT" "$WEBOUT"
( cd "$FROZEN" && tar cf - testdata )    | ( cd "$OUT"    && tar xf - )
( cd "$FROZEN" && tar cf - webtemplate ) | ( cd "$WEBOUT" && tar xf - )
# webtemplate/ published one level up: /opt/webtemplate/{template,empty}
mv "$WEBOUT/webtemplate"/* "$WEBOUT/" && rmdir "$WEBOUT/webtemplate"

find "$OUT" "$WEBOUT" -exec touch -h -d "$EPOCH" {} +

# --- every fixture named by a test or a flag must be here -------------------
for f in "$OUT/testdata/charts/mychart/mychart-0.0.1.tgz" \
         "$OUT/testdata/charts/mychart/mychart-0.1.0.tgz" \
         "$OUT/testdata/charts/mychart/mychart-0.1.0.tgz.prov" \
         "$OUT/testdata/charts/mychart/mychart-0.2.0.tgz" \
         "$OUT/testdata/charts/mychart2/mychart2-0.1.0-SNAPSHOT-1.tgz" \
         "$OUT/testdata/charts/mychart2/mychart2-0.1.0-SNAPSHOT-1.tgz.prov" \
         "$OUT/testdata/charts/otherchart/otherchart-0.1.0.tgz" \
         "$OUT/testdata/charts/otherchart/otherchart-0.1.0.tgz.prov" \
         "$OUT/testdata/badcharts/mybadchart/mybadchart-1.0.0.tgz" \
         "$OUT/testdata/badcharts/mybadchart/mybadchart-1.0.0.tgz.prov" \
         "$OUT/testdata/pgp/helm-test-key.secret" \
         "$OUT/testdata/pgp/helm-test-key.pub" \
         "$OUT/testdata/bearerauth/server.pem" \
         "$OUT/testdata/bearerauth/server.key" \
         "$OUT/testdata/clientauthcerts/ca.pem" \
         "$WEBOUT/template/index.html" \
         "$WEBOUT/template/static/main.css"; do
  test -f "$f" || { echo "FAIL: missing $f" >&2; exit 1; }
done
test -d "$WEBOUT/empty" || { echo "FAIL: missing $WEBOUT/empty" >&2; exit 1; }
if find "$WEBOUT/empty" -name '*.html' | grep -q .; then
    echo "FAIL: $WEBOUT/empty must contain no .html" >&2; exit 1
fi

# --- and publishing must not have changed a single byte ---------------------
# Checked against the published copies, not the frozen ones, so this catches a
# tar/touch step that damaged something rather than just re-checking the source.
# The manifest paths are testdata/... and webtemplate/..., which is why this runs
# from a directory where both resolve.
( cd "$OUT" && ln -sfn "$WEBOUT" webtemplate \
  && sha256sum -c --quiet "$FROZEN/MANIFEST.sha256" \
  && rm -f webtemplate ) || {
    echo "FAIL: publishing changed a fixture" >&2; exit 1; }

cp "$FROZEN/MANIFEST.sha256"    "$OUT/FIXTURES.sha256"
cp "$FROZEN/MANIFEST.aggregate" "$OUT/FIXTURES.aggregate"

# Read-only from here.  Every consumer -- the oracle, the submission under test,
# the agent's own experiments -- copies these into a scratch directory before
# serving them, because ChartMuseum writes into its storage directory.  A fixture
# tree that cannot be written to is how that stays true by construction rather
# than by everyone remembering.
chmod -R a-w "$OUT" "$WEBOUT"

echo "--- published digests ---"
cat "$OUT/FIXTURES.sha256"
echo "fixtures published: $(find "$OUT/testdata" "$WEBOUT" -type f | wc -l) files, aggregate ${have_agg:0:16}..."
