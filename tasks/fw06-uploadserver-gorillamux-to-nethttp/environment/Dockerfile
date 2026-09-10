# =============================================================================
# SWERefactorBench -- fw06-uploadserver-gorillamux-to-nethttp
# Agent environment image.
#
# Build-time responsibilities (network IS available during build; the trial
# itself runs with network_mode = "no-network"):
#
#   1. Verify + extract the frozen State A snapshot to /workspace/repo.
#   2. Publish the committed fixture docroot to /opt/fixtures/docroot and tie it
#      to the verifier's copy with an aggregate digest.
#   3. Build two offline Go module proxies from the pinned go.mod/go.sum:
#        - /opt/goproxy-baseline : the complete State A closure, gorilla/mux
#                                 included.  Used once, to build the read-only
#                                 oracle, then deleted from the image.
#        - /opt/goproxy          : the delivery mirror.  No .zip for the retired
#                                 module, and no replacement router either --
#                                 this task retires the whole category.  This is
#                                 also the verifier's mirror.
#   4. Build the frozen State A oracle at /opt/oracle (read-only), prove it
#      serves, then drop the baseline proxy and warm the agent's module cache.
#   5. Initialise a git repo with exactly ONE baseline commit, and prove State A
#      no longer builds against the delivery mirror -- the constraint is
#      discovered in the first minute, not at grading time.
#
# WHAT THE AGENT GETS, AND WHAT IT DELIBERATELY DOES NOT.  The oracle is the
# whole feedback loop: a pinned State A binary the agent can launch per flag set
# and interrogate with curl.  Any question about State A's behaviour has an
# answer the agent can measure for itself.
#
# What this image does NOT carry is the graded case list, or any tool that
# reports whether the graded suite would pass.  An earlier revision shipped
# both -- the verifier's corpus module and a `swerefactor-diff` that replayed it
# against the oracle and printed IDENTICAL -- and the combination was worse
# than the sum of its parts.  The case list gave away the suite's size and its
# per-suite partitioning; the `why` field on each case explained the trap in
# prose, including the one the audit gate exists to detect independent
# discovery of; and the differential tool turned the whole thing into a
# fill-in-the-blanks exercise, since a submission that merely reproduced 87
# recorded responses scored the behavioural stage in full.  The oracle answers
# the same questions without answering which questions get asked.
#
# WHAT MAKES THIS TASK DIFFERENT FROM fw04.  fw04 retires Gin and stocks chi as
# the replacement, so "Gin is gone" plus "behaviour matches" is the whole
# requirement.  Here the delivery mirror carries NO third-party router at all:
# the only routing available is net/http.ServeMux from the standard library.
#
# The base image is pinned by digest so it can never drift.
# =============================================================================

# golang:1.25 -- go1.25.12, the same digest every stage of this task pins.
FROM golang@sha256:9006890ecba0a168034d99516084099ae3114d9f2b7d6572c77f2dde57ebc980

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=UTC \
    SOURCE_DATE_EPOCH=1705276800 \
    GOTOOLCHAIN=local \
    GOFLAGS=-mod=mod \
    GOPATH=/opt/go \
    GOMODCACHE=/opt/go/pkg/mod \
    GOCACHE=/opt/gocache \
    GOSUMDB=off \
    PATH=/usr/local/go/bin:/opt/go/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# --- OS packages -------------------------------------------------------------
