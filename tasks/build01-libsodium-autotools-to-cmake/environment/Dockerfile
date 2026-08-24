# SWERefactorBench — build-toolchain / libsodium: GNU Autotools -> CMake
#
# Agent environment (State A). Ships the full toolchain for *both* build systems,
# because the agent has to be able to run the Autotools build it is replacing:
# the old build is the specification, and reading configure.ac is not the same as
# watching what it actually puts on a command line.
#
# The grading images are elsewhere and are not this one. Stage 2
# (tests/behavioural/Dockerfile) has CMake and asserts at its own build time that
# autoconf, automake, libtool and m4 are absent, so a submission that still needs
# them cannot build there at all. Stage 1 (tests/audit/Dockerfile) has
# no build system, and stage 3 (tests/verification/Dockerfile) has both. None of
# the three is visible to the agent, and nothing in this image tells the agent
# what any of them check — this file installs a toolchain and unpacks a
# repository, and that is the whole of it.
#
# Base image pinned by digest: debian:bookworm-slim, Debian 12.15,
# gcc 12.2.0, binutils 2.40, cmake 3.25.1, ninja 1.11.1. The same digest is
# pinned by all three grading images, so the answers gcc gives about macros,
# per-file flags and emitted instructions are the same answers on both sides of
# every comparison.
FROM debian:bookworm-slim@sha256:7b140f374b289a7c2befc338f42ebe6441b7ea838a042bbd5acbfca6ec875818

ENV DEBIAN_FRONTEND=noninteractive \
    LC_ALL=C.UTF-8 \
    LANG=C.UTF-8

# ---------------------------------------------------------------------------
# Toolchain. Installed at image-build time; the trial container has no network.
# ---------------------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential \
      gcc g++ make \
      autoconf automake libtool m4 \
      cmake ninja-build \
      pkg-config \
      binutils file \
      python3 \
      git \
      ca-certificates \
      xz-utils bzip2 \
      less procps vim-tiny \
    && rm -rf /var/lib/apt/lists/* \
    && cmake --version && ninja --version && gcc --version | head -1

# ---------------------------------------------------------------------------
# Repository snapshot. Verified by checksum, then unpacked to /workspace/repo.
# The tarball carries no VCS metadata and no build system other than Autotools.
# ---------------------------------------------------------------------------
ARG REPO_SHA256=cf7b6ea531362d7855e6625a8bc71e42f3b9712ea5f2436c9775642c3cd2315e
COPY original.tar.gz /tmp/original.tar.gz
RUN echo "${REPO_SHA256}  /tmp/original.tar.gz" | sha256sum -c - \
    && mkdir -p /workspace/repo \
    && tar -xzf /tmp/original.tar.gz -C /workspace/repo \
    && rm -f /tmp/original.tar.gz \
    && test -f /workspace/repo/configure.ac \
    && test -f /workspace/repo/src/libsodium/Makefile.am \
    && test ! -e /workspace/repo/.git \
    && test ! -e /workspace/repo/.gitignore

# ---------------------------------------------------------------------------
# Baseline git snapshot: one commit, no history, no remotes. Gives the agent a
# clean `git diff` against State A without revealing upstream history.
# ---------------------------------------------------------------------------
COPY gitignore.baseline /workspace/repo/.gitignore
RUN cd /workspace/repo \
    && git init -q -b main \
    && git config user.email "bench@swerefactor.local" \
    && git config user.name  "SWERefactorBench" \
    && git config commit.gpgsign false \
    && git add -A \
    && git commit -q -m "State A: libsodium 1.0.20, GNU Autotools build system" \
    && git tag state-a \
    && git --no-pager log --oneline

# ---------------------------------------------------------------------------
# Offline dependency + initial-build validation.
#
# Done on a scratch copy so that /workspace/repo stays pristine for the trial:
# every trial starts from the same clean State A tree with no build products.
# A failure here fails the image build, which is what we want — it proves the
# environment can build State A entirely offline.
# ---------------------------------------------------------------------------
# It runs at image-build time and is then deleted: the agent never receives it,
# and it says nothing about grading — it builds State A the way State A documents
# and fails the image build if that does not work.
COPY verify_environment.sh /tmp/verify_environment.sh
RUN chmod +x /tmp/verify_environment.sh \
    && /tmp/verify_environment.sh \
    && rm -f /tmp/verify_environment.sh

WORKDIR /workspace/repo
ENV DO_NOT_UPDATE_CONFIG_SCRIPTS=1
CMD ["/bin/bash"]
