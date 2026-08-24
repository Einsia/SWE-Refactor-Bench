# syntax=docker/dockerfile:1
# =============================================================================
# SWERefactorBench -- lang05-goyaml-go-to-zig
# Stage 2 image: the behavioural suite.
#
#     docker build -t swerefactor/lang05-behavioural:1 \
#       tasks/lang05-goyaml-go-to-zig/tests/behavioural
#
# Built from the same digest-pinned base and the same Debian snapshot as the agent
# environment, so a submission that builds there builds here for the same reasons.
#
# Four properties of this file are load-bearing:
#
#  1. It carries its own copy of original.tar.gz and never reads the submitted
#     tree to learn what State A was.  The baseline is how the suite tells an
#     unchanged file from a rewritten one, so it cannot come from the party being
#     graded.  The digest below is the one environment/original.sha256 records; if
#     the two copies ever diverge, one of the two builds fails on its checksum
#     instead of grading against the wrong baseline.
#
#  2. freeze.py runs here, at image build time.  It builds go-yaml v3.0.1 and the
#     case generator, generates the cases, and records the reference's answer to
#     every one into an immutable blob.  By the time a submission exists the
#     answers are already fixed and digested; grading only compares against them.
#     Computing expectations at grading time would put the submission in the
#     container while the answers were being decided.
#
#  3. Go survives into the graded image, confined to /usr/local/go and off PATH,
#     and is deleted at run time by lib/build.py -- before the build when the
#     submission is Zig, after it when the submission is still the Go module it
#     started as.  The claim under test is that go-yaml is now a Zig program, and a
#     stage that left a reachable Go toolchain lying around would let a submission
#     shell out to the reference and pass every comparison by being it.  What makes
#     keeping it safe is that the removal happens before any module drives a
#     submission's binary, and `provenance/shim-clean` scores the record of it.
#     The graded stage asserts the two properties that make one `rm -rf` enough:
#     no Go tool on PATH, and no Go executable outside GOROOT.
#
#     Why keep it at all: the suite declares two build drivers and resolves one
#     from the tree.  An unmodified State A is a Go module, and grading it as
#     nothing but a failed `zig build` would measure the absence of a port with the
#     same score as a broken one.  Which driver ran is recorded, and the three
#     provenance checks a Go tree cannot answer are unscored by name in the
#     contract rather than silently passed.
#
#  4. What does survive is the reference *binary* -- static, CGO_ENABLED=0 -- so
#     the unseen-inputs module can put questions to upstream go-yaml at grading
#     time that nobody has seen the answers to.  A static binary and a compiler
#     are very different things to leave in a grading image.
#
# At runtime the harness provides:
#
#     /workspace/repo    the submission                        (read-write copy)
#     /opt/original      State A, unpacked from original.tar.gz     (read-only)
#     /logs/verifier     where the stage result JSON is written
#
# The suite reads its baseline from the frozen assets, not from /opt/original --
# that mount is there for the stages that compare trees.  Network: none.
# =============================================================================

ARG INFRA_IMAGE=swerefactor/infra:1
FROM ${INFRA_IMAGE} AS infra

FROM debian@sha256:7b140f374b289a7c2befc338f42ebe6441b7ea838a042bbd5acbfca6ec875818 AS toolchain

# Every ARG is declared in the stage that reads it, and repeated where a later
# stage reads it again.  BuildKit does carry a parent stage's ARGs into a child, but
# that is not something this file should depend on: the documented scope is the
# stage, and a build on an older or stricter frontend would otherwise silently
# expand `${GO_SHA256}` to nothing and check a downloaded tarball against an empty
# digest.
ARG DEBIAN_FRONTEND=noninteractive
ARG DEBIAN_SNAPSHOT=20251020T000000Z
ARG ZIG_VERSION=0.14.1
ARG ZIG_SHA256=24aeeec8af16c381934a6cd7d95c807a8cb2cf7df9fa40d359aa884195c4716c

SHELL ["/bin/bash", "-euo", "pipefail", "-c"]
ENV TZ=UTC \
    LC_ALL=C.UTF-8 \
    LANG=C.UTF-8

