# =============================================================================
# SWERefactorBench -- fw04-chartmuseum-gin-to-chi
# Agent environment image.
#
# Build-time responsibilities (network IS available during build; the trial
# itself runs with network_mode = "no-network"):
#
#   1. Verify + extract the frozen State A snapshot to /workspace/repo.
#   2. Publish the committed fixture tree to /opt/testdata and /opt/webtemplate,
#      and prove it is the snapshot's own testdata plus exactly the 10 packaged
#      chart artifacts.  The fixtures are NOT built here: `helm package` stamps
#      time.Now() into every tar header, so two builds of the same chart produce
#      different bytes and index.yaml's digests would disagree between this image
#      and the verifier's.  They are frozen once at construction time and
#      committed; see scripts/install-testdata.sh.  helm 3.8.0 is still installed,
#      because the repo's own test scripts fetch it and the trial has no network.
#   3. Build two offline Go module proxies from the pinned go.mod/go.sum:
#        - /opt/goproxy-baseline : the complete State A closure, Gin included.
#                                 Used once, to build the read-only oracle,
#                                 then deleted from the image.
#        - /opt/goproxy          : the delivery mirror.  Everything a Gin-free
#                                 port may legitimately need, and NO .zip for
#                                 any of the three retired modules.  This is
#                                 also the verifier's mirror.
#   4. Build the frozen State A oracle binary at /opt/oracle (read-only) and
#      prove it serves.
#   5. Initialise a git repo containing exactly ONE baseline commit.
#   6. Prove State A no longer builds against the delivery mirror -- the
#      constraint is discovered in the first minute, not at grading time.
#
# /workspace/repo therefore ships in a state where `go build ./...` FAILS, and
# the failure names github.com/gin-gonic/gin, github.com/gin-contrib/size and
# github.com/zsais/go-gin-prometheus along with the source lines importing them.
#
# The base image is pinned by digest so it can never drift.
# =============================================================================

# golang:1.24 -- go1.24.13
FROM golang@sha256:d2d2bc1c84f7e60d7d2438a3836ae7d0c847f4888464e7ec9ba3a1339a1ee804

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=UTC \
    GOTOOLCHAIN=local \
    GOFLAGS=-mod=mod \
    GOPATH=/opt/go \
    GOMODCACHE=/opt/go/pkg/mod \
    GOCACHE=/opt/gocache \
    GOSUMDB=off \
    PATH=/usr/local/go/bin:/opt/go/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# --- OS packages -------------------------------------------------------------
