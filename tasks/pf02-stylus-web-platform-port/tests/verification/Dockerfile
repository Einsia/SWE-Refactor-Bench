# =============================================================================
# SWERefactorBench -- pf02-stylus-web-platform-port
# Stage 3 image: the verification probe.
#
#     docker build -t swerefactor/pf02-verification:1 \
#         tasks/pf02-stylus-web-platform-port/tests/verification
#
# Six models, an hour each, trying to write a test the original Stylus passes
# and the ported one fails.  Both trees are npm packages, so unlike build01 this
# stage has no second toolchain to install: what it needs is one Node, one npm,
# and an offline cache holding exactly the five dependencies State A declares.
#
# The Node version is pinned to stage 2's, digest and all, because almost
# everything a candidate can measure here is an answer Node gives.  Module
# resolution reads `exports` and `main` and `type`; a function's own properties
# depend on whether its module is strict; `queueMicrotask` ordering decides
# whether a callback ran before the next line.  A different Node would change
# State A's own answers, and every candidate would then be comparing two
# interpreters instead of two trees.
#
# At runtime the harness provides:
#
#     /workspace/repo    the submission                            (read-only)
#     /opt/original      State A, unpacked from original.tar.gz    (read-only)
#     /logs/verifier     where the stage result JSON is written
#
# The stage copies both trees before anything is installed, so what stage 2
# measured is not what stage 3 mutates, and both copies are writable: `npm ci`
# writes `node_modules/` into the tree it installs.
#
# Network: reaches the model API for the six rounds and the adjudicator, and
# nothing else.  npm never reaches it -- every install in this stage is
# `--offline` against the cache baked in below, which is also how "there is no
# network, and a new dependency would not install" (§3) stops being a promise and
# becomes a property of the image.
# =============================================================================

ARG INFRA_IMAGE=swerefactor/infra:1
FROM ${INFRA_IMAGE} AS infra

FROM node:22.14.0-bookworm-slim@sha256:1c18d9ab3af4585870b92e4dbc5cac5a0dc77dd13df1a5905cea89fc720eb05b

# Same payload digests stage 2 pins.  Both files are byte-identical copies of
# stage 2's, and these ARGs are how a divergence gets caught at build time rather
# than becoming a difference between what the two stages measured.
ARG REPO_SHA256=919ce95f40ea747fd1b0fd5b8dd40c7f40fdfa8cb5102368df2343df3f6f4221
ARG NPM_CACHE_SHA256=fc1947e124174fb27a32d6f6ec6e0eab3dc7e7b456e529827985d2d1550f8d4b

ENV DEBIAN_FRONTEND=noninteractive \
    LC_ALL=C.UTF-8 \
    LANG=C.UTF-8 \
    TZ=UTC \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONHASHSEED=0 \
    NO_COLOR=1 \
    NPM_CONFIG_CACHE=/opt/npm-cache \
    NPM_CONFIG_UPDATE_NOTIFIER=false \
    NPM_CONFIG_FUND=false \
    NPM_CONFIG_AUDIT=false