# The same frozen snapshot as the agent image, and `rm -f sources.list.d/*` for
# the same reason it is there: writing sources.list does not replace the
# distribution's own debian.sources, and without the removal these pins resolve
# against the live archive.
RUN rm -f /etc/apt/sources.list.d/*; \
    printf '%s\n' \
      "deb http://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT} bookworm main" \
      "deb http://snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT} bookworm-security main" \
      "deb http://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT} bookworm-updates main" \
      > /etc/apt/sources.list; \
    printf 'Acquire::Check-Valid-Until "false";\n' \
      > /etc/apt/apt.conf.d/99no-check-valid-until; \
    printf 'Acquire::Retries "5";\n' > /etc/apt/apt.conf.d/99retries

# The agent image's closure, minus what only a solver needs (git, make) and plus
# nothing.  The suite's ELF reading is done by lib/elflib.py, which parses the
# headers itself with `struct` -- so binutils is deliberately absent rather than
# accidentally so, and a future module that wants `readelf` has to add it here and
# say why.
RUN apt-get -qq update \
 && apt-get -qq install -y --no-install-recommends \
      ca-certificates=20230311+deb12u1 \
      curl=7.88.1-10+deb12u14 \
      xz-utils=5.4.1-1 \
      python3=3.11.2-1+b1 \
 && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*.deb /var/cache/apt/*.bin

# Zig, which stays: the build module compiles the submission with it.
RUN cd /tmp \
 && curl -fsSLO "https://ziglang.org/download/${ZIG_VERSION}/zig-x86_64-linux-${ZIG_VERSION}.tar.xz" \
 && echo "${ZIG_SHA256}  zig-x86_64-linux-${ZIG_VERSION}.tar.xz" | sha256sum -c - \
 && install -d /opt/zig \
 && tar -xJf "zig-x86_64-linux-${ZIG_VERSION}.tar.xz" -C /opt/zig --strip-components=1 \
 && rm -f "zig-x86_64-linux-${ZIG_VERSION}.tar.xz" \
 && ln -s /opt/zig/zig /usr/local/bin/zig \
 && zig version | grep -Fx "${ZIG_VERSION}" \
 && python3 --version | cut -d' ' -f2 | grep -Fx '3.11.2'

# Zig writes .zig-cache into the working directory by default, and the working
# directory during grading is a copy of the submission.  Pointed elsewhere so a
# build cannot deposit anything inside the tree being measured.
ENV ZIG_GLOBAL_CACHE_DIR=/tmp/zig-cache-global \
    ZIG_LOCAL_CACHE_DIR=/tmp/zig-cache-local
RUN install -d -m 0777 /tmp/zig-cache-global /tmp/zig-cache-local

# ---------------------------------------------------------------------------
# The two instruments' toolchain, present in this stage and no later one
# ---------------------------------------------------------------------------
FROM toolchain AS gotoolchain

ARG GO_VERSION=1.23.4
ARG GO_SHA256=6924efde5de86fe277676e929dc9917d466efa02fb934197bc2eba35d5680971

RUN cd /tmp \
 && curl -fsSLO "https://go.dev/dl/go${GO_VERSION}.linux-amd64.tar.gz" \
 && echo "${GO_SHA256}  go${GO_VERSION}.linux-amd64.tar.gz" | sha256sum -c - \
 && tar -xzf "go${GO_VERSION}.linux-amd64.tar.gz" -C /usr/local \
 && rm -f "go${GO_VERSION}.linux-amd64.tar.gz" \
 && /usr/local/go/bin/go version | cut -d' ' -f3 | grep -Fx "go${GO_VERSION}"

# ---------------------------------------------------------------------------
# Baseline: the suite's own authenticated copy of State A
# ---------------------------------------------------------------------------
FROM gotoolchain AS baseline

ARG REPO_SHA256=4a9603dd8ad249177cd87b1f01ba6fe1794ddec017766ed371bba1df03b46ab2
ARG REPO_SIZE=99730
ARG REPO_FILES=33
ARG ZIG_VERSION=0.14.1
ARG UPSTREAM_VERSION=v3.0.1
ARG UPSTREAM_SHA256=cf05411540d3e6ef8f1fd88434b34f94cedaceb540329031d80e23b74540c4e5

COPY --chown=0:0 data/original.tar.gz /run/baseline/original.tar.gz
COPY --chown=0:0 data/source-contract.json /run/baseline/source-contract.json

# Five of the ARGs above are also published by source-contract.json, which is the
# document the agent was given -- so each one has two homes and could drift.
# freeze.py already checks the two version strings against its own constants; these
# are the three digests and counts it does not, checked here because this is the
# stage where the values are used.  A pin that disagreed with the contract would
# otherwise mean grading against a tree the agent was never promised.
RUN python3 - "${REPO_SHA256}" "${REPO_SIZE}" "${REPO_FILES}" \
             "${ZIG_VERSION}" "${UPSTREAM_VERSION}" "${UPSTREAM_SHA256}" <<'EOF'
import json, sys
sha, size, files, zig, upv, upsha = sys.argv[1:7]
c = json.load(open("/run/baseline/source-contract.json"))
want = [
    ("state_a.archive_sha256", c["state_a"]["archive_sha256"], sha),
    ("state_a.file_count", c["state_a"]["file_count"], int(files)),
    ("state_b.zig_version", c["state_b"]["zig_version"], zig),
    ("native_code_policy.zig_toolchain", c["native_code_policy"]["zig_toolchain"], zig),
    ("product.upstream_version", c["product"]["upstream_version"], upv),
    ("product.upstream_archive_sha256", c["product"]["upstream_archive_sha256"], upsha),
]
bad = [f"  {k}: contract says {v!r}, Dockerfile pins {a!r}"
       for k, v, a in want if v != a]
if bad:
    sys.exit("the image pins disagree with the source contract:\n" + "\n".join(bad))
print(f"pins agree with the contract ({len(want)} checked); "
      f"tarball {size} bytes, {files} files")
EOF

RUN echo "${REPO_SHA256}  /run/baseline/original.tar.gz" | sha256sum -c - \
 && test "$(stat -c '%s' /run/baseline/original.tar.gz)" = "${REPO_SIZE}" \
 && install -d /run/unpack \
 && tar --extract --file=/run/baseline/original.tar.gz --directory=/run/unpack \
        --no-same-owner --no-same-permissions \
 && test -f /run/unpack/repo/parserc.go \
 && test -f /run/unpack/repo/build.zig \
 && test "$(find /run/unpack/repo -type f | wc -l)" = "${REPO_FILES}" \
 && test ! -e /run/unpack/repo/.git

# ---------------------------------------------------------------------------
# The module cache the Go driver builds against
# ---------------------------------------------------------------------------
# State A is a Go module, and one of the two build drivers compiles it as one.
# That build runs offline with `GOPROXY=off` and `-mod=readonly`, so every module
# its graph names has to already be here.  Populated now, at image build time,
# from the go.mod of the tree this stage has just authenticated -- so the cache is
# derived from the baseline rather than pinned separately from it, and cannot name
# a version State A does not require.
#
# `go mod download` is not what does it, and must not be what does it.  It fills
# the download cache with zips and go.mod files, but the driver's test step
# compiles the repository's `_test.go` files, which import check.v1 -- and that
# needs the module *extracted*.  `go test -run '^$'` builds every test binary and
# runs no test, which extracts what it needs and lets the toolchain decide the
# layout rather than this file guessing at it.
#
# `go mod download all` is also the one subcommand that edits go.mod.  Measured
# across build/vet/test/list/download at go1.23.4, offline, with
# `-mod=readonly`: only `download all` rewrote the file, and it rewrote three
# things -- `module "gopkg.in/yaml.v3"` unquoted, the `require` block collapsed to
# one line, and a `go 1.23.4` directive inserted that upstream's go.mod does not
# have.  `-mod=readonly` does not prevent it, so the subcommand is not run in this
# step and the `cmp` below is what would catch its return.
#
# In a copy, not in /run/unpack/repo, and the copy is compared afterwards.  A `go`
# subcommand that edits go.mod would otherwise edit the tree freeze.py is about to
# stage as the baseline -- and the baseline is what every module compares a
# submission against, so a rewritten quote there would show up as an edit the
# submission never made.  The comparison passes today; it is here for the next
# toolchain bump, where a subcommand's behaviour can change without notice.
RUN install -d /tmp/prime \
 && cp -a /run/unpack/repo /tmp/prime/repo \
 && cd /tmp/prime/repo \
 && env GOROOT=/usr/local/go \
        PATH=/usr/local/go/bin:"${PATH}" \
        GOMODCACHE=/opt/gomodcache \
        GOPATH=/tmp/prime/gopath \
        GOCACHE=/tmp/prime/gocache \
        GOFLAGS=-mod=readonly \
        CGO_ENABLED=0 \
        GOTOOLCHAIN=local \
        go test -run '^$' -count=1 ./... \
 && cd / \
 && test -n "$(find /opt/gomodcache/gopkg.in -maxdepth 1 -name 'check.v1@*' -type d)" \
 && cmp /tmp/prime/repo/go.mod /run/unpack/repo/go.mod \
 && rm -rf /tmp/prime \
 && echo "primed /opt/gomodcache: $(du -sh /opt/gomodcache | cut -f1); go.mod intact"

# ---------------------------------------------------------------------------
# Upstream go-yaml, the `replace` target the reference is built against
# ---------------------------------------------------------------------------
# Fetched rather than taken from the baseline, even though the baseline holds the
# same Go sources.  The reference has to be the library as upstream published it,
# and "the tree we handed the agent, which we also assembled" is a weaker claim
# than a digest over a release tarball -- if the two ever disagreed, this is the
# copy that should win.  The digest is the one source-contract.json publishes as
# `product.upstream_archive_sha256`.
FROM baseline AS upstream

ARG UPSTREAM_VERSION=v3.0.1
ARG UPSTREAM_SHA256=cf05411540d3e6ef8f1fd88434b34f94cedaceb540329031d80e23b74540c4e5

# Both values were checked against the contract in the previous stage.
RUN cd /tmp \
 && curl -fsSL -o goyaml.tar.gz \
      "https://codeload.github.com/go-yaml/yaml/tar.gz/refs/tags/${UPSTREAM_VERSION}" \
 && echo "${UPSTREAM_SHA256}  goyaml.tar.gz" | sha256sum -c - \
 && install -d /run/upstream \
 && tar -xzf goyaml.tar.gz -C /run/upstream --strip-components=1 \
 && rm -f goyaml.tar.gz \
 && test -f /run/upstream/parserc.go \
 && test -f /run/upstream/LICENSE

# ---------------------------------------------------------------------------
# Freeze: build the instruments, generate the cases, record the answers
#
# freeze.py stages its own fixed inputs (the baseline tree, the Go tripwire, the
# documents, the handwritten protocol lines, the source contract) into
# /opt/assets, so the whole grading input set is one directory that can be sealed
# in one step.  It lives beside /opt/swerefactor rather than inside it: the harness
# there is shared by every task and arrives from the donor image, and one
# directory should not be half vendored code and half this task's answers.
# ---------------------------------------------------------------------------
FROM upstream AS frozen

COPY --chown=0:0 . /tests/behavioural

# The tarball and this file are build context, not grading input: leaving them in
# the image would mean two copies of State A inside it, one of them unverified.
# The compile is a syntax check over every module the driver will import -- a
# NameError twenty minutes into a grading run is a dead verifier, and it is
# cheaper to find here.
RUN rm -f /tests/behavioural/data/original.tar.gz /tests/behavioural/Dockerfile \
          /tests/behavioural/.dockerignore \
 && find /tests/behavioural/lib -name '*.py' -print0 | xargs -0 python3 -m py_compile \
 && python3 -I /tests/behavioural/lib/freeze.py \
      --baseline /run/unpack/repo \
      --upstream /run/upstream \
      --goroot /usr/local/go \
      --assets /opt/assets \
      --suite /tests/behavioural \
      --workspace /tmp/freeze \
      --image lang05-goyaml-go-to-zig-behavioural \
 && rm -rf /tmp/freeze /run/unpack /run/baseline /run/upstream

# ---------------------------------------------------------------------------
# Go out, and asserted out
# ---------------------------------------------------------------------------
# Go survives into this stage, and has to: one of the two declared build drivers
# compiles State A as the Go module it is, and a stage carrying no compiler could
# not run that driver at all -- which would mean the unmodified repository
# failing the build cases for want of a toolchain rather than for anything it
# did.
#
# So "there is no Go here" is not the claim.  Three narrower ones are, the first
# two checked below and the third enforced at run time by lib/build.py:
#
#   1. `go` is not reachable.  /usr/local/go/bin is not on this image's PATH, so
#      nothing that does not go looking finds a compiler.  The check is aimed at
#      the copy this file did not install: a distribution `golang-go` in /usr/bin
#      would satisfy every other assertion here and hand a Zig submission a
#      compiler through plain `go build`.
#   2. Every Go executable on the filesystem is inside GOROOT.  That is what
#      makes one `rm -rf` sufficient, because GOROOT is what lib/build.py
#      deletes.
#   3. At run time, `Builder.retire_go_toolchain` removes GOROOT, the module
#      cache and Go's leftovers *before* a Zig build and *after* a Go one, so no
#      module that drives a submission's binary ever runs with a compiler
#      present.  The `provenance/shim-clean` case reads that removal record, and
#      on the Zig path it is scored.
#
# Kept in place rather than restaged from `toolchain`: the frozen assets are
# 40-odd MB of expectations that would have to be copied between stages, and a
# COPY that silently missed one of them would be a module reading an absent file.
FROM frozen AS graded

# freeze.py's own GOPATH and build cache, which nothing downstream reads.  Removed
# so that the state lib/build.py finds before a graded build is the state this file
# describes, rather than that plus whatever the freeze happened to leave.
RUN rm -rf /root/go /root/.cache/go-build \
 && for tool in go gofmt godoc gopls gccgo go1.23.4 gorun goimports \
                golangci-lint dlv cgo; do \
      if command -v "$tool" >/dev/null 2>&1; then \
        echo "a Go tool is on PATH in the grading image: $tool at $(command -v "$tool")" >&2; \
        exit 1; \
      fi; \
    done \
 && test -x /usr/local/go/bin/go \
 && stray="$(find / -xdev -type f -name 'go' -perm -u+x \
               -not -path '/usr/local/go/*' 2>/dev/null)" \
 && if [ -n "$stray" ]; then \
      echo "a Go executable outside GOROOT: $stray" >&2; exit 1; \
    fi \
 && test ! -e /root/go && test ! -e /root/.cache/go-build \
 && echo "no Go on PATH; the toolchain is confined to /usr/local/go"

# The module cache the Go driver needs, and the two facts lib/build.py depends on:
# that it is where GOMODCACHE points, and that the offline build can be satisfied
# from it.  Asked by building State A's own probe command with the network refused,
# in a copy, and then thrown away -- because a claim that the graded build will
# work offline is worth more when something has actually done it.
RUN test -d /opt/gomodcache \
 && install -d /tmp/cachecheck \
 && cp -a /opt/assets/baseline /tmp/cachecheck/repo \
 && chmod -R u+w /tmp/cachecheck/repo \
 && cd /tmp/cachecheck/repo \
 && env GOROOT=/usr/local/go \
        PATH=/usr/local/go/bin:"${PATH}" \
        GOMODCACHE=/opt/gomodcache \
        GOPATH=/tmp/cachecheck/gopath \
        GOCACHE=/tmp/cachecheck/gocache \
        GOPROXY=off \
        GOFLAGS=-mod=readonly \
        CGO_ENABLED=0 \
        GOTOOLCHAIN=local \
        go build -o /tmp/cachecheck/probe ./cmd/yaml-probe \
 && test -x /tmp/cachecheck/probe \
 && printf '%s\n' '{"id":1,"op":"hello","source":""}' \
      | /tmp/cachecheck/probe | grep -F '"ok"' >/dev/null \
 && cd / && rm -rf /tmp/cachecheck \
 && echo "the Go driver builds offline from /opt/gomodcache"

# What did survive, and has to have: the reference the unseen-inputs module asks
# fresh questions of.  Static, so it does not care what libc this image has;
# asked through the protocol rather than trusted, because a reference that cannot
# answer `hello` here would fail every fresh case at grading time instead.
RUN test -x /opt/assets/bin/reference \
 && test -x /opt/assets/bin/generator \
 && printf '%s\n' '{"id":1,"op":"hello"}' \
      | /opt/assets/bin/reference serve \
      | grep -F '"upstream":"v3.0.1"' \
 && echo "reference answers hello: upstream v3.0.1"

# The frozen assets are the answers; nothing that runs later has any business
# writing to them.  Grading runs as root because it has to copy a submission and
# run a compiler over it, so read-only bits are a guard rail rather than a
# boundary -- but they do mean an accidental write fails loudly.
RUN chmod -R a-w /opt/assets \
 && test "$(stat -c '%a' /opt/assets/verifier-manifest.json)" = "444"

# --- The harness -------------------------------------------------------------
# The module runner, the result contract and the scorer, shared by every task.
COPY --from=infra /opt/swerefactor /opt/swerefactor

# --- The manifest, checked at build time -------------------------------------
# suite.toml is the file most likely to be edited and least likely to be tested,
# so it is parsed here by the same loader the harness will use: a duplicate id, a
# weight that does not parse, or a module directory that does not exist fails the
# build rather than the run.
#
# driver.py --self-check is second because it is the widest single check in the
# suite.  It runs catalog.check_modules() -- which is where a module weight that
# has drifted from the case budgets it grades gets caught -- then opens the frozen
# assets through the same Assets class the modules use, verifies every digest, and
# confirms each module id has a kind the driver can dispatch.  `catalog.py --check`
# is deliberately not run here: it checks generated case counts against the weight
# table and is invoked by freeze.py above, where those counts exist.
RUN chmod +x /tests/behavioural/lib/run-module.sh \
 && for d in /tests/behavioural/modules/*/; do \
      test -x "$d/run.sh" || { echo "no runner in $d" >&2; exit 1; }; \
    done \
 && PYTHONPATH=/opt/swerefactor python3 -c "\
import sys; from swerefactor import config; \
s = config.Suite.load('/tests/behavioural/suite.toml'); \
missing = [m.id for m in s.modules if not (s.root / m.dir).is_dir()]; \
sys.exit(f'suite.toml names missing module dir(s): {missing}') if missing else None; \
print(f'suite: {len(s.modules)} modules, weight {s.weight_total:g}')" \
 && python3 -I /tests/behavioural/lib/driver.py --self-check

