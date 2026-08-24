# =============================================================================
# SWERefactorBench -- lang01-cmark-c-to-rust
# Stage 2 image: the behavioural suite.
#
#     docker build -t swerefactor/lang01-behavioural:1 tasks/lang01-.../tests/behavioural
#
# Built from the same digest-pinned base and the same Debian snapshot as the
# agent environment, so a submission that builds there builds here for the same
# reasons.
#
# Three properties of this file are load-bearing:
#
#  1. It carries its own copy of original.tar.gz and never reads the submitted
#     tree to learn what State A was.  The baseline is how the suite tells an
#     unchanged file from a rewritten one, so it cannot come from the party being
#     graded.  The digest below is the same one environment/original.sha256
#     records; if the two copies ever diverge, one of the two builds fails on its
#     checksum instead of grading against the wrong baseline.
#
#  2. freeze.py runs here, at build time.  It builds the pinned upstream release
#     from source, generates the corpus, and records the expected output of every
#     case into an immutable blob.  By the time a submission exists the answers
#     are already fixed and digested; grading only compares against them.
#     Computing expectations at grading time would put the submission in the
#     container while the answers were being decided.
#
#  3. Nothing here trusts a build directory the agent left behind.  The `build`
#     module configures both link configurations from the submitted source, under
#     a shimmed C compiler, into scratch prefixes of its own.
#
# At runtime the harness provides:
#
#     /workspace/repo    the submission                       (read-write copy)
#     /opt/original      State A, unpacked from original.tar.gz    (read-only)
#     /logs/verifier     where the stage result JSON is written
#
# The suite reads its baseline from the frozen assets, not from /opt/original --
# that mount is there for the stages that compare trees.  Network: none.  cargo
# is configured offline and the build has no registry to reach.
# =============================================================================

ARG INFRA_IMAGE=swerefactor/infra:1
FROM ${INFRA_IMAGE} AS infra

FROM rust@sha256:3914072ca0c3b8aad871db9169a651ccfce30cf58303e5d6f2db16d1d8a7e58f AS toolchain

