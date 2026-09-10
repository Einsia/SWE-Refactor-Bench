# =============================================================================
# SWERefactorBench -- lang04-acorn-js-to-rust
# Stage 3 image: the verification probe.
#
#     docker build -t swerefactor/lang04-verification:1 \
#         tasks/lang04-acorn-js-to-rust/tests/verification
#
# Six rounds, an hour each, trying to write a test that acorn 8.14.0 passes and
# the Rust port fails.  So this is the only image that has to produce BOTH trees'
# artifacts: the submission from Rust with cargo and its own Makefile, the
# original from JavaScript with the pinned rollup.  It therefore carries both
# toolchains and, unlike stage 2, keeps the bundler.
#
# That is not a loophole.  Stage 1 has already judged whether the JavaScript left,
# and stage 2 runs in an image where the submission is built and installed on its
# own and measured over 16,049 recorded cases.  What this image adds is the ability
# to ask the original a question at grading time, which is the entire point of the
# stage: a candidate's claim that "8.14.0 does X" is worth nothing unless 8.14.0
# is right there to demonstrate it, and `try_test` reports the original's own
# result on every attempt precisely so a round cannot build on a guess.
#
# WHAT A CANDIDATE CAN AND CANNOT BE STOPPED FROM SEEING
#
# A candidate is arbitrary Python written by a model, and it runs as root.  One of
# the two trees is node and the other is a compiled binary; a candidate that wants
# to know which is which can time the first request, read what `bin/acorn`
# ultimately execs, or look in /proc.  None of that can be closed off by the
# image, and this file does not pretend otherwise.  What run-candidate.sh does is
# remove the *cheap* tells -- both prefixes present the same two wrapper scripts
# at the same paths, and no path a candidate is handed contains the word
# "original" -- and what probe.toml does is deny identification of the tree under
# test however it is learned, so that succeeding at it earns nothing.  The line
# being defended is "telling earns no points", not "telling is impossible".
#
# One consequence is deliberate: reference.js is installed to /opt/assets and is
# *not* left under /tests/verification/lib, which is the directory on the
# candidate's PYTHONPATH.  A candidate could still reach it by absolute path.  It
# would learn how the harness implements the protocol, which is not secret -- it
# is specified in instruction.md -- but it would not learn which tree it is on,
# because both trees answer through the same file.
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
ARG NODE_VERSION=22.20.0
ARG NODE_SHA256=00bbd05e306ea68b6e13e17360d0e2f680b493ef95f2fea1c4296ff7437530bc

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# The same frozen snapshot as the agent environment and stage 2, so the original
# this image bundles is the original every other image bundles.
# Check-Valid-Until has to be off: a snapshot's Release file is, by construction,
# always expired.  Written as the base image's own debian.sources and preceded by
# `rm -f /etc/apt/sources.list` -- a second file would be *added* to the live
# archive rather than replace it, and these pins would then resolve against
# whatever deb.debian.org holds today.
#
# The four load-bearing pins are the ones stage 2 also pins, at the same
# versions: make builds the submission, python3 runs the harness and the
# candidates, and file and xz-utils are used while installing node.  The reading
# tools the adversaries use on the source are left unpinned, as lang01 leaves
# them -- a newer ripgrep does not change what the parser answers.
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
      python3-venv=3.11.2-1+b1 \
      xz-utils=5.4.1-1 \
      diffutils \
      git \
      jq \
      less \
      ripgrep \
      tree; \
    rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*.deb /var/cache/apt/*.bin

# node from the official dist tarball rather than the distro: the distro's nodejs
# moves within a snapshot's lifetime, and this interpreter is what the original
# side of every comparison runs on.  Same version and digest as the agent
# environment and stage 2.
RUN set -eux; \
    cd /tmp; \
    curl -fsSLO "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz"; \
    echo "${NODE_SHA256}  node-v${NODE_VERSION}-linux-x64.tar.xz" | sha256sum -c -; \
    tar -xJf "node-v${NODE_VERSION}-linux-x64.tar.xz" -C /usr/local --strip-components=1 \
      --exclude='*/CHANGELOG.md' --exclude='*/README.md' --exclude='*/LICENSE'; \
    rm -f "node-v${NODE_VERSION}-linux-x64.tar.xz"