# curl: so the agent can drive its own server and the oracle over HTTP, and so
#       the readiness probe can distinguish a 204 from a 404.
# git:  required for the baseline snapshot commit.
# xxd/file: the fixture docroot deliberately contains binary and extensionless
#       files, and an agent debugging a Content-Type mismatch needs to see bytes.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git jq procps less vim-tiny file xxd \
        python3 tree \
    && rm -rf /var/lib/apt/lists/*

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

# =============================================================================
# 1. Unpack the frozen State A snapshot.
#
# Content-addressed: the build refuses to continue if the snapshot does not
# match its recorded digest, so every trial in every run starts from
# bit-identical sources.
# =============================================================================
COPY original.tar.gz /opt/snapshot/original.tar.gz
COPY original.sha256 /opt/snapshot/original.sha256

RUN cd /opt/snapshot \
 && sha256sum -c original.sha256 \
 && mkdir -p /workspace \
 && tar xzf original.tar.gz -C /workspace \
 && test -f /workspace/repo/go.mod \
 && test -f /workspace/repo/go.sum \
 && test -f /workspace/repo/app.go \
 && test -f /workspace/repo/pkg/server.go \
 && test -f /workspace/repo/pkg/file_naming.go \
 && test -f /workspace/repo/pkg/server_test.go \
 && test -f /workspace/repo/pkg/server_it_test.go \
 && test -f /workspace/repo/app_test.go \
 && test ! -e /workspace/repo/vendor \
 && ! find /workspace/repo -name '.git' | grep -q . \
 && grep -q 'github.com/gorilla/mux' /workspace/repo/go.mod \
 && grep -q 'github.com/gorilla/mux' /workspace/repo/pkg/server.go

COPY retired-modules.txt /opt/srb/retired-modules.txt
COPY banned-routers.txt  /opt/srb/banned-routers.txt
ENV SRB_RETIRED_MODULES=/opt/srb/retired-modules.txt \
    SRB_BANNED_ROUTERS=/opt/srb/banned-routers.txt \
    SRB_GOPROXY_ROOT=/opt/goproxy

# =============================================================================
# 2. The frozen fixture docroot.
#
# Nine files chosen so that one GET can distinguish extension-driven
# Content-Type from sniffed, a nested path from a flat one, percent-decoding
# from raw bytes, and a binary body from a text one.  Both images COPY
# byte-identical committed trees and both assert this same aggregate, so a drift
# between them fails a build instead of silently moving an expectation.
#
# The digest is computed over NUL-delimited "sha256  path" lines under LC_ALL=C
# so that `sp ace.txt` cannot split a field -- computing it with a bare
# `xargs sha256sum` yields a stable but WRONG value in both images, which is the
# failure mode that never announces itself.
#
# big.bin's contents are byte i = i % 256, and that is load-bearing.  Go resolves
# Content-Type from the FIRST mime database it finds: /usr/share/mime/globs2 if
# shared-mime-info is installed, otherwise /etc/mime.types.  globs2 has no entry
# for `.bin`, so on such a host the type falls through to sniffing, while
# /etc/mime.types maps bin -> application/octet-stream.  A text-filled big.bin
# therefore served as text/plain in one place and octet-stream in another -- and
# because the verifier is a SEPARATE image from this one, that discrepancy would
# have failed an honest agent on every range case, whose part headers embed the
# type.  Genuinely binary content makes sniffing and the table agree, so the
# answer no longer depends on an apt package.  Do not refill this file with
# printable bytes.
# =============================================================================
ENV SRB_FIXTURE_ROOT=/opt/fixtures/docroot \
    SRB_FIXTURE_AGGREGATE=bfa364db081206cba49def81caf967e5b80785bcdcfca4c4565017c5d6b51c2f \
    SRB_FIXTURE_MTIME="2026-01-01 00:00:00 UTC"

COPY fixtures/docroot /opt/fixtures/docroot

RUN set -eu; \
    cd /opt/fixtures/docroot; \
    got=$(find . -type f -print0 | LC_ALL=C sort -z \
            | xargs -0 sha256sum | sha256sum | cut -d' ' -f1); \
    n=$(find . -type f | wc -l); \
    echo "fixture docroot: $n files, aggregate $got"; \
    [ "$n" = "9" ] || { echo "FAIL: expected 9 fixture files, found $n" >&2; exit 1; }; \
    [ "$got" = "$SRB_FIXTURE_AGGREGATE" ] || { \
        echo "FAIL: fixture aggregate drifted" >&2; \
        echo "  expected $SRB_FIXTURE_AGGREGATE" >&2; \
        echo "  actual   $got" >&2; exit 1; }; \
    test -f 'sp ace.txt'; \
    test -f 'odd+name.txt'; \
    test -f README; \
    test -f sub/b.txt; \
    test "$(stat -c%s big.bin)" = "4096"; \
    echo "fixture docroot verified"

# =============================================================================
# 3. The two offline module proxies.
# =============================================================================
COPY scripts/build-goproxy.sh /opt/srb/scripts/build-goproxy.sh
RUN chmod +x /opt/srb/scripts/build-goproxy.sh \
 && /opt/srb/scripts/build-goproxy.sh /workspace/repo

# =============================================================================
# 4. The read-only State A oracle.
#
# Built from the baseline proxy (gorilla/mux present) and from a pristine copy of
# the snapshot, never from /workspace/repo -- the agent will edit that, and the
# oracle must stay State A for the whole trial.
#
# GOMODCACHE is overridden to a throwaway path for exactly the reason the warming
# step uses one: `go build` will happily use an already-extracted mux@v1.8.1 from
# the module cache without ever consulting a proxy, so leaving the oracle's
# dependencies in the agent's GOMODCACHE would hand the agent a working
# gorilla/mux and void the retirement.
# =============================================================================
RUN set -eux; \
    mkdir -p /opt/oracle-src; \
    tar xzf /opt/snapshot/original.tar.gz -C /opt/oracle-src --strip-components=1; \
    cd /opt/oracle-src; \
    GOPROXY=file:///opt/goproxy-baseline CGO_ENABLED=0 \
    GOMODCACHE=/tmp/oracle-modcache \
      go build -v -o /opt/oracle/simple-upload-server .; \
    /opt/oracle/simple-upload-server -h 2>&1 | head -3 || true; \
    chmod -R a-w /opt/oracle; \
    rm -rf /tmp/oracle-modcache /opt/oracle-src

# The oracle must carry the retired module in its build info -- it is the State A
# reference, and the verifier's closure checks read exactly this metadata from
# the SUBMISSION to prove the module is gone there.  Asserting it here proves
# those checks read a field that is actually populated, so "absent" means absent
# rather than never recorded in the first place.
RUN set -eu; \
    go version -m /opt/oracle/simple-upload-server \
      | grep -q 'github.com/gorilla/mux' \
      || { echo "FAIL: oracle build info does not mention gorilla/mux" >&2; exit 1; }; \
    echo "oracle build info records gorilla/mux (the closure check has a populated field to test)"

COPY scripts/oracle-serve /opt/srb/scripts/oracle-serve
RUN chmod +x /opt/srb/scripts/oracle-serve \
 && ln -s /opt/srb/scripts/oracle-serve /usr/local/bin/oracle-serve

# Prove the oracle actually serves, that the fixture docroot is seeded, and that
# the mtime stamping works -- Last-Modified is a graded header, and git records no
# mtimes, so a docroot that inherited checkout times would make it unreproducible
# between this image and the verifier's.  The expected value is the constant that
# oracle-serve stamps, so this is an end-to-end check of the stamping, not just of
# the server.
RUN set -eux; \
    /opt/srb/scripts/oracle-serve start --port 8899; \
    curl -fsS http://127.0.0.1:8899/files/a.txt; echo; \
    curl -sS -D- -o /dev/null http://127.0.0.1:8899/files/a.txt \
      | grep -i '^last-modified:' ; \
    curl -sS -D- -o /dev/null http://127.0.0.1:8899/files/a.txt \
      | grep -qi '^last-modified: Thu, 01 Jan 2026 00:00:00 GMT' \
      || { echo "FAIL: fixture Last-Modified is not the stamped constant" >&2; \
           /opt/srb/scripts/oracle-serve stop; exit 1; }; \
    curl -sS -o /dev/null -w '%{http_code}\n' -X OPTIONS \
        http://127.0.0.1:8899/upload | grep -qx 204; \
    curl -fsS http://127.0.0.1:8899/files/sub/b.txt; echo; \
    /opt/srb/scripts/oracle-serve stop

# =============================================================================
# 5. Drop the baseline proxy.  From here on, gorilla/mux is unobtainable.
#
# The .mod file stays deliberately.  Retirement is by omission of the .zip: `go
# mod tidy` still needs to read the module's own go.mod to compute the build
# list, so deleting that too would break tidy even for a correct migration and
# the failure would look like the agent's fault.  What is unobtainable is the
# source, which is what an import needs.
# =============================================================================
RUN rm -rf /opt/goproxy-baseline \
 && test ! -e /opt/goproxy/github.com/gorilla/mux/@v/v1.8.1.zip \
 && test -f /opt/goproxy/github.com/gorilla/mux/@v/v1.8.1.mod

# Only now is it safe to write the cache the agent inherits: /opt/goproxy is the
# single remaining module source in the image, and it has no zip for any retired
# module or banned router, so nothing this step unpacks can be a router.
COPY scripts/warm-modcache.sh /opt/srb/scripts/warm-modcache.sh
RUN chmod +x /opt/srb/scripts/warm-modcache.sh \
 && /opt/srb/scripts/warm-modcache.sh

# The retirement is enforced by the mirror, but the mirror is only consulted when
# the module cache misses -- so the cache the agent inherits is asserted to hold
# no retired module and no banned router.  Had warming left an extracted
# gorilla/mux behind, a submission that never migrated would compile against it
# regardless of GOPROXY, and the task would have no constraint at all.  This is
# the same assertion the verification stage makes about its own image, for the
# same reason.
RUN set -eu; \
    for f in /opt/srb/retired-modules.txt /opt/srb/banned-routers.txt; do \
      for mod in $(sed -e 's/#.*//' -e '/^[[:space:]]*$/d' "$f" | tr -d '[:blank:]'); do \
        if [ -e "/opt/go/pkg/mod/$mod" ] || \
           [ -n "$(find "/opt/go/pkg/mod/cache/download/$mod" -name '*.zip' 2>/dev/null)" ]; then \
          echo "FAIL: $mod is present in the module cache the agent inherits." >&2; \
          echo "A build that does not override GOMODCACHE would compile against" >&2; \
          echo "it regardless of GOPROXY, voiding the retirement." >&2; \
          exit 1; \
        fi; \
      done; \
    done; \
    echo "the warmed module cache holds no retired module and no banned router"

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
    GIT_AUTHOR_DATE="2025-01-01T00:00:00Z" \
    GIT_COMMITTER_DATE="2025-01-01T00:00:00Z" \
      git commit -q -m "State A: go-simple-upload-server v2.2.0 (gorilla/mux 1.8.1)"; \
    test "$(git rev-list --count HEAD)" = "1"; \
    test -z "$(git remote)"; \
    test -z "$(git tag)"

