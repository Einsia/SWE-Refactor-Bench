# =============================================================================
# SWERefactorBench -- fw03-conduit-vue-to-react
# Stage 3 image: the verification probe.
#
#     docker build -t swerefactor/fw03-verification:1 \
#         tasks/fw03-conduit-vue-to-react/tests/verification
#
# Six models, an hour each, trying to write a browser test the original passes
# and the submission fails. So this image is the only one that can drive BOTH
# applications.
#
# It gets there differently for each side, because the two sides are not
# symmetric here:
#
#   submission  built from the tree with the target toolchain, from the same
#               offline npm cache stage 2 uses, minted from the same lockfile.
#
#   original    unpacked from data/oracle-dist.tgz -- State A's own build output
#               from the source the agent received, byte-identical to the copy
#               stage 2 measures against and to the one the agent had at
#               /opt/oracle/dist.
#
# Building State A from source here was the alternative and was rejected: its
# toolchain (@vue/cli-service 3, webpack 4, node-sass 4.12 against a different
# Node ABI) is not installable from the target's cache, so it would need a second
# cache and a second Node in this image to reproduce a bundle that is already
# shipped and checksummed. What the stage needs is both artifacts present and
# drivable, which this gives.
#
# That is not a loophole. The submission is built only from the target cache, in
# which Vue, vue-router, vuex and @vue/cli-service are not installable at all --
# the same assertion stage 2 makes, repeated below because this image builds too.
# So a submission cannot pass by reaching for something only the reference side
# has: the reference side has no node_modules at all, only static files.
#
# The candidate's Python is a test runner and a driver, at one set of versions for
# both trees. A candidate that ran under different versions on the two sides would
# not be comparing the two applications.
#
# At runtime the harness provides:
#
#     /workspace/repo    the submission                            (read-only)
#     /opt/original      State A, unpacked from original.tar.gz    (read-only)
#     /logs/verifier     where the stage result JSON is written
#
# The stage copies both trees before building or driving anything, so what stage 2
# measured is not what stage 3 mutates.
#
# Network: reaches the model API for the six rounds and the adjudicator, and
# nothing else. Candidate tests run with SRB_NETWORK=denied: a test that needs the
# internet is not measuring this migration, and the browser is sealed against
# off-origin requests regardless.
#
# Pinned by digest, like the other two images, so the grader cannot drift.
# =============================================================================

ARG INFRA_IMAGE=swerefactor/infra:1
FROM ${INFRA_IMAGE} AS infra

FROM node@sha256:968ca0550acc7589a8b1324401ec6e39ace53b2c82d2aed3a278e9ff491c2b1c