# grep -Fx so a version that merely contains the string does not pass.  A
# mismatch here would make every candidate compare against the wrong original,
# which is a failure that produces plausible numbers rather than an error.
RUN set -eux; \
    rustc --version | cut -d' ' -f2 | grep -Fx '1.90.0'; \
    cargo --version | cut -d' ' -f2 | grep -Fx '1.90.0'; \
    make --version | head -1 | grep -F 'GNU Make 4.3'; \
    node --version | grep -Fx "v${NODE_VERSION}"; \
    python3 --version | cut -d' ' -f2 | grep -Fx '3.11.2'

# cargo has to work offline, because the submission is built here with an empty
# registry.  Proven on a throwaway crate, so a broken toolchain is an image
# failure and not six rounds of exit 71.
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

# --- The bundler, staged once for the whole stage ----------------------------
# rollup and its buble plugin, locked by integrity hash.  Unlike stage 2, which
# deletes these after freezing its corpus, stage 3 keeps them: the original side
# of every comparison is bundled at run time from the tree the harness hands
# run-candidate.sh, so what a candidate is measured against is built from the
# bytes under test rather than from something baked in beside them.
#
# buble depends on acorn 6.4.2, which would take the top-level `acorn` name and
# shadow the 8.14.0 being built.  Nesting it under its only consumer is what npm
# does for a version conflict and what the agent environment does, and it frees
# the name.  Done here rather than in run-candidate.sh because it is a property of
# the image: doing it per run would repeat it 24 times and hide a failure in a
# build log.
COPY data/buildtools-package.json      /run/buildtools/package.json
COPY data/buildtools-package-lock.json /run/buildtools/package-lock.json

RUN set -eux; \
    cd /run/buildtools; \
    npm ci --no-audit --no-fund --ignore-scripts; \
    install -d /run/buildtools/node_modules/buble/node_modules; \
    for p in acorn acorn-dynamic-import acorn-jsx; do \
      if [ -d "/run/buildtools/node_modules/$p" ]; then \
        mv "/run/buildtools/node_modules/$p" \
           "/run/buildtools/node_modules/buble/node_modules/$p"; \
      fi; \
    done; \
    test ! -e /run/buildtools/node_modules/acorn; \
    node -e 'require("/run/buildtools/node_modules/buble")'; \
    node -p 'require("/run/buildtools/node_modules/buble/node_modules/acorn/package.json").version' \
      | grep -Fx '6.4.2'; \
    node -p 'require("/run/buildtools/node_modules/rollup/package.json").version' \
      | grep -Fx '3.29.4'; \
    test -f /run/buildtools/node_modules/rollup/dist/bin/rollup; \
    install -d /opt/buildtools; \
    mv /run/buildtools/node_modules /opt/buildtools/node_modules; \
    rm -rf /run/buildtools /root/.npm

# npm's work is done.  The submission is built with make and cargo and the
# original with rollup invoked directly, so no package manager has a role from
# here on -- and their absence means an absolute-path invocation by a candidate
# or a submission fails outright rather than quietly succeeding.  node itself
# stays: it is what the original side runs on.
RUN set -eux; \
    rm -rf /usr/local/lib/node_modules/npm /usr/local/bin/npm /usr/local/bin/npx \
           /usr/local/bin/corepack /root/.npm; \
    for tool in npm npx yarn pnpm corepack deno bun; do \
      if command -v "${tool}" >/dev/null 2>&1; then \
        echo "FATAL: ${tool} is still reachable at $(command -v "${tool}")" >&2; \
        exit 1; \
      fi; \
    done; \
    node --version | grep -Fx "v${NODE_VERSION}"; \
    test -f /opt/buildtools/node_modules/rollup/dist/bin/rollup; \
    echo "bundler staged, package managers gone, node retained"

