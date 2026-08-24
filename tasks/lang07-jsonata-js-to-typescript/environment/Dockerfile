# =============================================================================
# SWERefactorBench -- lang07-jsonata-js-to-typescript -- the agent environment
#
#     docker build -t swerefactor/lang07-env:1 \
#         tasks/lang07-jsonata-js-to-typescript/environment
#
# State A is JSONata 2.2.2: a JSON query and transformation language in nine
# JavaScript files under `src/` -- a Pratt parser, a tree-walking evaluator with
# tail calls and a stack limit, about a hundred builtins, an XPath-style datetime
# formatter, a run-time signature validator -- plus a differential probe that
# speaks the protocol the port has to speak.  Fifteen files, 358,338 bytes of
# `src/`, no dependencies of any kind.
#
# There is no TypeScript here beyond the hand-written `jsonata.d.ts`, and no
# `tsconfig.json` at all: the submission writes that file from nothing, and there
# is no stub to grow.
#
# The image is built from the same digest-pinned Debian base, the same Debian
# snapshot, and the same node and tsc pins as the verifier, so a submission that
# builds here builds there for the same reasons.
#
# The workspace carries no repository-management data from upstream: State A is
# extracted from a tarball with no .git, .gitignore, .gitattributes, .gitmodules
# or .github/.  Upstream history is an answer key, and jsonata's is a particularly
# direct one -- the project maintains its own TypeScript declarations and has an
# active issue history about typing the engine.  What the workspace does carry is
# one history-free commit made at the end of this file, so the agent has a local
# diff surface.  Grading never trusts it: the verifier holds its own copy of
# State A and treats `.git` as agent-writable data.
#
# ---------------------------------------------------------------------------
# What is different about this task, stated once, here
# ---------------------------------------------------------------------------
# Every other language pair in this benchmark supplies the old toolchain to the
# agent and withholds it from the graders.  `tommy` -- a C#-to-TypeScript pair --
# hands over a .NET SDK and then asserts `dotnet` is off PATH in the verifier, so a
# delivered probe that shells out to C# finds nothing to shell out to.
#
# This task cannot do that, and the reason is the language pair itself: TypeScript
# compiles *to* JavaScript, so `node` is simultaneously the runtime being ported
# away from and the runtime the port targets.  Withholding it from the graders
# would leave nothing to grade.
#
# Two consequences, both of which shaped this file:
#
#   1. There is no second toolchain to install.  `node` and `npm` are here once,
#      and they serve both `npm run build` on State A and `tsc -p` on the port.
#      Nothing in this image should be read as permission or as suspicion --
#      unlike tommy's SDK, node's presence here is not a decision, it is the
#      only way an image can exist at all.
#
#   2. The cheat this task actually has to defend against is not a subprocess, it
#      is a *rename*.  `mv src/jsonata.js src/jsonata.ts`, a `// @ts-nocheck`
#      banner and a tsconfig produce a tree that compiles, builds, and answers
#      all 13,940 behavioural cases perfectly -- measured, not assumed: it scores
#      40.000 of 40.  Behaviourally a rename *is* the original, so no behavioural
#      test can see it, and the whole question of whether the repository was
#      rewritten lives in stage 1 where a reader can weigh it.
#
# So this file supplies a complete, working, buildable State A and makes no
# attempt to constrain what the agent does with it.  Reading the original,
# building it, running it, and diffing a port against it are all the intended
# workflow -- the instruction says so explicitly.  What is graded is what is
# *delivered*.
#
# Network: none after the first stage.  Two artifacts, each with a digest.
# =============================================================================

ARG DEBIAN_FRONTEND=noninteractive
ARG DEBIAN_SNAPSHOT=20251020T000000Z

# --------------------------------------------------------------------------- #
# Stage `fetch` -- every out-of-band artifact, each checked by sha256
# --------------------------------------------------------------------------- #
# Separated from the toolchain stage so a download and its digest check sit in one
# layer with nothing else, and so the pins read as a list.  These are the same two
# artifacts and the same two digests every grading image in this task carries;
# `swerefactor validate` compares the pin lists across the Dockerfiles, because a
# toolchain that differs between images is a submission graded against a compiler
# it never saw.
#
# A pinned version is not a pin.  npm allows a version to be republished and
# nodejs.org serves through caches that have been wrong before; the digest is the
# pin and the version in the URL is a comment on it.
FROM debian@sha256:26f2a7cab45014541c65f9d140ccfa6aaefbb49686c6759bea9c6f7f5bb3d72f AS fetch
ARG DEBIAN_FRONTEND
ARG DEBIAN_SNAPSHOT
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

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
      ca-certificates=20230311+deb12u1 \
      curl=7.88.1-10+deb12u14 \
    ; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /dl

