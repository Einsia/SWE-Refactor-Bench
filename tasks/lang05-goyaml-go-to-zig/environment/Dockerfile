# syntax=docker/dockerfile:1
#
# Agent environment for lang05-goyaml-go-to-zig.
#
# Hands the solver State A -- gopkg.in/yaml.v3 v3.0.1, in Go, buildable and
# testable -- plus the Zig toolchain the port targets.  Everything is pinned by
# digest or exact version: a task whose environment drifts is a task whose
# scores cannot be compared across months.
#
# The repository is placed at /workspace/repo with a single-commit git baseline
# and no upstream history.  The port is the work, and a solver who can read the
# next commit is not doing it.

FROM debian@sha256:7b140f374b289a7c2befc338f42ebe6441b7ea838a042bbd5acbfca6ec875818 AS base

ARG DEBIAN_SNAPSHOT=20251020T000000Z
ARG GO_VERSION=1.23.4
ARG GO_SHA256=6924efde5de86fe277676e929dc9917d466efa02fb934197bc2eba35d5680971
ARG ZIG_VERSION=0.14.1
ARG ZIG_SHA256=24aeeec8af16c381934a6cd7d95c807a8cb2cf7df9fa40d359aa884195c4716c

SHELL ["/bin/bash", "-euo", "pipefail", "-c"]
ENV DEBIAN_FRONTEND=noninteractive \
    TZ=UTC \
    LC_ALL=C.UTF-8 \
    LANG=C.UTF-8