# --- The candidate's test stack ----------------------------------------------
# pytest and pytest-timeout, at one set of versions for both trees.  A candidate
# that ran under different pytest versions on the two trees would not be comparing
# the two implementations.  Nothing else: a candidate imports srbacorn and the
# standard library.
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

# reference.js is moved out of the candidate's PYTHONPATH and installed where
# run-candidate.sh's wrapper expects it.  Its digest is asserted against stage 2's
# copy of the same file: the two stages must answer the protocol identically, and
# two copies of one file in one repository is exactly the shape that drifts.  If
# this fails, the copies diverged and one of the two stages is measuring a
# protocol the other does not implement.
RUN set -eux; \
    echo 'f7c5f5fc00b1805a67aa65f0429187e35f8a4c53b9cfab9bd4c4727879bb762e  /tests/verification/lib/reference.js' \
      | sha256sum -c -; \
    install -D -m 0644 /tests/verification/lib/reference.js /opt/assets/reference.js; \
    rm -f /tests/verification/lib/reference.js; \
    node --check /opt/assets/reference.js

# This stage ships no copy of State A, and that is asserted rather than assumed.
# The original arrives at run time on /opt/original and run-candidate.sh bundles
# the copy the harness makes of it, so an archive in data/ would be an input to
# nothing -- but it would be a second answer to "what was the original", with no
# way to tell which one a failure came from.  Stage 2 does need its own copy,
# because it freezes expectations before any submission exists and cannot take its
# baseline from the party being graded.  Stage 3 asks the tree in front of it.
RUN set -eux; \
    if [ -e /tests/verification/data/original.tar.gz ]; then \
      echo "FATAL: stage 3 has acquired its own copy of State A" >&2; exit 1; \
    fi; \
    test -f /tests/verification/data/buildtools-package-lock.json

RUN set -eux; \
    chmod +x /tests/verification/run-candidate.sh; \
    bash -n /tests/verification/run-candidate.sh; \
    /opt/venv/bin/python -m py_compile /tests/verification/lib/srbacorn.py; \
    # A scope-less probe is unwinnable and a mismatched adversary count creates
    # points nobody can earn.  Checked here as well as by `swerefactor validate`,
    # because this is the image that would otherwise discover it six rounds in.
    python3 /tests/verification/check-probe.py; \
    # The prompt is what the six rounds are actually given.  A placeholder this
    # harness does not fill stays in the text verbatim, so a typo becomes an
    # instruction to read a directory called `{{orginal}}`; and `gates` and
    # `findings` render empty in this stage, so a stage-3 prompt that used them
    # would be quietly missing a paragraph.
    python3 -c "\
import re, sys; \
from swerefactor import audit; \
text = open('/tests/verification/prompt.txt', encoding='utf-8').read(); \
used = set(re.findall(r'\{\{(\w+)\}\}', text)); \
unknown = sorted(used - set(audit.PLACEHOLDERS)); \
sys.exit('prompt.txt: unknown placeholder(s): %s' % unknown) if unknown else None; \
empty = sorted(used & {'gates', 'findings'}); \
sys.exit('prompt.txt: %s render empty in stage 3' % empty) if empty else None; \
sys.exit('prompt.txt is too short to be a brief: %d chars' % len(text)) if len(text) < 2000 else None; \
print('prompt.txt ok: %d chars, placeholders %s' % (len(text), sorted(used)))"; \
    find /tests/verification -name __pycache__ -type d -prune -exec rm -rf {} +

ENV CARGO_NET_OFFLINE=true \
    RUST_BACKTRACE=1 \
    TZ=UTC \
    LC_ALL=C.UTF-8 \
    LANG=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1

RUN mkdir -p /opt/original /workspace /logs/verifier

WORKDIR /tests/verification