ENV DEBIAN_FRONTEND=noninteractive \
    NPM_CONFIG_CACHE=/opt/npm-cache \
    NPM_CONFIG_OFFLINE=true \
    NPM_CONFIG_AUDIT=false \
    NPM_CONFIG_FUND=false \
    NPM_CONFIG_UPDATE_NOTIFIER=false \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright-browsers \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=0 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=UTC

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        diffutils \
        file \
        jq \
        procps \
        ripgrep \
        util-linux \
        python3 \
        python3-pip \
        python3-venv \
    && rm -rf /var/lib/apt/lists/*

# --- The target dependency closure, as an offline npm cache ------------------
# Byte-identical inputs to stage 2's and to the environment image's, warmed the
# same way and for the same reason: `npm ci` reads resolved URLs from the lockfile
# and never fetches a package document, so a cache warmed only by `npm ci` cannot
# resolve a range. A submission writes its own manifest, so the second pass is
# what makes a lockfile-free `npm install --offline` possible at all.
COPY allowlist-package.json /opt/allowlist/package.json
COPY allowlist-package-lock.json /opt/allowlist/package-lock.json
RUN cd /opt/allowlist \
 && NPM_CONFIG_OFFLINE=false npm ci --ignore-scripts --loglevel=error \
 && rm -rf /opt/allowlist/node_modules \
 && npm cache verify >/dev/null

RUN set -eu; d=$(mktemp -d); cd "$d"; \
    cp /opt/allowlist/package.json .; \
    NPM_CONFIG_OFFLINE=false npm install --ignore-scripts --loglevel=error; \
    cd /; rm -rf "$d"; \
    echo "package documents cached for lockfile-free resolution"

# This image builds the submission, so it needs the same withholding stage 2 has.
# If the retired stack were installable here, a submission that never migrated
# would build in the one image that can also drive the reference -- and a round
# would then compare State A against State A.
RUN set -eu; \
    for pkg in vue vue-router vuex vue-template-compiler vue-axios \
               @vue/cli-service @vue/cli-plugin-babel @vue/cli-plugin-pwa \
               @vue/compat vuera veaury petite-vue alpinejs preact; do \
      if grep -rqsl "\"name\":\"$pkg\"" /opt/npm-cache/_cacache/index-v5 2>/dev/null; then \
        echo "FATAL: retired package '$pkg' is present in the offline cache"; exit 1; \
      fi; \
    done; \
    echo "cache holds no retired framework package"

# --- The browser -------------------------------------------------------------
# The same Playwright and the same Chromium stage 2 drives with, at the same path.
RUN printf '{"name":"swerefactor-tooling","private":true,"version":"1.0.0"}\n' > /opt/package.json \
 && cd /opt \
 && NPM_CONFIG_OFFLINE=false npm i --loglevel=error playwright@1.49.1 \
 && /opt/node_modules/.bin/playwright install --with-deps chromium

# ESM resolution walks up the directory chain and nothing else: no NODE_PATH, no
# global root. From /tests/verification/lib the chain is /tests/verification,
# /tests, / -- and /opt is not on it, so probe-one.mjs's `import "playwright"`
# needs this. One symlink rather than a second 300MB install of the same driver.
# mkdir first: the probe is copied in below, so /tests does not exist yet.
RUN mkdir -p /tests && ln -s /opt/node_modules /tests/node_modules

# --- The candidate's Python, one version for both trees ----------------------
# pytest and pytest-timeout are what a candidate may import, plus the driver
# module in lib/. Declared in a lockfile rather than inline: the versions a
# candidate may import are part of the task definition, and a reviewer should
# find them by opening a file named after them.
#
# No HTTP client. A candidate does not speak to the app over HTTP -- it drives a
# browser through srbprobe and reads back a normalised observation, which is the
# same reduction stage 2 compares. Handing it a base URL instead would hand it the
# bundle's bytes, and with them the identity of the tree.
COPY requirements-candidate.txt /opt/requirements-candidate.txt
RUN python3 -m pip install --break-system-packages \
        --require-hashes --no-deps -r /opt/requirements-candidate.txt \
 && python3 -c "import pytest, pytest_timeout; print('candidate stack ok')"

# --- The harness's own interpreter -------------------------------------------
# Runs the six rounds and the adjudicator. Kept apart from the interpreter that
# runs candidate code: the process orchestrating the stage must not share an
# environment with code an adversary wrote.
RUN python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --upgrade "pip==24.3.1" \
 && /opt/venv/bin/python -c "print('harness venv ok')"

COPY --from=infra /opt/swerefactor /opt/swerefactor
ENV PYTHONPATH=/opt/swerefactor

# --- The probe ---------------------------------------------------------------
COPY . /tests/verification

RUN chmod +x /tests/verification/run-candidate.sh \
 && bash -n /tests/verification/run-candidate.sh

# The driver has to be the harness the other two stages use, or "the same
# reduction stage 2 compares" is a sentence about two different reductions -- and
# a divergence found here would not be one stage 2 could have seen. The digest
# list is generated from environment/harness/; `swerefactor validate` checks this
# copy of it against the environment's, and this line checks the files against it.
RUN cd /tests/verification/lib/harness \
 && sha256sum -c /tests/verification/data/harness-core.sha256 \
 && echo "harness core matches the copy the agent was given"

# The reference build, checked here rather than only at run time: an image whose
# tarball is truncated should fail to build, not fail every round with "the
# reference does not match its checksum".
RUN cd /tests/verification/data && sha256sum -c oracle-dist.tgz.sha256

# A scope-less probe is unwinnable and a mismatched adversary count creates points
# nobody can earn. Checked here as well as by `swerefactor validate`, because this is
# the image that would otherwise discover it six rounds in. The same script
# imports srbprobe and exercises every accessor prompt.txt promises, so a renamed
# harness export fails the build rather than an adversary's first candidate.
#
# Run with the interpreter that will run candidates, not the harness venv: the
# half of this check that exercises srbprobe is only meaningful in the environment
# a candidate actually gets.
RUN python3 /tests/verification/check-probe.py

# The driver, end to end, against the reference build. This is the expensive line
# in this file and it earns it: it proves Chromium runs here, that the harness can
# serve and drive a bundle in this image, and that a scenario written the way
# prompt.txt describes actually produces an observation -- the precondition every
# one of the six rounds depends on. A Chromium that cannot start would otherwise
# surface as six rounds of errors and a stage that scores nothing.
#
# Nothing is kept: no expectation is recorded and no observation is baked into a
# layer, only the fact that it worked.
RUN set -eu; \
    mkdir -p /tmp/probesmoke/dist; \
    tar -C /tmp/probesmoke/dist -xzf /tests/verification/data/oracle-dist.tgz \
        --strip-components=1; \
    cd /tests/verification; \
    SRB_PROBE_DIST=/tmp/probesmoke/dist \
    SRB_PROBE_WORK=/tmp/probesmoke \
    PYTHONPATH=/tests/verification/lib \
    python3 -c "\
import srbprobe; \
obs = srbprobe.observe([{'goto': '#/'}, {'click': '.sidebar .tag-pill'}], \
                       probes=['.article-preview'], name='smoke'); \
assert obs.rendered, f'the reference build did not render: {obs.step_errors}'; \
assert not obs.step_errors, f'steps failed: {obs.step_errors}'; \
assert obs.probe('.article-preview').count > 0, 'no previews on the tag feed'; \
assert len(obs.requests) > 0, 'the page made no API calls'; \
print(f'probe smoke: {obs}')"; \
    rm -rf /tmp/probesmoke

RUN mkdir -p /opt/original /workspace /logs/verifier

WORKDIR /tests/verification
