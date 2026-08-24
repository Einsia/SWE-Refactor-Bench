# =============================================================================
# SWERefactorBench -- lang04-acorn-js-to-rust
# Stage 2 image: the behavioural suite.
#
#     docker build -t swerefactor/lang04-behavioural:1 tasks/lang04-.../tests/behavioural
#
# Built from the same digest-pinned base, the same Debian snapshot and the same
# node tarball as the agent environment, so a port that builds there builds here
# for the same reasons.
#
# Four properties of this file are load-bearing.
#
#  1. It carries its own copy of original.tar.gz and never reads the submitted
#     tree to learn what State A was.  The baseline is how the suite tells an
#     unchanged file from a rewritten one, so it cannot come from the party being
#     graded.  The digest below is the one environment/original.sha256 records; if
#     the two copies ever diverge, one of the two builds fails on its checksum
#     instead of grading against the wrong baseline.
#
#  2. freeze.py runs here, at build time.  It builds acorn 8.14.0's own `dist/`
#     with the pinned rollup, runs upstream's suite against the result, generates
#     the corpus from a recorded seed, and answers every case into files that are
#     then digested.  By the time a submission exists the answers are already
#     fixed; grading only compares against them.  Computing expectations at
#     grading time would put the submission in the container while the answers
#     were being decided.
#
#  3. node survives into the grading stage.  This is the opposite of the sibling
#     Go-to-Zig task, which deletes its source toolchain, and the reason is the
#     CLI differential: the `cli` module runs `acorn` on both sides, argv for
#     argv, and the reference side of that comparison is a node script.  The
#     defence against a submission reaching node is not absence but build.py's
#     PATH tripwire plus the interpreter gates -- and, during the CLI module, the
#     submission is invoked as its own installed binary while node is reachable
#     only at the absolute path the driver resolved for itself.
#
#  4. What does *not* survive is the bundler.  rollup, buble and npm exist only to
#     produce the reference, and grading has no use for any of them.  They are
#     deleted and the reference is then asked to answer, after the deletion, in
#     the layer that made it -- because "static enough to survive" is a claim
#     about 16,000 cases and is cheaper to test than to argue.
#
# At runtime the harness provides:
#
#     /workspace/repo    the submission                          (read-write copy)
#     /opt/original      State A, unpacked from original.tar.gz       (read-only)
#     /logs/verifier     where the stage result JSON is written
#
# The suite reads its baseline from the frozen assets, not from /opt/original --
# that mount is there for the stages that compare trees.  Network: none.  cargo is
# configured offline and the build has no registry to reach.
# =============================================================================

ARG INFRA_IMAGE=swerefactor/infra:1
FROM ${INFRA_IMAGE} AS infra

FROM rust@sha256:3914072ca0c3b8aad871db9169a651ccfce30cf58303e5d6f2db16d1d8a7e58f AS toolchain

ARG DEBIAN_FRONTEND=noninteractive
ARG DEBIAN_SNAPSHOT=20251020T000000Z
ARG NODE_VERSION=22.20.0
ARG NODE_SHA256=00bbd05e306ea68b6e13e17360d0e2f680b493ef95f2fea1c4296ff7437530bc

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# The same frozen snapshot as the agent environment, so both images resolve to one
# set of bytes for as long as snapshot.debian.org keeps that timestamp.
# Check-Valid-Until has to be off: a snapshot's Release file is, by construction,
# always expired.  Written as the base image's own debian.sources rather than
# beside it -- a second file would be *added* to the live archive, not replace it,
# and these pins would then resolve against whatever deb.debian.org holds today.
RUN set -eux; \
    test "$RUST_VERSION" = "1.90.0"; \
    rm -f /etc/apt/sources.list; \
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
    apt-get -o Acquire::Retries=8 -qq update; \
    apt-get -qq install -y --no-install-recommends \
      ca-certificates=20230311+deb12u1 \
      file=1:5.44-3 \
      make=4.3-4.1 \
      python3=3.11.2-1+b1 \
      xz-utils=5.4.1-1; \
    rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*.deb /var/cache/apt/*.bin

# node from the official dist tarball rather than the distro: the distro's nodejs
# moves within a snapshot's lifetime, and this interpreter's behaviour is the thing
# being frozen.  Same version and digest as the agent environment.
RUN set -eux; \
    cd /tmp; \
    curl -fsSLO "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz"; \
    echo "${NODE_SHA256}  node-v${NODE_VERSION}-linux-x64.tar.xz" | sha256sum -c -; \
    tar -xJf "node-v${NODE_VERSION}-linux-x64.tar.xz" -C /usr/local --strip-components=1 \
      --exclude='*/CHANGELOG.md' --exclude='*/README.md' --exclude='*/LICENSE'; \
    rm -f "node-v${NODE_VERSION}-linux-x64.tar.xz"