RUN set -eux; \
    curl -fsSL --retry 5 --retry-connrefused -o node.tar.xz \
      https://nodejs.org/dist/v24.18.1/node-v24.18.1-linux-x64.tar.xz; \
    echo 'd6c664df3f3f61458e8c277585571328522d705166723a7c7823a9253a4d15a0  node.tar.xz' \
      | sha256sum -c -; \
    curl -fsSL --retry 5 --retry-connrefused -o typescript.tgz \
      https://registry.npmjs.org/typescript/-/typescript-5.9.3.tgz; \
    echo '10e108c9cf7d5f2879053dff18515fb405abf2ccef63eaaf017d9c571687a1d3  typescript.tgz' \
      | sha256sum -c -

# --------------------------------------------------------------------------- #
# Stage `toolchain` -- the closure the agent develops in
# --------------------------------------------------------------------------- #
# The verifier's apt list, plus git and git-man, minus nothing.  Each entry earns
# its place: python3 is here because the verifier has it and a closure that
# differs is a closure that can surprise; xz-utils unpacks node; tzdata and the
# C.UTF-8 locale settle two things the corpus depends on -- `$fromMillis`
# formats through the process timezone, and a probe exchanging NDJSON byte for
# byte must not have its stdio recoded; git is for the baseline commit at the end
# of this file and for the agent's own diff surface.
#
# git-man is pinned to git's own version rather than left to apt.  It arrives as a
# recommendation, and `--no-install-recommends` means it would otherwise be
# absent, which makes `git help` fail in a way that reads like a broken image.
FROM debian@sha256:26f2a7cab45014541c65f9d140ccfa6aaefbb49686c6759bea9c6f7f5bb3d72f AS toolchain
ARG DEBIAN_FRONTEND
ARG DEBIAN_SNAPSHOT
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

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
      ca-certificates=20230311+deb12u1 \
      git=1:2.39.5-0+deb12u2 \
      git-man=1:2.39.5-0+deb12u2 \
      python3=3.11.2-1+b1 \
      tzdata=2025b-0+deb12u2 \
      xz-utils=5.4.1-1 \
    ; \
    rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*.deb /var/cache/apt/*.bin

# Node, and tsc as a global.  Global is the contract, not a shortcut: the
# verifier's `build` module deletes `node_modules` from its copy before running
# `npm run build`, so a submission whose build resolves `tsc` from a local install
# would fail on a tree that has nothing wrong with it.  State A's package.json has
# no dependencies to install from either, so this global is the only tsc that
# exists in any image in this task.
COPY --from=fetch /dl/node.tar.xz /dl/typescript.tgz /tmp/
RUN set -eux; \
    install -d /usr/local/lib/nodejs; \
    tar -xJf /tmp/node.tar.xz -C /usr/local/lib/nodejs --strip-components=1; \
    ln -s /usr/local/lib/nodejs/bin/node /usr/local/bin/node; \
    ln -s /usr/local/lib/nodejs/bin/npm /usr/local/bin/npm; \
    ln -s /usr/local/lib/nodejs/bin/npx /usr/local/bin/npx; \
    npm install -g --no-audit --no-fund /tmp/typescript.tgz; \
    ln -sf /usr/local/lib/nodejs/bin/tsc /usr/local/bin/tsc; \
    npm cache clean --force; \
    rm -rf /root/.npm; \
    rm -f /tmp/node.tar.xz /tmp/typescript.tgz

# Every version the task depends on, asserted rather than printed, in one layer
# whose failure names the pin that moved.  `tsc --version` succeeding says tsc
# runs; it does not say it is the pin, and a drift of one major version would
# change what `strict` means and what the emitted JavaScript looks like -- the
# behavioural suite grades both.  These are the same assertions the verifier makes,
# in the same form, so a pin that moves in one image and not the other fails a
# build rather than producing a quiet difference.
RUN set -eux; \
    test "$(node --version)" = "v24.18.1" \
      || { echo "node is $(node --version), expected v24.18.1" >&2; exit 1; }; \
    test "$(tsc --version)" = "Version 5.9.3" \
      || { echo "tsc is $(tsc --version), expected Version 5.9.3" >&2; exit 1; }; \
    python3 -c 'import sys; assert sys.version_info[:2] == (3, 11), sys.version'; \
    git --version; \
    echo "toolchain pinned: node 24.18.1, tsc 5.9.3, python 3.11"

# --------------------------------------------------------------------------- #
# The agent account
# --------------------------------------------------------------------------- #
# uid 2000, a real home, and a login shell.  Not root: the workspace is the
# agent's and nothing else in the image is, so a tool that writes outside
# /workspace fails at the time it does it rather than silently changing the
# environment the grading assumptions were built on.
RUN set -eux; \
    groupadd --gid 2000 agent; \
    useradd --uid 2000 --gid 2000 --create-home --shell /bin/bash agent; \
    install -d -m 0755 -o agent -g agent /workspace

# --------------------------------------------------------------------------- #
# Stage `state-a` -- the tarball, verified, unpacked, its shape asserted
# --------------------------------------------------------------------------- #
# `COPY --chown=0:0` into a directory outside the workspace, so the archive and
# its digest are never in a place the agent can reach.  /run/state-a is deleted
# before the image is finished.
FROM toolchain AS state-a

COPY --chown=0:0 original.tar.gz /run/state-a/original.tar.gz
COPY --chown=0:0 original.sha256 /run/state-a/original.sha256
COPY --chown=0:0 source-contract.json /run/state-a/source-contract.json

# The digest is asserted from the `original.sha256` file beside the archive rather
# than inlined here, because the grading stages ship that same file and assert
# against it too -- three build contexts, one digest, and a context cannot reach
# outside itself to share a constant.
RUN set -eux; \
    cd /run/state-a; \
    sha256sum -c original.sha256; \
    install -d -m 0755 /workspace/repo; \
    tar -xzf original.tar.gz -C /workspace/repo --strip-components=1; \
    test -f /workspace/repo/package.json \
      || { echo "the archive did not unpack to the expected layout. It carries a \
single top-level \`repo/\` directory, which --strip-components=1 removes; if that \
changed, this is where it shows up." >&2; exit 1; }

# The tree is what the contract says it is, asserted at build time.  Three
# independent numbers -- the file count, the byte total of `src/`, and the absence
# of a tsconfig -- because each catches a different accident: a tarball rebuilt
# with an extra file, a tarball rebuilt from a different upstream, and a scaffold
# that appeared from an edit to the curated tree.  A submission graded against a
# contract that no longer describes its starting point is a broken task, and this
# is where that is cheapest to catch.
#
# `find -type f` rather than `git ls-files`: there is no git yet.
RUN set -eux; \
    cd /workspace/repo; \
    n="$(find . -type f | wc -l)"; \
    test "$n" = "15" \
      || { echo "State A holds $n files, the contract says 15:" >&2; \
           find . -type f | sort >&2; exit 1; }; \
    b="$(cat src/*.js | wc -c)"; \
    test "$b" = "358338" \
      || { echo "src/*.js is $b bytes, the contract says 358338" >&2; exit 1; }; \
    test ! -e tsconfig.json \
      || { echo "a tsconfig.json is present in State A. The contract says the \
submission authors that file from nothing, and a scaffold here would make \
`submission_must_author` false and hand over a starting point the task does not \
describe." >&2; exit 1; }; \
    test -z "$(find . \( -name '.git*' -o -name .hg -o -name .svn -o -name .bzr \
       -o -name _darcs -o -name CVS -o -name '*.pyc' \) -print -quit)" \
      || { echo "repository-management data survived into the workspace" >&2; \
           exit 1; }; \
    install -d -m 0755 /opt/swerefactor; \
    install -m 0444 /run/state-a/source-contract.json \
      /opt/swerefactor/source-contract.json; \
    echo "State A: 15 files, 358,338 bytes of src/, no tsconfig, no VCS data"

# --------------------------------------------------------------------------- #
# Prove the delivered environment does what the task tells the agent to do
# --------------------------------------------------------------------------- #
# The instruction says: build the original, run its probe, diff your port against
# it.  Both of those run here first, offline, so that a failure is my build's
# rather than the agent's third hour.  Then the build output is deleted and the
# file count re-asserted, because the workspace the agent receives has to be the
# source snapshot again.
#
# This is the part of this Dockerfile that tommy's could not have: State A is
# JavaScript and the runtime is already installed, so proving the original works
# costs one `npm run build` and no second toolchain.
RUN set -eux; \
    cd /workspace/repo; \
    npm run build > /tmp/build.log 2>&1 \
      || { tail -40 /tmp/build.log; exit 1; }; \
    test -f dist/probe.js; \
    test -f dist/jsonata.js; \
    test -f dist/jsonata.d.ts; \
    npm run selfcheck > /tmp/selfcheck.log 2>&1 \
      || { tail -40 /tmp/selfcheck.log; exit 1; }; \
    echo "State A builds and its probe self-check passes"

# The `hello` line, exact rather than a grep for `"ok":true`.  Its bytes are fixed
# by `opHello` (four fields, and each list sorted) inside the envelope
# `encodeResponse` writes (`id`, `ok`, `op`, `result`), with an encoder that emits
# no whitespace anywhere outside strings.  A port has to reproduce this line for
# id 1, so if this string is wrong then the task is wrong, and asserting the shape
# instead would hide that.
RUN set -eux; \
    cd /workspace/repo; \
    got="$(echo '{"id":1,"op":"hello"}' | node dist/probe.js)"; \
    want='{"id":1,"ok":true,"op":"hello","result":{"engines":["broken","caseless","native","nomatch"],"impls":["asyncDouble","concat2","counter","describe","double","focusInput","focusLookup","hostDate","hostMap","makeAdder","nothing","throwing","throwingCode"],"ops":["assign","ast","eval","evalcb","hello","register"],"protocol":"jsonata-probe/1"}}'; \
    test "$got" = "$want" \
      || { echo "the built probe answered hello with:"; echo "  $got"; \
           echo "expected:"; echo "  $want"; exit 1; }

# Two real evaluations through the same binary.  `hello` proves the driver and the
# encoder; it does not touch the engine.  These do, on the two things the contract
# leads with.
#
#   * The first is the language's own README example, and its answer is
#     `90.57000000000001` -- the sum of two products that a decimal calculator
#     would call 90.57.  That is JavaScript number formatting, which the port
#     inherits for free because it targets the same runtime, and it is asserted
#     here because it is the single most legible proof that the engine ran rather
#     than something echoing.
#   * The second is the pinned clock: `$now()` under `clock: "pinned"` must be
#     2023-11-14T22:13:20.000Z, the ISO rendering of 1700000000000.  A port that
#     implements `registerFunction` correctly inherits the pinning, and a port
#     that does not fails every clock case -- so proving the pin works here is
#     proving the corpus can be met at all.
#
# Whole-line comparisons, because both lines are fully determined: the response
# envelope, the value encoder's tags (`d` for a number, `s` for a string) and the
# `results` array are all in `probe-wire.js`, with no formatter of my own between.
RUN set -eux; \
    cd /workspace/repo; \
    got="$(echo '{"id":7,"op":"eval","expr":"$sum(Account.Order.Product.(Price*Quantity))","input":{"Account":{"Order":[{"Product":[{"Price":34.45,"Quantity":2},{"Price":21.67,"Quantity":1}]}]}}}' \
      | node dist/probe.js)"; \
    want='{"id":7,"ok":true,"op":"eval","result":{"results":[["d",90.57000000000001]]}}'; \
    test "$got" = "$want" \
      || { echo "the eval response was:"; echo "  $got"; \
           echo "expected:"; echo "  $want"; exit 1; }; \
    got="$(echo '{"id":8,"op":"eval","expr":"$now()","clock":"pinned"}' \
      | node dist/probe.js)"; \
    want='{"id":8,"ok":true,"op":"eval","result":{"results":[["s","2023-11-14T22:13:20.000Z"]]}}'; \
    test "$got" = "$want" \
      || { echo "the pinned clock answered:"; echo "  $got"; \
           echo "expected:"; echo "  $want"; exit 1; }; \
    echo "the engine evaluates and the clock is pinned"

# Prove the TypeScript side of the environment works before handing it over, on a
# throwaway tree outside the workspace.  The agent writes its own tsconfig.json,
# so what has to be true is narrower than "the scaffold compiles": tsc must be
# reachable without a node_modules, it must honour `strict` and `noEmitOnError`,
# it must emit declarations, and node must run what it emits.  All four are
# asserted, the `strict` one by compiling something that must fail.
#
# The compilerOptions below are not arbitrary: `target`, `module`, `rootDir`,
# `outDir`, the empty `types` and `declaration` are the values
# `source-contract.json`'s `tsconfig_contract` obliges the submission to use, so
# this proves the toolchain can build what the contract demands rather than some
# neighbouring configuration.  `module` in particular must stay `commonjs` --
# there is no package.json in this throwaway tree, and under `nodenext` the module
# system would be inferred from a file that is not there.  If the contract's
# required values change, change them here too.
RUN set -eux; \
    install -d /tmp/ts-probe/src; \
    cd /tmp/ts-probe; \
    printf '%s\n' '{"compilerOptions":{"target":"ES2022","module":"commonjs",' \
      '"strict":true,"noImplicitAny":true,"strictNullChecks":true,' \
      '"noEmitOnError":true,"declaration":true,' \
      '"types":[],"outDir":"dist","rootDir":"src"},"include":["src"]}' \
      > tsconfig.json; \
    printf '%s\n' 'export const n: number = 41 + 1;' \
      'declare const console: { log(s: string): void };' \
      'console.log("ts ok " + n);' > src/main.ts; \
    tsc -p tsconfig.json; \
    test -f dist/main.js; \
    test -f dist/main.d.ts \
      || { echo "declaration:true emitted no .d.ts, and the contract requires \
the submission's declarations to be generated rather than copied" >&2; exit 1; }; \
    node dist/main.js | grep -Fx 'ts ok 42'; \
    printf '%s\n' 'const bad: number = "not a number";' > src/main.ts; \
    if tsc -p tsconfig.json > /tmp/ts-fail.log 2>&1; then \
      echo "tsc accepted a string assigned to a number: strict is not in \
effect, and the submission would be graded under a compiler that does not \
enforce what the contract says it does" >&2; exit 1; \
    fi; \
    grep -q 'TS2322' /tmp/ts-fail.log \
      || { echo "tsc rejected the ill-typed file but not with TS2322:"; \
           cat /tmp/ts-fail.log; exit 1; }; \
    cd /; rm -rf /tmp/ts-probe /tmp/ts-fail.log

# Every artifact the proofs above produced, gone -- and the count re-asserted, so
# the cleanup is proved complete rather than assumed.  `dist/` is the one thing
# the build leaves behind, and it is exactly the thing that must not ship: a
# workspace handed over with a built `dist/` would let a submission pass `build`
# without ever compiling anything.
RUN set -eux; \
    cd /workspace/repo; \
    rm -rf dist; \
    rm -f /tmp/build.log /tmp/selfcheck.log; \
    rm -rf /run/state-a; \
    test ! -e /run/state-a; \
    test ! -e dist; \
    n="$(find . -type f | wc -l)"; \
    test "$n" = "15" \
      || { echo "the workspace holds $n files after cleanup, expected 15"; \
           find . -type f | sort; exit 1; }

# A single history-free baseline commit gives the agent a local diff surface.  The
# verifier never trusts this repository: it holds its own copy of State A and
# treats `.git` as untrusted agent-writable data.
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
      git commit -q -m 'State A: JSONata 2.2.2 (JavaScript implementation and probe)'; \
    test "$(git rev-list --count HEAD)" = "1"; \
    test -z "$(git status --porcelain)"; \
    git log --format=%H -1 > /opt/swerefactor/baseline-commit.txt; \
    chmod 0444 /opt/swerefactor/baseline-commit.txt; \
    chown -R agent:agent /workspace

# TZ and the locale are the two the corpus depends on; see the toolchain stage.
# `npm_config_offline` is set here as well as in the verifier: there is no registry
# in either image, and a build that tries to reach one should fail immediately with
# a clear message rather than after a DNS timeout.
ENV NODE_OPTIONS=--max-old-space-size=2048 \
    npm_config_offline=true \
    npm_config_update_notifier=false \
    npm_config_fund=false \
    npm_config_audit=false \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=UTC \
    LC_ALL=C.UTF-8 \
    LANG=C.UTF-8

WORKDIR /workspace/repo
USER agent

