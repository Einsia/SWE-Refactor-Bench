# syntax=docker/dockerfile:1
# =============================================================================
# SWERefactorBench -- pf02-stylus-web-platform-port
# Agent environment: State A.
#
#     docker build -t swerefactor/pf02-env:1 tasks/pf02-.../environment
#
# What this image does, in order:
#
#   1. Unpack the frozen State A snapshot -- Stylus 0.63.0, upstream sources
#      minus all version-control and CI data -- into /workspace/repo.
#   2. Install its five runtime dependencies from a vendored offline cache.
#   3. Initialise a git repo holding exactly ONE baseline commit, tagged
#      `state-a`, so the agent has `git diff` against the starting point and no
#      history to mine.
#   4. Keep a read-only, still-runnable copy of State A at /opt/state-a, with a
#      `stylus-state-a` wrapper on PATH.
#
# The tarball is content-addressed: the build refuses to continue if the
# snapshot does not match original.sha256, so every trial in every run starts
# from bit-identical sources, and the graded stages compare against the same
# bytes the agent was given.
#
# The image is built with network access; the trial container runs without it,
# so every dependency must already be on disk when the agent starts.
#
# /opt/state-a is deliberately here.  The agent is allowed to observe State A's
# behaviour, because reproducing that behaviour is the task -- and the port
# deletes lib/, so without a frozen copy the agent would destroy its own
# reference on the first commit.  Nothing in this image reports on the grading
# gates or the graded dimensions: an agent that can query its own score
# optimises the report instead of the migration.
# =============================================================================
FROM node:22.14.0-bookworm-slim@sha256:1c18d9ab3af4585870b92e4dbc5cac5a0dc77dd13df1a5905cea89fc720eb05b

# Digest of the vendored npm cache.  The State A snapshot carries its digest in
# original.sha256, checked below; a mismatch on either fails the image build
# rather than silently handing the agent a different State A than it is scored
# against.
ARG NPM_CACHE_SHA256=fc1947e124174fb27a32d6f6ec6e0eab3dc7e7b456e529827985d2d1550f8d4b

ENV DEBIAN_FRONTEND=noninteractive \
    NPM_CONFIG_FUND=false \
    NPM_CONFIG_AUDIT=false \
    NPM_CONFIG_UPDATE_NOTIFIER=false \
    NPM_CONFIG_CACHE=/opt/npm-cache \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=UTC

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ca-certificates \
        git \
        make \
 && rm -rf /var/lib/apt/lists/*

# =============================================================================
# 1. Unpack the frozen State A snapshot into /workspace/repo.
# =============================================================================
COPY original.tar.gz  /opt/snapshot/original.tar.gz
COPY original.sha256  /opt/snapshot/original.sha256

RUN cd /opt/snapshot \
 && sha256sum -c original.sha256 \
 && mkdir -p /workspace \
 && tar xzf original.tar.gz -C /workspace \
 && test -f /workspace/repo/package.json \
 && test -f /workspace/repo/lib/stylus.js \
 && test -f /workspace/repo/bin/stylus \
 && test -d /workspace/repo/test/cases

# The snapshot must carry no version-control data: an agent that could read
# upstream history could read a published port instead of writing one.
RUN set -eu; \
    for forbidden in .git .gitignore .gitmodules .github .npmignore; do \
      if [ -e "/workspace/repo/${forbidden}" ]; then \
        echo "FATAL: snapshot leaked ${forbidden}" >&2; exit 1; \
      fi; \
    done; \
    if find /workspace/repo -name '.git*' -print -quit | grep -q .; then \
      echo "FATAL: snapshot contains nested version-control data" >&2; exit 1; \
    fi

# =============================================================================
# 2. Install the five runtime dependencies from the vendored cache.
#
# npm never reaches the network, here or at trial time.  The cache holds exactly
# the closure of package-lock.json, so `npm ci --offline` in the trial container
# reproduces this install and nothing wider.
# =============================================================================
COPY npm-offline-cache.tar.gz /opt/snapshot/npm-offline-cache.tar.gz

RUN cd /opt/snapshot \
 && echo "${NPM_CACHE_SHA256}  npm-offline-cache.tar.gz" | sha256sum -c - \
 && mkdir -p /opt/npm-cache \
 && tar xzf npm-offline-cache.tar.gz -C /opt \
 && (mv /opt/npmcache/* /opt/npm-cache/ 2>/dev/null || true) \
 && (rmdir /opt/npmcache 2>/dev/null || true) \
 && cd /workspace/repo \
 && npm ci --offline --no-audit --no-fund \
 && test -d /workspace/repo/node_modules/source-map \
 && rm -f /opt/snapshot/npm-offline-cache.tar.gz

# =============================================================================
# 3. A read-only, runnable copy of State A at /opt/state-a.
#
# The port deletes lib/.  Without this the agent's own reference implementation
# would disappear from the container the moment it started work, and "the same
# CSS as before" would become unobservable from inside the trial.
#
# Copied from the unpacked tree before the git init below, so /opt/state-a holds
# no repository either.
# =============================================================================
RUN cp -a /workspace/repo /opt/state-a \
 && printf '%s\n' \
      '#!/bin/sh' \
      '# State A (Stylus 0.63.0, upstream) command line, frozen and read-only.' \
      '# Same arguments as bin/stylus.  For diffing your port against the' \
      '# behaviour it has to reproduce.' \
      'exec node /opt/state-a/bin/stylus "$@"' \
      > /usr/local/bin/stylus-state-a \
 && chmod +x /usr/local/bin/stylus-state-a \
 && chmod -R a-w /opt/state-a

# =============================================================================
# 4. Prove the image is sound, on a throwaway copy.
#
# /workspace/repo must reach the agent exactly as unpacked, so the check runs
# against a copy and the copy is deleted in the same layer.  This asserts facts
# about the *environment* -- the upstream version, that the library and CLI run,
# that the upstream suite passes, that State A reads no clock and no RNG so
# differential comparison is sound.  It reports no verdict about any submission
# and it is removed from the image, so there is nothing here for an agent to
# read as a grading contract.
# =============================================================================
COPY verify_environment.sh /opt/verify_environment.sh

RUN chmod +x /opt/verify_environment.sh \
 && rm -rf /var/tmp/state-a-check \
 && cp -a /workspace/repo /var/tmp/state-a-check \
 && /opt/verify_environment.sh /var/tmp/state-a-check \
 && rm -rf /var/tmp/state-a-check /opt/verify_environment.sh

# =============================================================================
# 5. The baseline commit.
#
# One commit, one tag, no remote.  `git diff` and `git checkout -- .` work;
# there is no upstream history and nothing to fetch.
# =============================================================================
COPY gitignore.baseline /workspace/repo/.gitignore

RUN cd /workspace/repo \
 && git init -q -b main \
 && git config user.email "bench@swerefactor.local" \
 && git config user.name  "SWERefactorBench" \
 && git config commit.gpgsign false \
 && git add -A \
 && git commit -q -m "State A: stylus 0.63.0 (upstream, unmodified)" \
 && git tag state-a \
 && test "$(git rev-list --count HEAD)" = "1"

ENV STYLUS_TASK_STATE_A_TAG=state-a \
    STYLUS_STATE_A_ROOT=/opt/state-a

WORKDIR /workspace/repo