# The toolchain the contract names.  grep -Fx so a version that merely contains
# the string does not pass.  cargo and rustc are asserted because build.py
# compiles the submission with them and the provenance module reads the .comment
# string they leave in the ELF; a mismatch would fail every submission for the
# image's reason rather than its own.
RUN set -eux; \
    rustc --version | cut -d' ' -f2 | grep -Fx '1.90.0'; \
    cargo --version | cut -d' ' -f2 | grep -Fx '1.90.0'; \
    node --version | grep -Fx "v${NODE_VERSION}"; \
    python3 --version | cut -d' ' -f2 | grep -Fx '3.11.2'; \
    readelf --version | head -1

# cargo has to work offline, because build.py builds the submission with
# CARGO_NET_OFFLINE=true and an empty registry.  Proven here, on a throwaway
# crate, so a broken toolchain is an image failure and not twelve modules of
# zeroes.
RUN set -eux; \
    install -d -m 0755 "$CARGO_HOME"; \
    printf '%s\n' \
      '[net]' \
      'offline = true' \
      '' \
      '[build]' \
      'incremental = false' \
      > "$CARGO_HOME/config.toml"; \
    chmod 0644 "$CARGO_HOME/config.toml"; \
    install -d /tmp/rustproof/src; \
    printf '[package]\nname="p"\nversion="0.0.0"\nedition="2021"\n' > /tmp/rustproof/Cargo.toml; \
    printf 'fn main(){println!("ok");}\n' > /tmp/rustproof/src/main.rs; \
    cd /tmp/rustproof; \
    cargo build --release --offline -q; \
    ./target/release/p | grep -Fx ok; \
    cd /; rm -rf /tmp/rustproof

# ---------------------------------------------------------------------------
# Baseline: the suite's own authenticated copy of State A, and the bundler that
# builds the reference from it.  Neither reaches the final image.
#
# Size as well as digest: a truncated download that is still a valid gzip is a
# real failure mode, and it reports as a short file count rather than as a corrupt
# archive.
# ---------------------------------------------------------------------------
FROM toolchain AS baseline

COPY --chown=0:0 data/original.tar.gz /run/baseline/original.tar.gz

RUN set -eux; \
    echo '11ba84c50c3ef2d63cfd665e7128f6c78d5cdc6d2bd4a0b3127fe8f34a0f019e  /run/baseline/original.tar.gz' \
      | sha256sum -c -; \
    test "$(stat -c '%s' /run/baseline/original.tar.gz)" = "1190550"; \
    install -d -m 0755 /run/unpack; \
    tar --extract --file=/run/baseline/original.tar.gz --directory=/run/unpack \
        --no-same-owner --no-same-permissions; \
    test -f /run/unpack/repo/package.json; \
    test "$(find /run/unpack/repo -type f | wc -l)" = "116"; \
    test -z "$(find /run/unpack/repo \( -type l -o -type b -o -type c -o -type p -o -type s \) -print -quit)"; \
    test ! -e /run/unpack/repo/.git

# rollup and its buble plugin, locked by integrity hash, in a stage that does not
# reach the final image.  This is the same install the agent environment performs
# at /workspace/node_modules; here it exists only so freeze.py can build the
# reference's `dist/`, and it is deleted once that is done.
COPY --chown=0:0 data/buildtools-package.json      /run/buildtools/package.json
COPY --chown=0:0 data/buildtools-package-lock.json /run/buildtools/package-lock.json

RUN set -eux; \
    cd /run/buildtools; \
    npm ci --no-audit --no-fund --ignore-scripts; \
    test -f /run/buildtools/node_modules/rollup/dist/bin/rollup; \
    test -d /run/buildtools/node_modules/buble; \
    node -p "require('/run/buildtools/node_modules/rollup/package.json').version" \
      | grep -Fx '3.29.4'; \
    rm -rf /root/.npm