# --- Tools -------------------------------------------------------------------
# The node image already carries node and npm.  Added here: python3 for the
# harness and the candidates, and the reading tools a candidate uses on the corpus
# it renders from.  The assertion is the inverse of stage 2's -- stage 2 asserts a
# forbidden toolchain is absent, and this stage asserts everything BOTH trees need
# is present, because a missing tool here would not fail the stage.  It would make
# every candidate fail on the original, which reads as "nobody found anything" and
# pays the submission all 60 points.
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-venv \
      xz-utils gzip tar \
      coreutils findutils diffutils \
      jq less ripgrep tree \
    && rm -rf /var/lib/apt/lists/* \
    && for required in node npm python3 tar find diff jq; do \
         command -v "$required" >/dev/null 2>&1 || { \
           echo "FATAL: the stage-3 image is missing $required." >&2; \
           echo "This stage drives BOTH trees. A tool the ORIGINAL needs and" >&2; \
           echo "does not have makes every candidate fail on the original," >&2; \
           echo "which reads as 'nobody found anything' and pays 60 points." >&2; \
           exit 1; }; \
       done \
    && want="v22.14.0" \
    && got="$(node --version)" \
    && { [ "$got" = "$want" ] || { \
           echo "FATAL: stage-3 node is $got, expected $want." >&2; \
           echo "A candidate measures module resolution, strict-mode function" >&2; \
           echo "properties and microtask ordering -- all answers Node gives." >&2; \
           exit 1; }; } \
    && node --version && npm --version && python3 --version

# --- The offline npm cache ---------------------------------------------------
# Holds exactly what State A's lockfile declares.  It stays in the image, because
# every install in this stage happens at run time: srbstylus runs `npm ci
# --offline` in whichever tree it is handed, once per tree, and caches the result.
#
# A submission that added a sixth dependency does not install from this cache at
# all, which is §3's no-network rule being enforced rather than stated.  Note what
# that means for scoring: such a submission fails every candidate at the install
# step, including the ones that would otherwise have found nothing.  That is the
# correct outcome -- a package whose dependencies cannot be installed offline is
# broken for the consumer this whole stage is written from the point of view of.
WORKDIR /opt/payload
COPY data/original.tar.gz data/npm-offline-cache.tar.gz data/SHA256SUMS ./
RUN sha256sum -c SHA256SUMS \
 && echo "${REPO_SHA256}  original.tar.gz" | sha256sum -c - \
 && echo "${NPM_CACHE_SHA256}  npm-offline-cache.tar.gz" | sha256sum -c -

RUN set -eu; \
    mkdir -p /opt/npm-cache; \
    tar -xzf npm-offline-cache.tar.gz -C /opt; \
    mv /opt/npmcache/* /opt/npm-cache/ 2>/dev/null || true; \
    rmdir /opt/npmcache 2>/dev/null || true; \
    du -sh /opt/npm-cache

# --- The candidate's test stack ----------------------------------------------
# pytest and pytest-timeout, hash-pinned, at one set of versions for both trees.
# A candidate running under different pytest versions on the two halves of its
# comparison would not be comparing the two trees.
#
# Nothing else.  A candidate imports srbstylus and the standard library; the
# compiler itself it reaches through Node, which is the point.  Debian's own
# python3-pytest is deliberately not used -- stage 2 uses it because its modules
# are the benchmark's own code, but a candidate's stack should be the same five
# versions in every task.
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
    node --check /tests/verification/lib/js/candidate-driver.mjs; \
    /opt/venv/bin/python -m py_compile /tests/verification/lib/srbstylus.py; \
    # srbstylus is the whole capability surface of this stage: two doors into the
    # package, and the one piece of code that knows how to open either without
    # revealing which tree answered.  A candidate that cannot import it can do
    # nothing, and an __all__ entry that does not exist is a name the prompt
    # advertises and the module does not have.
    SRB_TARGET=/nonexistent SRB_TARGET_TOKEN=check \
    PYTHONPATH=/tests/verification/lib /opt/venv/bin/python -c "\
import sys; import srbstylus as m; \
missing = [n for n in m.__all__ if not hasattr(m, n)]; \
sys.exit(f'srbstylus.__all__ names missing: {missing}') if missing else None; \
print('srbstylus ok: %d exports, stylus %s' % (len(m.__all__), m.VERSION))"; \
    # A scope-less probe is unwinnable and a mismatched adversary count creates
    # points nobody can earn.  Checked here as well as by `swerefactor validate`,
    # because this is the image that would otherwise discover it six rounds in.
    python3 /tests/verification/check-probe.py; \
    find /tests/verification -name __pycache__ -type d -prune -exec rm -rf {} +

# --- prove the ORIGINAL installs offline and answers through the driver -------
# The most important assertion in this file, and the reason it is worth a minute
# of build time.
#
# Every break in this stage is "passes on the original, fails on the submission".
# So anything that makes the ORIGINAL fail makes every candidate fail on it, and a
# candidate that fails on the original is discarded as broken -- six rounds of
# discarded candidates read as "six rounds found nothing", which pays the
# submission the whole 60 points. The failure mode is silent and it is scored in
# the submission's favour, which is the worst combination available.
#
# Three things could cause it, and all three are knowable now: the offline cache
# does not satisfy State A's lockfile, the consumer directory does not resolve a
# CommonJS package, or the driver does not run under this Node. So State A is
# unpacked here and driven through the real srbstylus -- the real `npm ci
# --offline`, the real `node_modules/stylus` symlink, the real `import('stylus')`,
# the real `bin/stylus` -- and the build fails if any of it does not answer.
#
# State A is then deleted. The harness mounts it at /opt/original at run time, so
# keeping a second copy at a fixed path would add nothing except a fixed path a
# candidate could diff the tree under test against, which is the identity break
# [scope] denies. One copy, provided by the harness, is enough.
# check-original.py is a file rather than a heredoc so that it can be run against
# an unpacked tree outside Docker, which is how it was developed.
RUN set -eu; \
    tar -xzf /opt/payload/original.tar.gz -C /opt/payload; \
    [ -f /opt/payload/repo/package.json ] || { \
      echo "FATAL: original.tar.gz did not unpack to repo/package.json" >&2; exit 1; }; \
    mkdir -p /tmp/selfcheck/install /tmp/selfcheck/scratch; \
    SRB_TARGET=/opt/payload/repo \
    SRB_TARGET_TOKEN=selfcheck00000000 \
    SRB_STATE=/tmp/selfcheck/install \
    SRB_SCRATCH=/tmp/selfcheck/scratch \
    PYTHONPATH=/tests/verification/lib \
    /opt/venv/bin/python /tests/verification/check-original.py; \
    rm -rf /opt/payload /tmp/selfcheck; \
    find /tests/verification -name __pycache__ -type d -prune -exec rm -rf {} +

# --- Runtime -----------------------------------------------------------------
# The mount points the harness fills in.  /opt/npm-cache must be writable:
# npm writes an index into its cache even for an offline install, and both trees
# are installed at run time.
RUN mkdir -p /opt/original /workspace /logs/verifier \
 && chmod -R a+w /opt/npm-cache

WORKDIR /tests/verification