ARG DEBIAN_FRONTEND=noninteractive
ARG DEBIAN_SNAPSHOT=20251020T000000Z

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# The same closure as the environment image, plus what grading itself needs:
# pkg-config to exercise the installed libcmark.pc the way a downstream package
# would, and binutils for the ELF and archive inspection the structural and
# provenance modules do.  binutils is present in the base image; it is named here
# so a future rebuild that loses it fails at build time instead of at grading
# time.
RUN set -eux; \
    test "$RUST_VERSION" = "1.90.0"; \
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
      cmake=3.25.1-1 \
      cmake-data=3.25.1-1 \
      libarchive13=3.6.2-1+deb12u3 \
      libjsoncpp25=1.9.5-4 \
      librhash0=1.4.3-3 \
      libuv1=1.44.2-1+deb12u1 \
      ninja-build=1.11.1-2~deb12u1 \
      pkg-config=1.8.1-1; \
    rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*.deb /var/cache/apt/*.bin; \
    cmake --version | head -1 | grep -Fx 'cmake version 3.25.1'; \
    pkg-config --version; \
    readelf --version | head -1; \
    ar --version | head -1; \
    rustc --version | grep -Fx 'rustc 1.90.0 (1159e78c4 2025-09-14)'

RUN set -eux; \
    install -d -m 0755 "$CARGO_HOME"; \
    printf '%s\n' \
      '[net]' \
      'offline = true' \
      '' \
      '[build]' \
      'incremental = false' \
      > "$CARGO_HOME/config.toml"; \
    chmod 0644 "$CARGO_HOME/config.toml"

# ---------------------------------------------------------------------------
# Baseline: the suite's own authenticated copy of State A.
# ---------------------------------------------------------------------------
FROM toolchain AS baseline

COPY --chown=0:0 data/original.tar.gz /run/baseline/original.tar.gz
COPY --chown=0:0 data/source-contract.json /run/baseline/source-contract.json

RUN set -eux; \
    echo '4a5466fa7ef20d7007b58fb217e7e7428201be6564f83c7bf76c17e004863a6c  /run/baseline/original.tar.gz' \
      | sha256sum -c -; \
    echo '2ab0c887bc09294c0eb36d42839f16e99f6eba6a8a7c8192a465a2eaf40204d5  /run/baseline/source-contract.json' \
      | sha256sum -c -; \
    test "$(stat -c '%s' /run/baseline/original.tar.gz)" = "257372"; \
    install -d -m 0755 /run/unpack; \
    tar --extract --file=/run/baseline/original.tar.gz --directory=/run/unpack \
        --no-same-owner --no-same-permissions; \
    test -f /run/unpack/repo/CMakeLists.txt; \
    test "$(find /run/unpack/repo -type f | wc -l)" = "113"

# ---------------------------------------------------------------------------
# Freeze: build the reference, generate the corpus, record the expectations.
#
# freeze.py stages its own fixed inputs (the baseline tree, the probe source, the
# compiler shim, the source contract) into /opt/assets, so the whole grading
# input set is one directory that can be sealed in one step.  It lives beside
# /opt/swerefactor rather than inside it: the harness there is shared by every task
# and arrives from the donor image, and one directory should not be half vendored
# code and half this task's answers.
# ---------------------------------------------------------------------------
FROM baseline AS frozen

COPY --chown=0:0 . /tests/behavioural

RUN set -eux; \
    rm -f /tests/behavioural/data/original.tar.gz /tests/behavioural/Dockerfile \
          /tests/behavioural/.dockerignore; \
    find /tests/behavioural/lib -name '*.py' -print0 \
      | xargs -0 /usr/bin/python3 -m py_compile; \
    /usr/bin/python3 -I /tests/behavioural/lib/freeze.py \
      --baseline /run/unpack/repo \
      --assets /opt/assets \
      --tests /tests/behavioural/lib \
      --data /tests/behavioural/data \
      --workspace /tmp/freeze \
      --image lang01-cmark-c-to-rust-behavioural; \
    rm -rf /tmp/freeze /run/unpack /run/baseline; \
    find /tests/behavioural -name __pycache__ -type d -prune -exec rm -rf {} +

# The frozen assets are the answers; nothing that runs later has any business
# writing to them.  Grading runs as root because it has to install a submission
# into a prefix and load shared objects, so read-only bits are a guard rail
# rather than a boundary -- but they do mean an accidental write fails loudly.
#
# The case floors are re-asserted against the manifest that actually shipped.
# catalog.py enforces them when it builds, but this is the copy being graded
# with, and a promise of "at least 800 cases" is worth checking on the artifact.
#
# State A must pass `build/parallel-safe`, asserted here because nothing else in
# the ladder asks it.  The identity run below grades the behavioural suite only,
# and stage 2 never builds State A at all, so a struct check State A cannot
# answer would ship looking correct: the reference would never be measured
# against it and the submission failing it would read as the submission's defect.
# That is not hypothetical for this check -- it reads generated makefiles, and a
# regex that happened to match cmake's C layout would have failed every tree.
#
# Only this one check, not the struct family.  Most of that family is Rust-shaped
# by design and State A is meant to fail it; requiring a clean sweep here would
# assert the opposite of what the port is for.  Build-graph hygiene is the part
# that is language-neutral, so it is the part State A can be held to.
#
# Calls the shipped staticmethod against the frozen reference build trees.  Free:
# the makefiles are already on disk from freeze.py, so there is nothing to build.
RUN set -eux; \
    /usr/bin/python3 -I /tests/behavioural/lib/manifest_check.py \
      --manifest /opt/assets/verifier-manifest.json; \
    /usr/bin/python3 -I -c "\
import sys; sys.path.insert(0, '/tests/behavioural/lib'); \
from pathlib import Path; from structure import StructureEvaluator; \
beds = [Path('/opt/assets/reference') / d \
        for d in ('ref-build-shared', 'ref-build-static')]; \
missing = [str(b) for b in beds if not (b / 'CMakeFiles/Makefile2').is_file()]; \
sys.exit(f'no generated makefiles under {missing}') if missing else None; \
bad = {str(b): StructureEvaluator.parallel_clashes(b) for b in beds}; \
bad = {k: v for k, v in bad.items() if v}; \
sys.exit('State A fails build/parallel-safe, so the check is wrong rather than \
the submission: ' + repr(bad)) if bad else None; \
print(f'build/parallel-safe: State A clean on {len(beds)} reference build(s)')"; \
    chmod -R a-w /opt/assets; \
    test "$(stat -c '%a' /opt/assets/catalog.json)" = "444"; \
    test -x /opt/assets/reference/ref-install-shared/bin/cmark; \
    LD_LIBRARY_PATH=/opt/assets/reference/ref-install-shared/lib \
      /opt/assets/reference/ref-install-shared/bin/cmark --version \
      | grep -F 'cmark 0.31.1'

# --- The harness -------------------------------------------------------------
# The module runner, the result contract and the scorer, shared by every task.
COPY --from=infra /opt/swerefactor /opt/swerefactor

# --- The manifest, checked at build time -------------------------------------
# suite.toml is the file most likely to be edited and least likely to be tested,
# and the family map in driver.py is the thing that decides whether a case is
# graded at all.  Both are checked here: a duplicate id, a weight that does not
# parse, a module directory that does not exist, or a case family no module
# claims fails the build rather than the run.
RUN set -eux; \
    chmod +x /tests/behavioural/lib/run-module.sh; \
    for d in /tests/behavioural/modules/*/; do \
      test -e "$d/run.sh" || { echo "no runner in $d" >&2; exit 1; }; \
    done; \
    PYTHONPATH=/opt/swerefactor /usr/bin/python3 -c "\
import sys; from swerefactor import config; \
s = config.Suite.load('/tests/behavioural/suite.toml'); \
missing = [m.id for m in s.modules if not (s.root / (m.dir or f'modules/{m.id}')).is_dir()]; \
sys.exit(f'suite.toml names missing module dir(s): {missing}') if missing else None; \
print(f'suite: {len(s.modules)} modules, weight {s.weight_total:g}')"; \
    /usr/bin/python3 -I /tests/behavioural/lib/driver.py --self-check