# ---------------------------------------------------------------------------
# Freeze: build the reference, generate the corpus, record every expectation.
#
# freeze.py stages its own fixed inputs (the baseline tree, the reference
# responder, the interpreter shim, the source contract) into /opt/assets, so the
# whole grading input set is one directory that can be sealed in one step.  It
# lives beside /opt/swerefactor rather than inside it: the harness there is shared by
# every task and arrives from the donor image, and one directory should not be half
# vendored code and half this task's answers.
#
# py_compile before freeze.py runs.  A syntax error in a module only the grading
# path imports -- structure.py's deep checks, elflib.py's ELF reader -- would
# otherwise surface as a failed submission months later, in a container nobody is
# watching.  The list is found, not written out: a hand-kept enumeration of "every
# shipped module" drifts the moment a file is added.  The count is echoed rather
# than pinned, because pinning it just moves the drift one line down -- but zero
# files must not pass silently.
# ---------------------------------------------------------------------------
FROM baseline AS frozen

COPY --chown=0:0 . /tests/behavioural

# reference.js exists twice in this task -- here and in tests/verification/lib --
# because stage 3 must answer the protocol the same way stage 2 measures it, and
# the two stages are separate build contexts.  Two copies of one file is the shape
# that drifts silently, so both Dockerfiles assert the same digest: edit either
# copy and both builds fail, naming their own file.  Without this, a change here
# would fail stage 3's build and blame stage 3's copy.
RUN set -eux; \
    echo 'f7c5f5fc00b1805a67aa65f0429187e35f8a4c53b9cfab9bd4c4727879bb762e  /tests/behavioural/lib/reference.js' \
      | sha256sum -c -

# py_compile before freeze.py runs.  A syntax error in a module that only the
# grading path imports would otherwise survive the image build and surface as a
# crashed run against a real submission.
#
# The sweep is over /tests/behavioural, not /tests/behavioural/lib: selftest.py sits
# at the top level and nothing else in this build imports it, so a lib-only sweep
# left the one file whose errors surface latest as the one file never checked.
RUN set -eux; \
    rm -f /tests/behavioural/data/original.tar.gz /tests/behavioural/Dockerfile \
          /tests/behavioural/.dockerignore; \
    find /tests/behavioural -name '*.py' -print0 > /tmp/pyfiles; \
    test -s /tmp/pyfiles; \
    echo "py_compile: $(tr -cd '\0' < /tmp/pyfiles | wc -c) modules"; \
    xargs -0r /usr/bin/python3 -m py_compile < /tmp/pyfiles; \
    rm -f /tmp/pyfiles; \
    /usr/bin/python3 -I /tests/behavioural/lib/freeze.py \
      --baseline /run/unpack/repo \
      --archive /run/baseline/original.tar.gz \
      --buildtools /run/buildtools \
      --assets /opt/assets \
      --node /usr/local/bin/node \
      --tests /tests/behavioural/lib \
      --data /tests/behavioural/data \
      --seed 20260730 \
      --generated 700 \
      --image lang04-acorn-js-to-rust-behavioural; \
    find /tests/behavioural -name __pycache__ -type d -prune -exec rm -rf {} +

# reference.js resolves the repository it answers from `ACORN_REFERENCE_REPO`, and
# falls back to /opt/swerefactor/reference.  The fallback is the path that matters:
# the identity run invokes the reference through `vlib.base_env()`, which builds a
# fresh environment from a fixed dict and carries no such variable.  A symlink is
# enough, and it keeps the assets tree the single copy.
RUN set -eux; \
    install -d -m 0755 /opt/swerefactor; \
    ln -s /opt/assets/reference /opt/swerefactor/reference; \
    test -f /opt/swerefactor/reference/acorn/dist/acorn.js

