# SWERefactorBench - lang03-sqlparse-python-to-go - agent environment
#
# State A is sqlparse 0.5.3, a pure-Python SQL parser.  State B is the same
# library as a standard-library-only Go module.  The image is built from a
# digest-pinned Go base whose apt sources are re-pointed at the exact Debian
# snapshot that base image documents, so the toolchain closure is reproducible
# years from now.
#
# This task's source language is one the image cannot be without: python3 ships
# in the base image and the grader itself is written in Python.  So "the old
# implementation is gone" cannot be enforced by removing the interpreter here --
# it is enforced at grading time, where every interpreter name on PATH is a shim
# that fails and records the attempt.  The interpreter present in this image is
# deliberate and useful: the solver is expected to run the Python reference,
# read its behavior, and diff against it for as long as it remains in the tree.
#
# The workspace carries no repository-management data of any kind.  An agent that
# could read upstream history could read the migration out of a later commit
# instead of doing it.
FROM golang:1.25.12-bookworm@sha256:ea341baa9bd5ba6784f6d7161ace70544349a6242d54d34a0fbfd2c4d51c9d58 AS toolchain

ARG DEBIAN_FRONTEND=noninteractive
ARG DEBIAN_SNAPSHOT=20260713T000000Z

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Pin the package closure to the snapshot recorded inside the base image, then
# install exactly the versions this task was calibrated against.
#
# sources.list.d/debian.sources is removed, not just overwritten: the base image
# ships its pins there, and writing /etc/apt/sources.list alone would leave the
# original file in place and resolve against the live archive.
#
# pytest is the whole addition, and it is here so the reference's own 461-case
# suite runs offline.  That suite is the most concentrated statement of State A's
# behavior in the repository, and a solver that can run it can see what it is
# porting.  It is not what the submission is graded against -- grading uses the
# frozen expectations in the verifier image -- and the solver is free to delete
# tests/ along with the rest of the Python.
RUN set -eux; \
    rm -f /etc/apt/sources.list /etc/apt/sources.list.d/*; \
    printf '%s\n' \
      'Types: deb' \
      "URIs: http://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}" \
      'Suites: bookworm bookworm-updates' \
      'Components: main' \
      'Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg' \
      '' \
      'Types: deb' \
      "URIs: http://snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT}" \
      'Suites: bookworm-security' \
      'Components: main' \
      'Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg' \
      > /etc/apt/sources.list.d/debian.sources; \
    echo 'Acquire::Check-Valid-Until "false";' > /etc/apt/apt.conf.d/99snapshot; \
    apt-get -o Acquire::Retries=8 update; \
    apt-get install -y --no-install-recommends \
      python3-attr=22.2.0-1 \
      python3-iniconfig=1.1.1-2 \
      python3-packaging=23.0-1 \
      python3-pluggy=1.0.0+repack-1 \
      python3-py=1.11.0-1 \
      python3-pytest=7.2.1-2; \
    rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*.deb /var/cache/apt/*.bin; \
    go version | grep -Fx 'go version go1.25.12 linux/amd64'; \
    gofmt --help 2>&1 | head -1 | grep -F 'usage: gofmt'; \
    python3 -c 'import sys; assert sys.version_info[:2] == (3, 11), sys.version'; \
    python3 -m pytest --version | grep -F 'pytest 7.2.1'; \
    git --version; \
    tar --version | head -1

# The Go environment a submission will be built with, written into the image so a
# solver discovers the offline rule at the first `go get` rather than at
# submission time.  GOTOOLCHAIN=local is inherited from the base image and
# re-asserted below: it stops a `toolchain` line in go.mod from asking for a Go
# this image does not have and cannot download.
#
# GOFLAGS=-mod=mod matches what the verifier passes.  GOPROXY=off and GOSUMDB=off
# turn a missing dependency into a build failure instead of a network timeout,
# which is the honest signal here -- the module closure is required to be the
# standard library plus itself, so there is nothing to fetch.
RUN set -eux; \
    go env -w GOFLAGS=-mod=mod; \
    go env -w GOPROXY=off; \
    go env -w GOSUMDB=off; \
    go env -w GOTOOLCHAIN=local; \
    go env -w CGO_ENABLED=0; \
    test "$(go env GOPROXY)" = "off"; \
    test "$(go env GOTOOLCHAIN)" = "local"

RUN set -eux; \
    groupadd --gid 2000 agent; \
    useradd --uid 2000 --gid 2000 --create-home --shell /bin/bash agent; \
    install -d -m 0755 -o agent -g agent /workspace

# ---------------------------------------------------------------------------
# State A materialization.  The archive is authenticated before it is opened and
# the extracted tree is audited afterwards, so neither a substituted archive nor
# a stray VCS directory can reach the workspace.
#
# The archive stores paths at its root rather than under a repo/ prefix, so the
# destination directory is created first and tar extracts into it.
# ---------------------------------------------------------------------------
FROM toolchain AS state-a

COPY --chown=0:0 original.tar.gz /run/state-a/original.tar.gz
COPY --chown=0:0 source-contract.json /run/state-a/source-contract.json
COPY --chown=0:0 verify_environment.py /run/state-a/verify_environment.py

RUN set -eux; \
    echo '1e1873b15c03f7984badb9c1a52a15cd416e4c8d21371bd3e2e5c906362b1b1f  /run/state-a/original.tar.gz' \
      | sha256sum -c -; \
    echo '2df0cf4b8486f5acac3d2ae44cdf7d0681be66171d131504d1ca606ac2d12b35  /run/state-a/source-contract.json' \
      | sha256sum -c -; \
    test "$(stat -c '%s' /run/state-a/original.tar.gz)" = "73963"; \
    install -d -m 0755 /workspace/repo; \
    tar --extract --file=/run/state-a/original.tar.gz --directory=/workspace/repo \
        --no-same-owner --no-same-permissions; \
    test -f /workspace/repo/sqlparse/__init__.py; \
    test -f /workspace/repo/pyproject.toml; \
    test "$(find /workspace/repo -type f | wc -l)" = "73"; \
    test -z "$(find /workspace/repo \
      \( -type l -o \( ! -type d ! -type f \) \) -print -quit)"; \
    test -z "$(find /workspace/repo \
      \( -name .git -o -name .github -o -name .gitignore \
         -o -name .gitattributes -o -name .gitmodules -o -name .hg \
         -o -name .svn -o -name .bzr -o -name _darcs -o -name CVS \
         -o -name .agit -o -name .idea -o -name .readthedocs.yaml \
         -o -name PKG-INFO -o -name '*.pyc' \) -print -quit)"; \
    install -d -m 0755 /opt/swerefactor; \
    install -m 0444 /run/state-a/source-contract.json \
      /opt/swerefactor/source-contract.json

# Prove the delivered environment can exercise State A and build Go offline,
# before any solver touches it, then discard every artifact so the workspace the
# agent receives is exactly the source snapshot.
#
# The Python side runs the reference's own test suite.  461 passing cases is the
# claim that the starting point works; a snapshot that could not pass its own
# tests would have every downstream expectation frozen against a broken oracle.
#
# The Go side builds a module with the same offline settings a submission gets,
# then asserts the two failure modes a solver has to know about: a third-party
# dependency cannot resolve (GOPROXY=off), and a `go` directive above what the
# local toolchain provides is rejected (GOTOOLCHAIN=local).  Both are cheaper to
# learn here than at grading time.
#
# The dependency probe imports the package as well as requiring it.  A bare
# `require` line for a module nothing imports is not resolved at all -- `go build
# ./...` with an unused require succeeded here, and the check passed while proving
# nothing.  The import is what forces the lookup.
#
# Both probes match their stderr, not just a non-zero exit, and both texts were
# measured in this image rather than recalled:
#
#   main.go:4:8: module lookup disabled by GOPROXY=off
#   go: go.mod requires go >= 1.99 (running go 1.25.12; GOTOOLCHAIN=local)
#
# The reason is the first version of the dependency probe, which built a main.go
# through `printf '%s\n' ... '\t"fmt"' ...`.  That writes a literal backslash, so
# Go rejected the file at `main.go:4:1: illegal character U+005C` -- a syntax
# error.  The build did fail, the probe's `if go build` was satisfied, and what it
# had actually proven was that malformed source does not compile.  Hence no tabs
# here, and hence a grep for the specific diagnostic in both halves: an
# offline-enforcement probe that accepts any failure passes in an image with
# working network as long as something else is broken.
#
# Cleanup runs before verify_environment.py, not after.  Importing sqlparse and
# running its test suite leaves __pycache__ behind, and the audit rejects .pyc as
# build residue -- correctly, so the audit has to be the last thing that looks at
# the tree if it is to certify the tree the agent actually receives.  It also owns
# the file count now (--expect-files 73) instead of a duplicate `find | wc -l`
# here.
RUN set -eux; \
    cd /workspace/repo; \
    python3 -c 'import sqlparse; assert sqlparse.__version__ == "0.5.3"'; \
    printf 'select a, b from t where c = 1;\n' \
      | python3 -m sqlparse --reindent - > /tmp/reindent.out; \
    grep -Fxq 'select a,' /tmp/reindent.out; \
    python3 -m pytest tests/ -q > /tmp/pytest.log 2>&1 \
      || { tail -40 /tmp/pytest.log; exit 1; }; \
    grep -Eq '461 passed' /tmp/pytest.log; \
    install -d -m 0755 /tmp/goprobe; \
    cd /tmp/goprobe; \
    printf '%s\n' 'module probe.example/p' '' 'go 1.22' > go.mod; \
    printf '%s\n' 'package main' '' 'import "fmt"' '' \
      'func main() { fmt.Println("offline-ok") }' > main.go; \
    go build -o /tmp/goprobe/p . ; \
    /tmp/goprobe/p | grep -Fx 'offline-ok'; \
    go vet ./...; \
    test -z "$(gofmt -l .)"; \
    printf '%s\n' 'module probe.example/p' '' 'go 1.22' '' \
      'require github.com/pkg/errors v0.9.1' > go.mod; \
    printf '%s\n' 'package main' '' 'import "fmt"' \
      'import "github.com/pkg/errors"' '' \
      'func main() { fmt.Println(errors.New("x")) }' > main.go; \
    if go build ./... 2>/tmp/offline.err; then \
      echo 'FAIL: a third-party import resolved with GOPROXY=off' >&2; exit 1; \
    fi; \
    grep -qF 'module lookup disabled by GOPROXY=off' /tmp/offline.err \
      || { echo 'FAIL: build failed, but not for want of the module:' >&2; \
           cat /tmp/offline.err >&2; exit 1; }; \
    printf '%s\n' 'package main' '' 'import "fmt"' '' \
      'func main() { fmt.Println("offline-ok") }' > main.go; \
    printf '%s\n' 'module probe.example/p' '' 'go 1.99' > go.mod; \
    if go build ./... 2>/tmp/directive.err; then \
      echo 'FAIL: go directive 1.99 was accepted' >&2; exit 1; \
    fi; \
    grep -qF 'requires go >= 1.99' /tmp/directive.err \
      && grep -qF 'GOTOOLCHAIN=local' /tmp/directive.err \
      || { echo 'FAIL: build failed, but not on the go directive:' >&2; \
           cat /tmp/directive.err >&2; exit 1; }; \
    find /workspace/repo -name '__pycache__' -type d -prune -exec rm -rf {} +; \
    find /workspace/repo -name '.pytest_cache' -type d -prune -exec rm -rf {} +; \
    find /workspace/repo -name '*.py[cod]' -delete; \
    /usr/bin/python3 -I /run/state-a/verify_environment.py \
      --repo /workspace/repo \
      --contract /opt/swerefactor/source-contract.json \
      --expect-files 73; \
    rm -rf /tmp/goprobe /tmp/reindent.out /tmp/pytest.log /tmp/offline.err \
      /tmp/directive.err /run/state-a; \
    test ! -e /run/state-a

# A single history-free baseline commit gives the solver a local diff surface.
# The verifier never trusts this repository: it carries its own copy of the State
# A tree and treats .git as untrusted agent-writable data, discarding it before
# grading.
RUN set -eux; \
    cd /workspace/repo; \
    git -c init.defaultBranch=main init -q .; \
    git config user.name 'SWERefactorBench'; \
    git config user.email 'bench@swerefactor.invalid'; \
    git config commit.gpgsign false; \
    git config gc.auto 0; \
    git add -A; \
    GIT_AUTHOR_DATE='2000-01-01T00:00:00 +0000' \
    GIT_COMMITTER_DATE='2000-01-01T00:00:00 +0000' \
    GIT_AUTHOR_NAME='SWERefactorBench' \
    GIT_COMMITTER_NAME='SWERefactorBench' \
    GIT_AUTHOR_EMAIL='bench@swerefactor.invalid' \
    GIT_COMMITTER_EMAIL='bench@swerefactor.invalid' \
      git commit -q -m 'State A: sqlparse 0.5.3 (Python implementation)'; \
    test "$(git rev-list --count HEAD)" = "1"; \
    test -z "$(git status --porcelain)"; \
    git log --format=%H -1 > /opt/swerefactor/baseline-commit.txt; \
    chmod 0444 /opt/swerefactor/baseline-commit.txt; \
    chown -R agent:agent /workspace

# GOPATH and GOMODCACHE are named rather than inherited, and the directory is
# created writable, because this image runs as uid 2000 and the toolchain image's
# default GOPATH is /go with a root-owned /go/pkg.  Nothing is ever fetched here --
# GOPROXY=off, and the offline assertion above proves a third-party import cannot
# resolve -- but `go build` still writes a stat-cache entry for the main module's
# own path, and with an unwritable cache every build the solver runs prints
#
#   go: writing stat cache: mkdir /go/pkg/mod/cache/download/github.com/...: permission denied
#
# which is worse than noise: it names a github.com path and a module cache in a
# task whose contract forbids third-party modules and whose solver is being graded
# on not fetching anything.  A model reading that line can reasonably conclude it
# has a dependency problem to solve and spend a round proving it does not.  The
# sibling Go task (lang05) names these for the same reason.
ENV CGO_ENABLED=0 \
    GOFLAGS=-mod=mod \
    GOPROXY=off \
    GOSUMDB=off \
    GOTOOLCHAIN=local \
    GOPATH=/opt/gopath \
    GOMODCACHE=/opt/gopath/pkg/mod \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=UTC \
    LC_ALL=C.UTF-8 \
    LANG=C.UTF-8

# The solver's harness runs every command through `bash -lc`, and a login shell
# reads /etc/profile, which sets PATH from scratch and discards the PATH this image
# declares in ENV.  The Go toolchain lives in /usr/local/go/bin, which is on the ENV
# PATH and not on the one a login shell builds, so without the profile.d file below
# `go version` answers "go: command not found" in the only shell the solver has --
# in an image whose entire purpose is that it can build Go.  The solver can recover
# by finding the toolchain itself, and a capable one does, but it pays rounds for it
# and a less capable one concludes Go is not installed.
#
# So PATH is exported where a login shell will read it, and the same file carries
# GOPATH and GOMODCACHE.  The assertion runs as the agent, through a login shell,
# because that is the only configuration the claim is about: root passes this with
# the profile.d file deleted, and so does a non-login shell.
# Only PATH needs restoring here.  Every other variable above survives, because a
# login shell inherits the process environment and /etc/profile rewrites PATH alone
# -- GOPROXY=off, GOSUMDB=off, GOFLAGS, CGO_ENABLED and GOTOOLCHAIN all still reach
# the solver, and the offline guarantee they carry is intact.  Restoring more than
# PATH would only duplicate ENV.
#
# The assertion drops privilege with `runuser`, not `su`, and the difference is the
# whole reason this layer failed twice before it passed.  `su agent -s /bin/bash -lc`
# does not run a login bash with the image's environment: `su` takes the `-l` for
# itself, enters login mode, and clears the environment on its way in -- measured
# here, GOPROXY arrives empty and `go env GOPROXY` answers proxy.golang.org, so the
# offline assertion fails against a shell no solver is ever given.  `su agent
# -s /bin/bash -c 'bash -lc ...'` would work, at the price of a second quoting level
# around the probe.  `runuser -u agent -- bash -lc` passes the flag to bash, keeps the
# environment, and needs no PAM, which is what the claim is about.
RUN install -d -m 0755 -o agent -g agent \
      /opt/gopath /opt/gopath/pkg /opt/gopath/pkg/mod \
 && printf '%s\n' 'export PATH=/usr/local/go/bin:$PATH' \
      > /etc/profile.d/10-swerefactor.sh \
 && chmod 0644 /etc/profile.d/10-swerefactor.sh \
 && runuser -u agent -- bash -lc '\
      set -e; \
      go version; \
      test "$(go env GOPROXY)" = off; \
      test "$(go env GOMODCACHE)" = /opt/gopath/pkg/mod; \
      test -w "$(go env GOMODCACHE)"; \
      d=$(mktemp -d); cd "$d"; \
      printf "module probe\n" > go.mod; \
      printf "package main\nfunc main() {}\n" > main.go; \
      go build -o /dev/null ./... 2>/tmp/probe.err; \
      if [ -s /tmp/probe.err ]; then \
          echo "FAIL: a clean build wrote to stderr in the solver shell:" >&2; \
          cat /tmp/probe.err >&2; exit 1; \
      fi; \
      rm -rf "$d" /tmp/probe.err'

WORKDIR /workspace/repo
USER agent