# curl: so the agent can drive its own server over HTTP.
# gnupg: helm package --sign needs a gpg keyring.
# git: required for the baseline snapshot commit (and the Makefile's REVISION).
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git gnupg jq procps less vim-tiny file xxd \
        python3 tree \
    && rm -rf /var/lib/apt/lists/*

# =============================================================================
# 1. Unpack the frozen State A snapshot.
#
# Content-addressed: the build refuses to continue if the snapshot does not
# match its recorded digest, so every trial in every run starts from
# bit-identical sources.
# =============================================================================
# The golang image puts the toolchain on PATH with ENV only, and /etc/profile
# overwrites PATH outright -- so `bash -lc 'go build'` reports "go: command not
# found" in an image that has Go installed.  Agents do use login shells.  Also
# corrects /go/bin, which is the base image's GOPATH/bin and not the one set
# above.
RUN printf 'export PATH="%s"\n' \
      '/usr/local/go/bin:/opt/go/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin' \
      > /etc/profile.d/10-golang.sh \
 && chmod 0644 /etc/profile.d/10-golang.sh \
 && bash -lc 'command -v go && go version'

COPY original.tar.gz /opt/snapshot/original.tar.gz
COPY original.sha256 /opt/snapshot/original.sha256

RUN cd /opt/snapshot \
 && sha256sum -c original.sha256 \
 && mkdir -p /workspace \
 && tar xzf original.tar.gz -C /workspace \
 && test -f /workspace/repo/go.mod \
 && test -f /workspace/repo/Makefile \
 && test -f /workspace/repo/pkg/chartmuseum/router/router.go \
 && test -f /workspace/repo/pkg/chartmuseum/router/match.go \
 && test -f /workspace/repo/pkg/chartmuseum/server/multitenant/routes.go \
 && test -f /workspace/repo/cmd/chartmuseum/main.go \
 && test -f /workspace/repo/.github/workflows/build.yml \
 && test ! -e /workspace/repo/vendor \
 && ! find /workspace/repo -name '.git' | grep -q . \
 && grep -q 'github.com/gin-gonic/gin' /workspace/repo/go.mod

COPY retired-modules.txt /opt/srb/retired-modules.txt
ENV SRB_RETIRED_MODULES=/opt/srb/retired-modules.txt \
    SRB_GOPROXY_ROOT=/opt/goproxy

# =============================================================================
# 2. helm 3.8.0, pinned by digest, and the one-and-only chart packaging pass.
# =============================================================================
ENV HELM_BIN=/opt/helm/helm
RUN set -eux; \
    mkdir -p /opt/helm; \
    cd /tmp; \
    curl -fsSLO https://get.helm.sh/helm-v3.8.0-linux-amd64.tar.gz; \
    echo "8408c91e846c5b9ba15eb6b1a5a79fc22dd4d33ac6ea63388e5698d1b2320c8b  helm-v3.8.0-linux-amd64.tar.gz" | sha256sum -c -; \
    tar xzf helm-v3.8.0-linux-amd64.tar.gz; \
    mv linux-amd64/helm /opt/helm/helm; \
    chmod +x /opt/helm/helm; \
    rm -rf /tmp/linux-amd64 /tmp/helm-v3.8.0-linux-amd64.tar.gz; \
    /opt/helm/helm version --short; \
    ln -s /opt/helm/helm /usr/local/bin/helm
# On PATH as well as in $HELM_BIN: scripts/setup-test-environment.sh downloads
# helm 3.8.0 itself and the trial has no network, so this pre-installed copy is
# what makes the repo's own test setup runnable offline.

# The frozen fixture tree, and the aggregate digest that ties this image's copy
# to the verifier's.  Both images COPY byte-identical committed trees and both
# assert this same value; if the two copies ever drift, one of the builds fails
# here rather than at grading time.  See install-testdata.sh for why the fixtures
# are frozen at construction rather than built here -- `helm package` stamps
# time.Now() into every tar header, so building them twice yields different bytes
# and index.yaml's digests would disagree between the oracle and the verifier.
ENV SRB_TESTDATA_AGGREGATE=6d888ec7db7d0a6944d7507ffd977f775457f09976859db33ca28c86cc496593
COPY testdata-frozen /opt/srb/testdata-frozen
COPY scripts/install-testdata.sh /opt/srb/scripts/install-testdata.sh
RUN chmod +x /opt/srb/scripts/install-testdata.sh \
 && /opt/srb/scripts/install-testdata.sh /opt/srb/testdata-frozen \
        /opt/testdata /opt/webtemplate

# The frozen tree must be the snapshot's own testdata plus exactly the packaged
# artifacts -- nothing edited, nothing missing.  Only this image can check it:
# it is the one that has both the snapshot and the frozen copy.  Without it, a
# fixture could be quietly patched at construction time and every recorded
# expectation would move with it, consistently, in both images.
RUN set -eu; \
    cd /workspace/repo; \
    drift=0; \
    for f in $(find testdata -type f | LC_ALL=C sort); do \
      if [ ! -f "/opt/srb/testdata-frozen/$f" ]; then \
        echo "MISSING from frozen tree: $f"; drift=1; continue; \
      fi; \
      a=$(sha256sum "$f" | cut -d' ' -f1); \
      b=$(sha256sum "/opt/srb/testdata-frozen/$f" | cut -d' ' -f1); \
      [ "$a" = "$b" ] || { echo "DIFFERS from snapshot: $f"; drift=1; }; \
    done; \
    find testdata -type f \! -name '*.tgz' \! -name '*.tgz.prov' \
        | LC_ALL=C sort > /tmp/snap.list; \
    ( cd /opt/srb/testdata-frozen && find testdata -type f \
        \! -name '*.tgz' \! -name '*.tgz.prov' | LC_ALL=C sort ) > /tmp/frozen.list; \
    if ! cmp -s /tmp/snap.list /tmp/frozen.list; then \
        echo "frozen tree and snapshot disagree on which unpackaged files exist:"; \
        diff /tmp/snap.list /tmp/frozen.list || true; drift=1; \
    fi; \
    rm -f /tmp/snap.list /tmp/frozen.list; \
    [ "$drift" = 0 ] || { echo "FAIL: frozen fixtures diverge from the snapshot" >&2; exit 1; }; \
    echo "frozen tree agrees with the snapshot on every non-packaged file"

# The frozen webtemplate tree must also be the snapshot's own.  It is published
# by install-testdata.sh above, for the same reason the charts are: the agent may
# legitimately move or edit pkg/chartmuseum/server/multitenant/testdata, and
# --web-template-path renders index.html and static/main.css into response bytes
# the suite compares, so the oracle and the submission are both pointed at the
# frozen copy rather than at anything in the repository.
RUN set -eu; \
    src=/workspace/repo/pkg/chartmuseum/server/multitenant/testdata/template; \
    for f in index.html static/main.css; do \
      a=$(sha256sum "$src/$f" | cut -d' ' -f1); \
      b=$(sha256sum "/opt/webtemplate/template/$f" | cut -d' ' -f1); \
      [ "$a" = "$b" ] || { \
          echo "FAIL: frozen webtemplate/$f differs from the snapshot" >&2; \
          exit 1; }; \
    done; \
    echo "frozen webtemplate agrees with the snapshot"

# =============================================================================
# 3. The two offline module proxies.
# =============================================================================
COPY scripts/build-goproxy.sh /opt/srb/scripts/build-goproxy.sh
RUN chmod +x /opt/srb/scripts/build-goproxy.sh \
 && /opt/srb/scripts/build-goproxy.sh /workspace/repo

# =============================================================================
# 4. The read-only State A oracle.
#
# Built from the baseline proxy (Gin present), from a pristine copy of the
# snapshot, with Version/Revision pinned so `--version` is reproducible -- the
# Makefile derives REVISION from `git rev-parse --short HEAD`, which would
# otherwise vary with the baseline commit's hash.
# =============================================================================
# GOMODCACHE is overridden to a throwaway path for exactly the reason the
# warming step uses one: `go build` will happily use an already-extracted
# gin@v1.8.1 from the module cache without ever consulting a proxy, so leaving
# the oracle's dependencies in the agent's GOMODCACHE would hand the agent a
# working Gin and void the ban.
RUN set -eux; \
    mkdir -p /opt/oracle-src; \
    tar xzf /opt/snapshot/original.tar.gz -C /opt/oracle-src --strip-components=1; \
    cd /opt/oracle-src; \
    GOPROXY=file:///opt/goproxy-baseline CGO_ENABLED=0 \
    GOMODCACHE=/tmp/oracle-modcache \
      go build -v -o /opt/oracle/chartmuseum \
        --ldflags="-w -X main.Version=0.15.0 -X main.Revision=460d8ec9" \
        ./cmd/chartmuseum; \
    /opt/oracle/chartmuseum --version; \
    chmod -R a-w /opt/oracle; \
    rm -rf /tmp/oracle-modcache /opt/oracle-src

COPY scripts/oracle-serve /opt/srb/scripts/oracle-serve
RUN chmod +x /opt/srb/scripts/oracle-serve \
 && ln -s /opt/srb/scripts/oracle-serve /usr/local/bin/oracle-serve

# prove the oracle actually serves before anyone depends on it
RUN set -eux; \
    /opt/srb/scripts/oracle-serve start --port 8899 --depth 0; \
    curl -fsS http://127.0.0.1:8899/health; echo; \
    curl -fsS http://127.0.0.1:8899/index.yaml | head -5; \
    /opt/srb/scripts/oracle-serve stop

# =============================================================================
# 5. Drop the baseline proxy.  From here on, Gin is unobtainable in this image.
# =============================================================================
RUN rm -rf /opt/goproxy-baseline \
 && test ! -e /opt/goproxy/github.com/gin-gonic/gin/@v/v1.8.1.zip \
 && test -f /opt/goproxy/github.com/gin-gonic/gin/@v/v1.8.1.mod

# Only now is it safe to write the cache the agent inherits: /opt/goproxy is the
# single remaining module source in the image, and it has no zip for any retired
# module, so nothing this step unpacks can be Gin.
COPY scripts/warm-modcache.sh /opt/srb/scripts/warm-modcache.sh
RUN chmod +x /opt/srb/scripts/warm-modcache.sh \
 && /opt/srb/scripts/warm-modcache.sh

# =============================================================================
# 6. One baseline commit, and proof that State A no longer builds.
#
# The agent gets `git diff` / `git stash` / `git checkout -- .` against State A
# but no history to mine: one commit, no remotes, no tags.
# =============================================================================
ENV GOPROXY=file:///opt/goproxy
RUN set -eux; \
    cd /workspace/repo; \
    git init -q -b main; \
    git config user.email "bench@swerefactor.local"; \
    git config user.name  "SWERefactorBench"; \
    git config commit.gpgsign false; \
    git add -A; \
    GIT_AUTHOR_DATE="2022-07-01T00:00:00Z" \
    GIT_COMMITTER_DATE="2022-07-01T00:00:00Z" \
      git commit -q -m "State A: chartmuseum v0.15.0 (Gin 1.8.1)"; \
    test "$(git rev-list --count HEAD)" = "1"; \
    test -z "$(git remote)"; \
    test -z "$(git tag)"

# There is deliberately no self-check command in this image.  An earlier revision
# installed one that reported on the agent's progress against the grading
# criteria, and it was the wrong tool in two directions: what it reported was a
# subset of stage 1's questions, so a passing self-check invited the agent to stop
# working, and what it could not report was everything the gates actually turn on.
# The agent has the oracle and `go build`; those measure the work rather than the
# grader's opinion of it.

WORKDIR /workspace/repo