# The freeze is over, so the bundler goes.  rollup and buble built `dist/`; from
# here on the reference is three pre-built UMD bundles and nothing needs to produce
# them again.  npm goes with it -- the submission is built with `make` and `cargo`,
# so npm has no role at grading time, and it is one of the names build.py shims:
# removing it means an absolute-path invocation fails outright instead of being
# merely recorded.
#
# Inside the reference's own node_modules, three symlinks survive and the rest does
# not.  Those three are load-bearing at *run* time, not build time: the rollup
# configs mark `acorn` external, so `acorn-loose/dist/acorn-loose.js` requires it
# by bare name and node resolves that by walking up to this directory.  Deleting
# them would break every loose-parse case.  So the deletion is by type --
# directories and regular files go, symlinks stay -- rather than by a glob that
# cannot tell the difference.
#
# `npm ci`'s inputs go too.  Nothing after this line reads /run.  Leaving them
# would ship a second copy of the baseline beside the one the suite actually reads,
# which is what .dockerignore in this directory exists to prevent -- exactly one of
# each.  This buys reachability, not bytes: both trees were created in a lower
# stage of the same chain, so `rm` cannot shrink the image, only make them
# unfindable from the final one.
RUN set -eux; \
    find /opt/assets/reference/node_modules -mindepth 1 -maxdepth 1 \
      \( -type d -o -type f \) -exec rm -rf {} +; \
    rm -rf /run/buildtools /run/unpack /run/baseline \
      /usr/local/lib/node_modules/npm /usr/local/bin/npm /usr/local/bin/npx \
      /usr/local/bin/corepack /root/.npm; \
    for name in acorn acorn-loose acorn-walk; do \
      test -L "/opt/assets/reference/node_modules/${name}"; \
    done; \
    test ! -e /opt/assets/reference/node_modules/rollup; \
    test ! -e /run/baseline; \
    test ! -e /run/unpack; \
    test ! -e /run/buildtools; \
    for tool in npm npx yarn pnpm corepack deno bun; do \
      if command -v "${tool}" >/dev/null 2>&1; then \
        echo "FATAL: ${tool} is still reachable at $(command -v "${tool}")" >&2; \
        exit 1; \
      fi; \
    done; \
    node --version | grep -Fx "v22.20.0"; \
    echo "bundler and freeze inputs gone; node retained for the CLI differential"

# The reference must still answer with the bundler gone.  Both halves of it, in
# this layer, after the deletion: the NDJSON probe that decides all but a few dozen
# of the graded cases, and the CLI that decides the rest.  `version` is the
# cheapest op that proves the whole require chain resolved -- it reads
# `acorn.version` out of a bundle rollup emitted in a stage that no longer exists.
# The loose parse is the one that proves the surviving symlinks work, because
# `acorn-loose` reaches `acorn` by bare name through exactly those links.
RUN set -eux; \
    printf '%s\n' \
      '{"id":1,"op":"version"}' \
      '{"id":2,"op":"loose_parse","source":"let x = (","options":{"ecmaVersion":2020}}' \
      | /usr/local/bin/node /opt/assets/reference.js > /tmp/probe.ndjson; \
    grep -qF '{"id":1,"ok":true,"result":"8.14.0"}' /tmp/probe.ndjson; \
    grep -qF '"id":2,"ok":true' /tmp/probe.ndjson; \
    test "$(wc -l < /tmp/probe.ndjson)" = "2"; \
    printf 'let x = 1\n' \
      | /usr/local/bin/node /opt/assets/reference/acorn/bin/acorn --ecma2020 | head -3; \
    rm -f /tmp/probe.ndjson; \
    echo "the reference answers without a bundler, on both protocols"

# What the freeze produced, read back rather than restated.  manifest_check.py holds
# the floors and imports them from catalog.py, and it now checks that every digested
# input is on disk -- so the files listed here are the other kind: the assets that
# ship *without* a digest, which nothing else would notice the absence of until a
# module tried to open one.
#
# When `excluded.json` joined the freeze, the copy here did not fail -- a
# hand-written list cannot miss a file it does not know about -- so the list is
# imported where it is needed and this layer keeps only what the inventory does not
# cover.
RUN set -eux; \
    /usr/bin/python3 -I /tests/behavioural/lib/manifest_check.py \
      --manifest /opt/assets/verifier-manifest.json \
      --assets /opt/assets; \
    for f in reference.js shim/jsshim.py \
             reference/acorn/dist/acorn.js reference/acorn/bin/acorn; do \
      test -f "/opt/assets/${f}" || { echo "missing asset: ${f}" >&2; exit 1; }; \
    done; \
    test -d /opt/assets/baseline; \
    test -d /opt/assets/reference/test/bench/fixtures; \
    test -x /opt/assets/reference/acorn/bin/acorn

# --- The harness -------------------------------------------------------------
# The module runner, the result contract and the scorer, shared by every task.
COPY --from=infra /opt/swerefactor /opt/swerefactor