# Point apt at a frozen snapshot so a rebuild six months from now resolves the
# same bytes.  Check-Valid-Until has to be off: a snapshot's Release file is, by
# construction, past its expiry.
RUN rm -f /etc/apt/sources.list.d/*; \
    printf '%s\n' \
      "deb http://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT} bookworm main" \
      "deb http://snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT} bookworm-security main" \
      "deb http://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT} bookworm-updates main" \
      > /etc/apt/sources.list; \
    printf 'Acquire::Check-Valid-Until "false";\n' \
      > /etc/apt/apt.conf.d/99no-check-valid-until; \
    printf 'Acquire::Retries "5";\n' > /etc/apt/apt.conf.d/99retries

RUN apt-get -qq update \
 && apt-get -qq install -y --no-install-recommends \
      ca-certificates=20230311+deb12u1 \
      curl=7.88.1-10+deb12u14 \
      xz-utils=5.4.1-1 \
      git=1:2.39.5-0+deb12u2 \
      make=4.3-4.1 \
      python3=3.11.2-1+b1 \
      file=1:5.44-3 \
 && rm -rf /var/lib/apt/lists/*

# Zig from the official tarball.  There is no Debian package for it at any
# version, and there would be no reason to prefer one if there were: the task
# names 0.14.1 exactly, and 0.15 rewrote the standard library's I/O interfaces
# in a way that breaks most code written against 0.14.
RUN cd /tmp \
 && curl -fsSLO "https://ziglang.org/download/${ZIG_VERSION}/zig-x86_64-linux-${ZIG_VERSION}.tar.xz" \
 && echo "${ZIG_SHA256}  zig-x86_64-linux-${ZIG_VERSION}.tar.xz" | sha256sum -c - \
 && install -d /opt/zig \
 && tar -xJf "zig-x86_64-linux-${ZIG_VERSION}.tar.xz" -C /opt/zig --strip-components=1 \
 && rm -f "zig-x86_64-linux-${ZIG_VERSION}.tar.xz" \
 && ln -s /opt/zig/zig /usr/local/bin/zig \
 && zig version

# Go, for building and studying State A.  It is deliberately present: the
# solver is expected to run the upstream test suite.  It will not be present
# when the port is graded.
RUN cd /tmp \
 && curl -fsSLO "https://go.dev/dl/go${GO_VERSION}.linux-amd64.tar.gz" \
 && echo "${GO_SHA256}  go${GO_VERSION}.linux-amd64.tar.gz" | sha256sum -c - \
 && tar -xzf "go${GO_VERSION}.linux-amd64.tar.gz" -C /usr/local \
 && rm -f "go${GO_VERSION}.linux-amd64.tar.gz" \
 && ln -s /usr/local/go/bin/go /usr/local/bin/go \
 && ln -s /usr/local/go/bin/gofmt /usr/local/bin/gofmt \
 && go version

# Assert the toolchain is what the contract names.  grep -Fx so a version that
# merely contains the string does not pass.
RUN zig version | grep -Fx "${ZIG_VERSION}" \
 && go version | cut -d' ' -f3 | grep -Fx "go${GO_VERSION}" \
 && python3 --version | cut -d' ' -f2 | grep -Fx '3.11.2'

# Non-root solver, fixed uid/gid so a collected artifact tree has stable
# ownership regardless of the host.
RUN groupadd --gid 2000 agent \
 && useradd --uid 2000 --gid 2000 --create-home --shell /bin/bash agent

# ---------------------------------------------------------------------------
# State A
# ---------------------------------------------------------------------------
FROM base AS state-a

ARG REPO_SHA256=4a9603dd8ad249177cd87b1f01ba6fe1794ddec017766ed371bba1df03b46ab2
ARG REPO_SIZE=99730
ARG REPO_FILES=33

# /opt/state-a, not /opt/swerefactor.  `/opt/swerefactor` is the shared grader and
# `/opt/assets` a stage's own answers; both names belong to the three verifier
# images and to no agent image.  What State A leaves behind for the agent goes
# here instead -- a grader-named directory in front of the agent is at best
# confusing and at worst a hint about how it is being marked.
COPY original.tar.gz /opt/state-a/original.tar.gz
COPY source-contract.json /opt/state-a/source-contract.json
COPY verify_environment.py /opt/state-a/verify_environment.py

# Verify the archive before trusting it.  Size as well as digest: a truncated
# download that happens to be a valid gzip is a real failure mode.
RUN echo "${REPO_SHA256}  /opt/state-a/original.tar.gz" | sha256sum -c - \
 && test "$(stat -c '%s' /opt/state-a/original.tar.gz)" = "${REPO_SIZE}"

RUN install -d -m 0755 /workspace \
 && tar -xzf /opt/state-a/original.tar.gz -C /workspace \
      --no-same-owner --no-same-permissions \
 && test -d /workspace/repo \
 && test "$(find /workspace/repo -type f | wc -l)" = "${REPO_FILES}" \
 && test -z "$(find /workspace/repo \( -type l -o -type b -o -type c -o -type p -o -type s \) -print -quit)"

# No repository-management data reached the solver.  This is the check that
# keeps the upstream history out of reach.
RUN for name in .git .github .gitlab .gitignore .gitattributes .gitmodules \
                .mailmap .hg .svn .bzr _darcs CVS .agit .travis.yml; do \
      if find /workspace/repo -name "$name" -print -quit | grep -q .; then \
        echo "repository-management data in State A: $name" >&2; exit 1; \
      fi; \
    done \
 && test -z "$(find /workspace/repo -name '*.pyc' -o -name '*.orig' -o -name '*.rej' -print -quit)"

RUN chmod 0444 /opt/state-a/source-contract.json

# ---------------------------------------------------------------------------
# Zig's caches, outside the repository
# ---------------------------------------------------------------------------
# Zig writes .zig-cache into the working directory by default.  Left alone, that
# puts build artefacts inside the tree Harbor collects as the submission, where
# they would trip the forbidden-paths check on .o files the solver never wrote.
# Both variables are set for every shell, including login shells.
#
# GOFLAGS is deliberately unset, so Go stays in its default -mod=readonly.  With
# the shipped go.sum that is enough to build and test, and it means any go command
# that would rewrite go.mod or go.sum fails loudly instead of quietly editing the
# tree the solver is about to submit.
ENV ZIG_GLOBAL_CACHE_DIR=/tmp/zig-cache-global \
    ZIG_LOCAL_CACHE_DIR=/tmp/zig-cache-local \
    GOPATH=/opt/gopath \
    GOMODCACHE=/opt/gopath/pkg/mod \
    GOCACHE=/tmp/go-build \
    CGO_ENABLED=0

RUN printf '%s\n' \
      'export PATH=/usr/local/go/bin:/opt/zig:$PATH' \
      'export ZIG_GLOBAL_CACHE_DIR=/tmp/zig-cache-global' \
      'export ZIG_LOCAL_CACHE_DIR=/tmp/zig-cache-local' \
      'export GOPATH=/opt/gopath' \
      'export GOMODCACHE=/opt/gopath/pkg/mod' \
      'export GOCACHE=/tmp/go-build' \
      > /etc/profile.d/10-swerefactor.sh \
 && chmod 0644 /etc/profile.d/10-swerefactor.sh \
 && install -d -m 0777 /tmp/zig-cache-global /tmp/zig-cache-local /tmp/go-build \
 && install -d -m 0755 /opt/gopath \
 && bash -lc 'zig version && go version'

# State A's test suite needs gopkg.in/check.v1, and the solver has no network.
# Fetched here into a world-readable module cache outside /workspace, so
# `go test ./...` works for them offline.  Not vendored: a vendor/ directory is on
# the forbidden-paths list and the file count would reject it.
#
# Downloaded from a throwaway copy, never from /workspace/repo.  `go mod download`
# writes go.sum into the module's directory, and /workspace/repo is the tree the
# contract digests and the solver submits -- a file the toolchain put there would
# be indistinguishable, at grading time, from one the solver left behind.  State A
# ships its own correct go.sum, so nothing here needs to generate one.
RUN cp -a /workspace/repo /tmp/prime \
 && cd /tmp/prime \
 && GOFLAGS=-mod=mod go mod download all \
 && chmod -R a+rX /opt/gopath \
 && test -d /opt/gopath/pkg/mod/gopkg.in \
 && cmp /tmp/prime/go.sum /workspace/repo/go.sum \
 && rm -rf /tmp/prime

# ---------------------------------------------------------------------------
# Prove State A works, in this image, before the image is allowed to exist
# ---------------------------------------------------------------------------
# No `go vet` here.  It always compiles the external test package, and go-yaml's
# own tests use bare struct tags (`a,omitempty` rather than `yaml:"a,omitempty"`)
# which vet rejects on sight.  That is upstream's idiom, the library parses those
# tags itself, and vetting it would fail the build over a style the task is not
# measuring.
#
# GOPROXY=off, so a cache that was not primed correctly fails here rather than for
# a solver with no network.  The trailing digest comparison is the point of the
# step: it proves running the tests leaves /workspace/repo byte-identical, which is
# what makes it safe to invite the solver to run them.
RUN cd /workspace/repo \
 && before="$(find . -type f | sort | xargs sha256sum | sha256sum)" \
 && GOPROXY=off go build ./... \
 && GOPROXY=off go test ./... 2>&1 | tee /tmp/statea-tests.txt \
 && grep -qE '^ok\s' /tmp/statea-tests.txt \
 && ! grep -qE '^(FAIL|--- FAIL)' /tmp/statea-tests.txt \
 && test "$(find . -type f | wc -l)" = "${REPO_FILES}" \
 && test "$(find . -type f | sort | xargs sha256sum | sha256sum)" = "$before"

# The stub must build and must exit 70, and it must do so without a network and
# with the cache outside the tree.  Both properties are load-bearing: the build
# gate has to distinguish "did not implement the protocol" from "broke the
# build", and a cache inside the tree would contaminate the submission.
RUN cd /workspace/repo \
 && before="$(find . -type f | sort | xargs sha256sum | sha256sum)" \
 && zig build \
 && test -x zig-out/bin/yaml-probe \
 && if printf '{"id":1,"op":"hello","source":""}\n' | ./zig-out/bin/yaml-probe; \
    then echo 'stub probe exited 0; the build gate cannot tell an unimplemented' \
              'submission from a working one' >&2; exit 1; \
    else rc=$?; test "$rc" = "70"; fi \
 && zig build test \
 && rm -rf zig-out .zig-cache zig-cache \
 && test "$(find . -type f | wc -l)" = "${REPO_FILES}" \
 && test "$(find . -type f | sort | xargs sha256sum | sha256sum)" = "$before"

# ---------------------------------------------------------------------------
# Baseline commit
# ---------------------------------------------------------------------------
# One commit, fixed identity, fixed dates.  Deterministic so the image digest
# does not change on rebuild, and single so there is no history to mine.
RUN cd /workspace/repo \
 && git init -q -b main \
 && git config user.name  'SWERefactorBench' \
 && git config user.email 'bench@swerefactor.invalid' \
 && git config commit.gpgsign false \
 && git config core.autocrlf false \
 && git add -A \
 && GIT_AUTHOR_DATE='2000-01-01T00:00:00+0000' \
    GIT_COMMITTER_DATE='2000-01-01T00:00:00+0000' \
    git commit -q -m 'gopkg.in/yaml.v3 v3.0.1 baseline' \
 && test "$(git rev-list --count HEAD)" = "1" \
 && test -z "$(git status --porcelain)" \
 && git rev-parse HEAD > /opt/state-a/baseline-commit.txt \
 && git log --format='%H %ad %s' --date=iso >> /opt/state-a/baseline-commit.txt

RUN python3 /opt/state-a/verify_environment.py \
      --repo /workspace/repo \
      --contract /opt/state-a/source-contract.json

# Every self-test above ran as root, and root's writes into the shared caches left
# their subdirectories (h/ o/ tmp/ z/ b/) owned by root at mode 0755.  The 0777 on
# the top directory does not help: zig creates its manifests and lock files
# *inside* those, so `zig build` as the agent uid died with a bare
# `error: AccessDenied` naming no path -- on the unmodified State A tree, before
# the solver had written a line.  Go only degrades under the same treatment (an
# uncachable entry is simply not cached), which is why the Zig side was the one
# that broke.
#
# Recreated rather than chown -Red: chown -R would copy ~175MB of cache into a new
# layer, and the entries are worth nothing here anyway -- the self-test below
# rewarms them as the uid that will actually read them.
RUN chown -R agent:agent /workspace \
 && chmod 0444 /opt/state-a/baseline-commit.txt \
 && rm -rf /tmp/zig-cache-global /tmp/zig-cache-local /tmp/go-build \
 && install -d -m 0777 -o agent -g agent \
      /tmp/zig-cache-global /tmp/zig-cache-local /tmp/go-build

# The same command the stub self-test runs, as the uid task.toml actually gives the
# solver.  Nothing above could catch a uid-only failure, which is exactly how the
# cache ownership bug survived: stage 2 builds as root and passes, while the agent
# cannot build at all.
#
# ZIG_*_CACHE_DIR is deliberately not passed through -- it has to arrive from the
# image ENV, the way it will for the solver -- and `test ! -e .zig-cache` asserts
# that it did.  Without it zig caches into the working directory, i.e. inside the
# tree Harbor collects as the submission.
# The .git prune is not decoration.  The two self-tests above count with a bare
# `find . -type f`, which equals REPO_FILES only because they run before
# `git init`; after the baseline commit the same expression also counts every
# object and ref under .git -- 94 files here, not 33.
RUN runuser -u agent -- env REPO_FILES="${REPO_FILES}" bash -eu -c '\
      cd /workspace/repo; \
      files() { find . -path ./.git -prune -o -type f -print; }; \
      before="$(files | sort | xargs sha256sum | sha256sum)"; \
      zig build; \
      test -x zig-out/bin/yaml-probe; \
      zig build test; \
      test ! -e .zig-cache; \
      test ! -e zig-cache; \
      rm -rf zig-out; \
      test "$(files | wc -l)" = "$REPO_FILES"; \
      test "$(files | sort | xargs sha256sum | sha256sum)" = "$before"; \
      test -z "$(git status --porcelain)"'

WORKDIR /workspace/repo
USER agent