# PYTHONDONTWRITEBYTECODE, because this RUN is the last thing that imports the
# lib, and bytecode surviving into the image would be the only mutable bytes in it
# beside the read-only assets.  Set after the checks above rather than before, so
# that the compile step is still allowed to produce the bytecode it is checking;
# what those steps wrote is swept below.
# One name is read by this suite: SWEREFACTOR_ASSETS, driver.py's default assets
# root.  suite.toml sets it too, and that copy is the one in force under the
# runner; this one is for a module invoked by hand, and tests/check-task.py
# cross-checks the two rather than trusting them to stay equal.
#
# The repo and result paths are *not* set here -- those arrive per module as
# $SRB_REPO and $SRB_RESULT from the runner, and a default baked into the image
# would be a second answer to a question the harness already answers.  Nor is
# SWEREFACTOR_LOG_DIR: reward.json belongs to swerefactor.harbor, and a module-level
# fallback writing its own version of that file would be a second writer
# disagreeing with the first about the schema, which is worse than no fallback at
# all.  A module that dies writing nothing is recorded as `error` by the runner,
# which is the contract's own answer.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=0 \
    PYTHONPATH=/opt/swerefactor \
    SWEREFACTOR_ASSETS=/opt/assets

# /opt as well as /tests/behavioural, and the assertion spans both.  Measured on
# the built image, not read off this file: the assertion passed because it was
# narrower than the claim it was standing behind.  The verification stage swept all
# three of its roots, so the contradiction was visible only in stage 2.
#
# This is the last RUN in the file.  A step added below it that imports anything
# would write bytecode this no longer catches, so new steps go above.
RUN find /tests/behavioural /opt -name __pycache__ -type d -prune -exec rm -rf {} + \
 && test -z "$(find /tests/behavioural /opt -name '*.pyc' -print -quit)" \
 && install -d -m 0755 /opt/original /workspace /logs/verifier

WORKDIR /tests/behavioural