# --- The identity run, at build time -----------------------------------------
# Grade the reference against its own frozen expectations and require a clean
# sweep: every behavioural module at weight-rate 1.0, and `provenance` failing
# nothing except the weight-0 toolchain gates that a C tree is *meant* to fail.
# mutants.py owns those assertions -- imported from driver.py rather than
# restated, so a gate quietly dropped from the charge cannot be quietly dropped
# from the check.
#
# This is the only thing in the ladder that can catch a suite its own reference
# cannot pass.  Stage 3 cannot: it never executes these cases -- it pays six
# models to write new ones -- and it only runs when stage 2 takes full marks.
# So in precisely the failure this catches, the ladder stops at stage 2 and
# nothing downstream is reached.
#
# The baseline is the suite's own authenticated copy, never the submitted tree --
# the same property the header insists on.  It is staged writable first because
# /opt/assets is sealed read-only and `build` configures and installs from the
# tree it is handed, exactly as it will at grading time.  ~20s, and the scratch
# copy is gone before the layer closes.
RUN set -eux; \
    install -d -m 0755 /tmp/identity/src; \
    cp -a /opt/assets/baseline/. /tmp/identity/src/; \
    chmod -R u+rwX /tmp/identity/src; \
    /usr/bin/python3 -I /tests/behavioural/lib/mutants.py \
      --identity-only \
      --source /tmp/identity/src \
      --suite /tests/behavioural/suite.toml \
      --out /tmp/identity/out \
      --workspace /tmp/identity/work; \
    rm -rf /tmp/identity; \
    find /tests/behavioural -name __pycache__ -type d -prune -exec rm -rf {} +

ENV CARGO_NET_OFFLINE=true \
    RUST_BACKTRACE=1 \
    CMAKE_BUILD_PARALLEL_LEVEL=4 \
    TZ=UTC \
    LC_ALL=C.UTF-8 \
    LANG=C.UTF-8 \
    PYTHONPATH=/opt/swerefactor \
    SWEREFACTOR_ASSETS=/opt/assets \
    SWEREFACTOR_REPO=/workspace/repo \
    SWEREFACTOR_LOG_DIR=/logs/verifier

RUN mkdir -p /opt/original /workspace /logs/verifier

WORKDIR /tests/behavioural
