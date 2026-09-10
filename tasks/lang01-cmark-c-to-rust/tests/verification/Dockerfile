# =============================================================================
# SWERefactorBench -- lang01-cmark-c-to-rust
# Stage 3 image: the verification probe.
#
#     docker build -t swerefactor/lang01-verification:1 \
#         tasks/lang01-.../tests/verification
#
# Six models, an hour each, trying to write a test the C passes and the Rust
# fails.  So this is the only image that has to build BOTH trees: the original from
# C with cc, the submission from Rust with cargo, each with the same published
# CMake argv.  It therefore carries both toolchains.
#
# That is not a loophole.  Stage 2 has already measured whether the submission
# needs a C compiler -- every repository compile lands in a shim's ledger there --
# and stage 1 has already judged, as a required gate, whether the C left.
# What this image adds is the ability to ask the C a question at grading time,
# which is the entire point of the stage: a candidate's claim that "the original
# does X" is worth nothing unless the original is right there to demonstrate it.
#
# The C compiler is also what a candidate uses for its own helpers.  srbcmark's
# CProgram compiles a candidate's C against the installed libcmark using flags
# from that install's own libcmark.pc, which is how the ABI, the custom allocator
# and the streaming parser get attacked -- none of them is reachable from a
# command line.  It cannot be used on the tree under test: both trees are built
# and installed before any candidate runs, and nothing here rebuilds them.
#
# At runtime the harness provides:
#
#     /workspace/repo    the submission                            (read-only)
#     /opt/original      State A, unpacked from original.tar.gz    (read-only)
#     /logs/verifier     where the stage result JSON is written
#
# The stage copies both trees before building anything, so what stage 2 measured
# is not what stage 3 mutates.
#
# Network: reaches the model API for the six rounds and the adjudicator, and
# nothing else.  cargo is configured offline, so a submission cannot acquire a
# dependency here that it could not acquire in the agent's own environment.
# =============================================================================

ARG INFRA_IMAGE=swerefactor/infra:1
FROM ${INFRA_IMAGE} AS infra

FROM rust@sha256:3914072ca0c3b8aad871db9169a651ccfce30cf58303e5d6f2db16d1d8a7e58f

ARG DEBIAN_FRONTEND=noninteractive
ARG DEBIAN_SNAPSHOT=20251020T000000Z

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# The environment image's closure, plus python3 for the harness and the candidates
# and the reading tools the adversaries use on the source.  Same Debian snapshot,
# so the C the original builds from here is the C it builds from anywhere else in
# this benchmark.
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
      pkg-config=1.8.1-1 \
      python3=3.11.2-1+b1 \
      python3-venv=3.11.2-1+b1 \
      diffutils \
      file \
      jq \
      less \
      ripgrep \
      tree \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*.deb /var/cache/apt/*.bin; \
    cmake --version | head -1 | grep -Fx 'cmake version 3.25.1'; \
    cc --version | head -1; \
    rustc --version | grep -Fx 'rustc 1.90.0 (1159e78c4 2025-09-14)'; \
    python3 --version

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

# --- The candidate's test stack ----------------------------------------------
# pytest and pytest-timeout, at one set of versions for both trees.  A candidate
# that ran under different pytest versions on the two trees would not be comparing
# the two implementations.  Nothing else: a candidate imports srbcmark and the
# standard library, and everything it needs beyond that it writes in C.
COPY requirements-candidate.txt /opt/pins/requirements-candidate.txt

RUN set -eux; \
    python3 -m venv /opt/venv; \
    /opt/venv/bin/pip install --no-cache-dir --upgrade "pip==24.3.1"; \
    /opt/venv/bin/pip install --no-cache-dir \
        --require-hashes -r /opt/pins/requirements-candidate.txt; \
    /opt/venv/bin/python -c "import pytest, pytest_timeout; print('candidate stack ok')"

# --- The harness -------------------------------------------------------------
# Runs the six rounds and the adjudicator.  The venv above is what candidates
# run in; this is the system python.  The process orchestrating the stage does not
# share an interpreter with code an adversary wrote.
COPY --from=infra /opt/swerefactor /opt/swerefactor
ENV PYTHONPATH=/opt/swerefactor

# --- The probe ---------------------------------------------------------------
COPY . /tests/verification

RUN set -eux; \
    chmod +x /tests/verification/run-candidate.sh; \
    bash -n /tests/verification/run-candidate.sh; \
    /opt/venv/bin/python -m py_compile /tests/verification/lib/srbcmark.py; \
    # A scope-less probe is unwinnable and a mismatched adversary count creates
    # points nobody can earn.  Checked here as well as by `swerefactor validate`,
    # because this is the image that would otherwise discover it six rounds in.
    python3 /tests/verification/check-probe.py; \
    find /tests/verification -name __pycache__ -type d -prune -exec rm -rf {} +

ENV CARGO_NET_OFFLINE=true \
    RUST_BACKTRACE=1 \
    CMAKE_BUILD_PARALLEL_LEVEL=4 \
    TZ=UTC \
    LC_ALL=C.UTF-8 \
    LANG=C.UTF-8

RUN mkdir -p /opt/original /workspace /logs/verifier

WORKDIR /tests/verification