# The constraint, demonstrated rather than described.  `go build ./...` must fail
# against the delivery mirror, and the failure must name the retired module -- an
# agent that starts by building learns the shape of the task immediately.
#
# A throwaway GOMODCACHE is used so this probe cannot populate the agent's cache,
# and the repo is copied so a failed `go mod tidy` inside it cannot rewrite the
# committed go.mod before the agent ever sees it.
RUN set -eu; \
    rm -rf /tmp/nobuild; mkdir -p /tmp/nobuild; \
    cp -a /workspace/repo/. /tmp/nobuild/; \
    rm -rf /tmp/nobuild/.git; \
    cd /tmp/nobuild; \
    if GOPROXY=file:///opt/goproxy GOMODCACHE=/tmp/nobuild-cache \
         go build ./... > /tmp/nobuild.log 2>&1; then \
      echo "FAIL: State A still builds against the delivery mirror -- the" >&2; \
      echo "      retirement is not in effect and the task has no constraint." >&2; \
      exit 1; \
    fi; \
    grep -qi 'gorilla/mux' /tmp/nobuild.log \
      || { echo "FAIL: build failed but not because of the retired module:" >&2; \
           cat /tmp/nobuild.log >&2; exit 1; }; \
    echo "confirmed: State A does not build offline, and the error names gorilla/mux"; \
    head -5 /tmp/nobuild.log; \
    rm -rf /tmp/nobuild /tmp/nobuild-cache /tmp/nobuild.log

# The one path instruction.md names, set here as well as in task.toml's
# [environment.env] so it is true of the image itself and not only of a
# Harbor-launched container.  SRB_ORACLE is the binary, not its directory:
# /opt/oracle is read-only and holds nothing else, and a variable that needs a
# filename appended is one an agent gets wrong once.
ENV SRB_ORACLE=/opt/oracle/simple-upload-server

WORKDIR /workspace/repo

# =============================================================================
# 7. Verify the finished environment, then delete the verifier.
#
# Last layer deliberately.  Everything above it is a check on one step; this is
# the check on the result -- the trial tree still matches the snapshot, the oracle
# still serves, the module cache carries no router, and none of the artefacts this
# task deliberately does not ship have come back.
#
# It runs at image-build time and is removed in the same layer, so the agent never
# receives it and it reports nothing to the agent either way.
# =============================================================================
COPY verify_environment.sh /tmp/verify_environment.sh
RUN chmod +x /tmp/verify_environment.sh \
 && /tmp/verify_environment.sh \
 && rm -f /tmp/verify_environment.sh
