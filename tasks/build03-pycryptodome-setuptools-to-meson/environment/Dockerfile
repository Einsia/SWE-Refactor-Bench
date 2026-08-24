# SWERefactorBench — build-toolchain / pycryptodome: setuptools+distutils -> Meson
#
# Agent environment (State A). Ships the toolchain for *both* build systems,
# because the agent has to be able to run the setuptools build it is replacing:
# the old build is the specification, and reading setup.py is not the same as
# watching what compiler_opt.py actually puts on a command line.
#
# The grading images are elsewhere and are not this one. Stage 2
# (tests/behavioural/Dockerfile) has Meson, meson-python and ninja and asserts at
# its own build time that setuptools, distutils and pkg_resources cannot be
# imported, so a submission that still needs them cannot build there at all.
# Stage 1 (tests/audit/Dockerfile) has no build system and no compiler,
# and stage 3 (tests/verification/Dockerfile) has both paths. None of the three is
# visible to the agent, and nothing in this image tells the agent what any of them
# check — this file installs a toolchain and unpacks a repository, and that is the
# whole of it.
#
# Base image pinned by digest: python:3.12-slim-bookworm, CPython 3.12.13 on
# Debian 12, gcc 12.2.0, binutils 2.40. The same digest is pinned by the grading
# images that need a compiler, so the answers gcc gives about macros, per-unit
# flags and emitted instructions are the same answers on both sides of every
# comparison — and so are the answers CPython gives about ABI tags and extension
# suffixes.
#
# CPython 3.12 is the forcing function for the whole task: distutils was removed
# from the standard library in 3.12 (PEP 632), so `setup.py` works here only
# because setuptools ships a vendored copy of it.
FROM python:3.12-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b

ENV DEBIAN_FRONTEND=noninteractive \
    LC_ALL=C.UTF-8 \
    LANG=C.UTF-8

# ---------------------------------------------------------------------------
# Toolchain. Installed at image-build time; the trial container has no network.
#
# libgmp-dev because Crypto.Math loads libgmp through ctypes and the self-test
# exercises that path. binutils/patchelf/file so the agent can inspect what its
# own build produced — the 41 artifacts are ELF shared objects and `nm -D` is how
# you find out what they export.
# ---------------------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential \
      binutils \
      patchelf \
      file \
      libgmp-dev \
      pkg-config \
      git \
      ca-certificates \
      procps less vim-tiny \
    && rm -rf /var/lib/apt/lists/* \
    && gcc --version | head -1 && nm --version | head -1

# ---------------------------------------------------------------------------
# Build tooling, pinned. `setuptools` and `wheel` are here because State A needs
# them to build at all — they are what the migration removes. `build` provides the
# PEP 517 front end both states are driven through, so the command that produces a
# wheel does not change when the backend does.
#
# `pycryptodome-test-vectors` carries the Wycheproof and NIST data the self-test
# suite loads; without it a large part of the 39,245 cases would not run.
# ---------------------------------------------------------------------------
RUN pip install --no-cache-dir \
      "setuptools==69.0.3" \
      "wheel==0.42.0" \
      "build==1.0.3" \
      "meson==1.3.2" \
      "meson-python==0.15.0" \
      "ninja==1.11.1.1" \
      "pyproject-metadata==0.7.1" \
      "packaging==23.2" \
      "pycryptodome-test-vectors==1.0.14" \
    && python3 -c "import setuptools, wheel, build, mesonpy; print('build tooling ok')" \
    && meson --version && ninja --version

# ---------------------------------------------------------------------------
# Repository snapshot. Verified by checksum, then unpacked to /workspace/repo.
# The tarball carries no VCS metadata and no build system other than the
# setuptools one.
# ---------------------------------------------------------------------------
WORKDIR /workspace

ARG REPO_SHA256=2851a1a416e12d3e00cee986e548a5a29f08fafcfc08fc3bc23f6cc8ad05d527
COPY original.tar.gz /tmp/original.tar.gz
RUN echo "${REPO_SHA256}  /tmp/original.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/original.tar.gz -C /workspace \
    && rm -f /tmp/original.tar.gz \
    && test -d /workspace/repo \
    && test ! -e /workspace/repo/.git \
    && test ! -e /workspace/repo/.gitignore

# The snapshot must be exactly State A: the setuptools build present, no VCS
# metadata the agent could mine for the upstream answer, no alternative build
# system, and no prebuilt artefact to copy. A failure here fails the image build.
RUN set -eu; cd /workspace/repo; \
    for p in setup.py compiler_opt.py MANIFEST.in setup.cfg pyproject.toml \
             lib/Crypto/__init__.py lib/Crypto/Cipher/AES.py \
             lib/Crypto/SelfTest/__init__.py \
             src/AES.c src/AESNI.c src/ghash_clmul.c src/cpuid.c \
             src/modexp.c src/ec_ws.c; do \
      test -e "$p" || { echo "MISSING from State A: $p" >&2; exit 1; }; \
    done; \
    for p in .git .svn .hg .gitattributes meson.build meson_options.txt \
             meson.options subprojects build dist builddir \
             lib/Crypto/Cipher/_raw_aes.abi3.so; do \
      test ! -e "$p" || { echo "LEAK in State A: $p" >&2; exit 1; }; \
    done; \
    if find . -name 'meson.build' -o -name '.git' -o -name '*.abi3.so' \
              -o -name '*.egg-info' | grep -q .; then \
      echo "LEAK: build-system metadata or prebuilt artefact found" >&2; exit 1; fi; \
    n=$(find . -type f | wc -l); \
    test "$n" -eq 590 || { echo "unexpected file count: $n" >&2; exit 1; }; \
    echo "State A snapshot verified ($n files)"

# ---------------------------------------------------------------------------
# Baseline git snapshot: one commit, no history, no remotes. Gives the agent a
# clean `git diff` against State A without revealing upstream history.
# ---------------------------------------------------------------------------
COPY gitignore.baseline /workspace/repo/.gitignore
RUN set -eu; cd /workspace/repo; \
    git init -q -b main; \
    git config user.email "bench@swerefactor.local"; \
    git config user.name "SWERefactorBench"; \
    git config commit.gpgsign false; \
    git add -A; \
    git -c core.autocrlf=false commit -q \
        -m "State A: pycryptodome 3.20.0, setuptools/distutils build system"; \
    git tag state-a; \
    git --no-pager log --oneline

# ---------------------------------------------------------------------------
# Offline dependency + initial-build validation.
#
# Done on a scratch copy so that /workspace/repo stays pristine for the trial:
# every trial starts from the same clean State A tree with no build products. A
# failure here fails the image build, which is what we want — it proves the
# environment can build State A entirely offline.
#
# It runs at image-build time and is then deleted: the agent never receives it,
# and it says nothing about grading — it builds State A the way State A documents
# and fails the image build if that does not work.
# ---------------------------------------------------------------------------
COPY verify_environment.sh /tmp/verify_environment.sh
RUN chmod +x /tmp/verify_environment.sh \
    && /tmp/verify_environment.sh \
    && rm -f /tmp/verify_environment.sh

ENV PIP_NO_INDEX=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SOURCE_DATE_EPOCH=1704758400 \
    TZ=UTC
WORKDIR /workspace/repo
CMD ["/bin/bash"]