# --- The manifest, checked at build time -------------------------------------
# suite.toml is the file most likely to be edited and least likely to be tested,
# and driver.py's family map is what decides whether a case is graded at all.  Both
# are checked here: a duplicate id, a weight that does not parse, a module directory
# that does not exist, a case family no module claims, or a guard claimed by two
# stages fails the build rather than the run.
#
# suite.toml's `[metadata] cases` is checked here too, against the manifest rather
# than against a number typed beside it.  It names the recorded population -- what
# identity is asked -- so the sum it must agree with is
# `graded_total + unportable_cases`, the same one the identity layer checks its own
# count against.  Checked here because this is the layer that can: a file cannot
# enforce a claim about another file.
RUN set -eux; \
    chmod +x /tests/behavioural/lib/run-module.sh; \
    for d in /tests/behavioural/modules/*/; do \
      test -e "$d/run.sh" || { echo "no runner in $d" >&2; exit 1; }; \
    done; \
    PYTHONPATH=/opt/swerefactor /usr/bin/python3 -c "\
import json, pathlib, sys; from swerefactor import config; \
s = config.Suite.load('/tests/behavioural/suite.toml'); \
missing = [m.id for m in s.modules if not (s.root / (m.dir or f'modules/{m.id}')).is_dir()]; \
sys.exit(f'suite.toml names missing module dir(s): {missing}') if missing else None; \
counts = json.loads(pathlib.Path('/opt/assets/verifier-manifest.json').read_text())['counts']; \
recorded = counts['graded_total'] + counts['unportable_cases']; \
declared = s.metadata.get('cases'); \
sys.exit(f'suite.toml declares cases={declared!r} and the manifest records ' \
         f'{recorded} ({counts[\"graded_total\"]} graded plus ' \
         f'{counts[\"unportable_cases\"]} unportable); one of the two is wrong') \
    if declared != recorded else None; \
print(f'suite: {len(s.modules)} modules, weight {s.weight_total:g}, ' \
      f'cases {declared} agreeing with the manifest')"; \
    /usr/bin/python3 -I /tests/behavioural/lib/driver.py --self-check --log /tmp/self-check; \
    rm -rf /tmp/self-check

# The self-check above validates the applicability tables and the unportable-case
# list; this asks whether that validation detects.  Deliberate breakages -- a case
# moved between the keep and skip tables, a case the catalog renamed underneath them,
# a reason blanked to whitespace, the Rust branch dropped, an excluded case orphaned
# into a family no module claims, the exclusion list dropped on the way into
# run_corpus -- must each be reported.  Mostly table logic: nothing is built and
# nothing is copied.  The last two run a four-case synthetic corpus against a probe
# that answers wrongly, which costs a fraction of a second.
#
# In the build rather than left to an operator because the tables it guards are what
# let a pre-migration tree score 40.00/40.  An error there does not lose a point, it
# changes which questions were asked, and the report still reads as full marks.
RUN set -eux; \
    /usr/bin/python3 -I /tests/behavioural/lib/applicability_mutants.py

# selftest.py is deliberately *not* run here, and the reason is cost rather than
# confidence: it builds State A with node and grades all 15,975 corpus cases, which
# is the identity run's work again with a real build in front of it.  The identity
# run below already proves the corpus is answerable by the reference; what selftest
# adds is that the *scoring* lands on 40.00/40 through the shipped builder, which is
# an operator's question about this stage rather than a property of the image.  Run
# it with:
#
#   docker run --rm -v /tmp/out:/out swerefactor/lang04-behavioural:4 \
#     python3 /tests/behavioural/selftest.py --report /out/selftest.json

# --- The identity run, in the image, before the image is allowed to exist -----
#
# Every module an identity run can cover, with the JavaScript reference standing in
# for the submission's probe.  The expectations were frozen from that reference an
# hour ago in a lower stage, so every case must pass; a single failure means the
# corpus disagrees with its own answers, or the framing lost a response, or the
# comparison is wrong.  It is the strongest check available here, because it
# exercises what a manifest digest cannot: probe.py's NDJSON framing, the fixture
# block reader, the CLI runner's argv and stdin handling, and the comparison
# itself.
#
# Including the cases no port can be asked, which is why the total below is
# `graded_total + unportable_cases` rather than `graded_total`.  A graded run skips
# those 31, because their expectation is the reference crashing and the message the
# schema demands is V8's wording for the shape of the code that crashed.  An
# identity run must not: the reference *is* that code, so it is the only probe that
# can answer them, and if it did not they would be the only expectations in the
# corpus nothing ever checks.  A corrupted one among them would then first surface
# as a submission being told it got a crash wrong.
#
# The module list comes from `--list-identity` rather than from a list written into
# this shell: a module added to the driver and not to a hand-written loop would be
# absent from the identity run with nothing to say so.  The three modules it cannot
# cover need a submission to build, and the driver refuses them by name.
#
# The per-module results are then read as one: total, failures, and the count of
# modules that reported.  `set -o pipefail` is on, so a module that dies mid-run
# fails this layer rather than contributing a short result nobody counts.
RUN set -eux; \
    install -d -m 0755 /tmp/identity/logs /tmp/identity/work; \
    /usr/bin/python3 -I /tests/behavioural/lib/driver.py --list-identity \
      > /tmp/identity/modules.txt; \
    test "$(wc -l < /tmp/identity/modules.txt)" -ge 10; \
    while read -r m; do \
      echo "identity: ${m}"; \
      SRB_SHIM=off /usr/bin/python3 -I /tests/behavioural/lib/driver.py \
        --identity --module "${m}" \
        --assets /opt/assets \
        --work /tmp/identity/work \
        --log /tmp/identity/logs \
        --result "/tmp/identity/logs/${m}.json"; \
    done < /tmp/identity/modules.txt; \
    /usr/bin/python3 -I -c "\
import json, pathlib, sys; \
logs = pathlib.Path('/tmp/identity/logs'); \
want = pathlib.Path('/tmp/identity/modules.txt').read_text().split(); \
files = [logs / f'{m}.json' for m in want]; \
missing = [f.name for f in files if not f.is_file()]; \
sys.exit(f'identity produced no result for: {missing}') if missing else None; \
runs = [json.loads(f.read_text()) for f in files]; \
checks = [c for r in runs for c in r['checks']]; \
bad = [c for c in checks if c['verdict'] != 'pass']; \
print('identity:', len(checks), 'checks over', len(runs), 'modules'); \
sys.exit('the frozen corpus disagrees with the reference it was frozen from: ' \
         + '; '.join(f\"{c['id']}: {c.get('summary','')[:100]}\" for c in bad[:8])) \
  if bad else None; \
counts = json.loads(pathlib.Path('/opt/assets/verifier-manifest.json') \
                    .read_text())['counts']; \
want_n = counts['graded_total'] + counts['unportable_cases']; \
sys.exit(f'identity ran {len(checks)} checks and the manifest counts ' \
         f'{want_n} cases ({counts[\"graded_total\"]} graded plus ' \
         f'{counts[\"unportable_cases\"]} with no portable answer); ' \
         f'one of the two is wrong') \
  if len(checks) != want_n else None; \
print('the frozen corpus agrees with the reference it was frozen from,', \
      'over every case including the', counts['unportable_cases'], \
      'no port can be asked')"; \
    rm -rf /tmp/identity

# The frozen expectations are read, never written -- freeze.py is the only thing
# that writes there and it has already run.  Locking the tree means a bug in the
# grading path fails loudly instead of quietly editing the answers it grades
# against.  Directories keep +x so they stay traversable.
#
# After the identity run, so that run graded the tree as frozen rather than a tree
# this line had just altered.
RUN set -eux; \
    chmod -R a-w /opt/assets; \
    find /opt/assets -type d -exec chmod a+rx {} +; \
    test "$(stat -c '%a' /opt/assets/verifier-manifest.json)" = "444"; \
    find /tests/behavioural -name __pycache__ -type d -prune -exec rm -rf {} +; \
    test -z "$(find /tests/behavioural -name '*.pyc' -print -quit)"

ENV CARGO_NET_OFFLINE=true \
    CARGO_TERM_COLOR=never \
    RUST_BACKTRACE=1 \
    PYTHONHASHSEED=0 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=UTC \
    LC_ALL=C.UTF-8 \
    LANG=C.UTF-8 \
    PYTHONPATH=/opt/swerefactor \
    SWEREFACTOR_ASSETS=/opt/assets \
    SWEREFACTOR_REPO=/workspace/repo \
    SWEREFACTOR_LOG_DIR=/logs/verifier

RUN mkdir -p /opt/original /workspace /logs/verifier

WORKDIR /tests/behavioural
