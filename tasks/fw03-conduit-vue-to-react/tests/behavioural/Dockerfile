# =============================================================================
# SWERefactorBench -- fw03-conduit-vue-to-react
# Stage 2 image: the behavioural suite.
#
#     docker build -t swerefactor/fw03-behavioural:1 \
#         tasks/fw03-conduit-vue-to-react/tests/behavioural
#
# Build context is this directory, so the suite is in the image and nothing else
# is. The agent container never sees it.
#
# What makes this a measurement rather than a claim: the image carries the same
# offline npm cache the agent's environment image did, minted from the same
# lockfile, and the `build` module installs and builds the submission from source
# into a fresh tree before any other module runs. Nothing the agent left behind is
# used -- no node_modules/, no dist/, no lockfile from a package manager this
# stage does not run. A submission whose manifest is broken cannot borrow a
# working install from the environment image.
#
# Both dependency closures are installable here: State B's from the offline npm
# cache, State A's from a yarn offline mirror minted from the lockfile State A
# actually ships.  Withholding Vue and asserting its absence would look like the
# stronger image, on the reasoning that "if the retired stack were installable
# here, a submission that never migrated would build".  That puts a stage-1
# question into stage 2's environment, and it costs the one property every other
# framework task has: feed State A back unchanged and stage 2 pays full marks,
# because stage 2 asks what the app does and State A is what it does.  Whether the
# port happened is asked by weight-0 checks in `build`, the way fw02 asks it about
# `X-Powered-By` -- recorded, reported, and unable to zero a behaviour score.
#
# The State A bundle ships in data/ as a tarball, byte-identical to the one the
# agent had at /opt/oracle/dist. It is not a recorded expectation: `build` unpacks
# it, mints a nonce, and drives 100 scenarios through one driver against both
# bundles under that nonce. The nonce is stamped through every title, body, bio,
# tag and comment the mock API serves, so the strings both sides must render did
# not exist when the submission was written.
#
# At runtime the harness provides:
#
#     /workspace/repo    the submission                       (read-write copy)
#     /opt/original      State A, unpacked from original.tar.gz    (read-only)
#     /logs/verifier     where the stage result JSON is written
#
# Network: none needed. Every install reads the cache; the browser is in the
# image; both servers listen on loopback.
#
# Pinned by digest, like the environment image, so the grader cannot drift.
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
        file \
        jq \
        procps \
        python3 \
        python3-pip \
        python3-venv \
    && rm -rf /var/lib/apt/lists/*

# --- The target dependency closure, as an offline npm cache ------------------
# Byte-identical inputs to the environment image's, warmed the same way and for
# the same reason: `npm ci` reads resolved URLs from the lockfile and never
# fetches a package document, so a cache warmed only by `npm ci` cannot resolve a
# range. The submission writes its own manifest, so the second pass is what makes
# a lockfile-free `npm install --offline` possible at all.
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

# --- State A's own closure, as a yarn offline mirror -------------------------
# State A ships yarn.lock and no package-lock.json.  Installing it with npm is
# not a stricter test of the same thing -- it is a different install: npm ignores
# the lockfile it cannot read and re-resolves 25 semver ranges against whatever
# is newest, which is how a probe of this repo ends up blaming eslint plugin
# resolution for a tree State A never had.  With the lockfile it ships, yarn
# installs the 2019 closure exactly, and eslint-plugin-prettier is right there.
#
# A mirror rather than more entries in the npm cache, because the mirror IS the
# lockfile's resolution: one tarball per locked entry, keyed by the lockfile.
# Nothing here re-resolves a range at grade time, for either side.
ARG YARN_VERSION=1.22.22
ENV YARN_CACHE_FOLDER=/opt/yarn-cache \
    SRB_YARN_MIRROR=/opt/yarn-mirror
COPY data/statea-package.json data/statea-yarn.lock data/statea-manifest.sha256 \
     /opt/statea-manifest/
# yarn is not installed, it is asserted: the base image ships 1.22.22 at
# /opt/yarn-v1.22.22, so the digest above already pins it and `npm i -g yarn`
# collides with the symlink it ships.  The assertion is what makes the pin visible.
RUN set -eu; \
    cd /opt/statea-manifest && sha256sum -c statea-manifest.sha256; \
    test "$(yarn -v)" = "${YARN_VERSION}" || { \
      echo "FATAL: base image ships yarn $(yarn -v), expected ${YARN_VERSION}"; \
      exit 1; }; \
    mkdir -p "$SRB_YARN_MIRROR"; \
    d=$(mktemp -d); cd "$d"; \
    cp /opt/statea-manifest/statea-package.json package.json; \
    cp /opt/statea-manifest/statea-yarn.lock yarn.lock; \
    printf 'yarn-offline-mirror "%s"\nyarn-offline-mirror-pruning false\n' \
        "$SRB_YARN_MIRROR" > .yarnrc; \
    yarn install --frozen-lockfile --ignore-scripts --non-interactive \
        --no-progress >/dev/null; \
    cd /; rm -rf "$d"; \
    echo "yarn offline mirror: $(ls "$SRB_YARN_MIRROR" | wc -l) tarballs"

# Both closures install, asserted -- rather than the retired one being absent,
# which is the assertion this image does not make and the header says why.  What
# is worth asserting is coverage, because the identity property rests on it: an
# offline install that silently stopped covering State A would otherwise show up
# as a submission failure with a grader-side cause.
RUN set -eu; \
    for pkg in vue vue-router vuex vue-template-compiler vue-axios \
               @vue/cli-service @vue/cli-plugin-babel @vue/cli-plugin-pwa; do \
      f=$(echo "$pkg" | sed 's|/|-|'); \
      if ! ls "$SRB_YARN_MIRROR" | grep -qi "^${f}-[0-9]"; then \
        echo "FATAL: State A needs '$pkg' and the offline mirror has no tarball"; \
        echo "  for it, so State A cannot install here and the identity property"; \
        echo "  this image is supposed to hold is broken before any grade runs."; \
        exit 1; \
      fi; \
    done; \
    echo "offline mirror covers the retired framework closure"

# The mint above ran with the network; a grade does not.  So the install is done a
# second time with --offline, which is the only form of it that says anything about
# grade time.  eslint-plugin-prettier is named explicitly because it is the package
# a probe of this repo blames when it installs with the wrong package manager: npm
# cannot read yarn.lock, re-resolves 25 ranges, and lands on a tree where
# .eslintrc.js names a plugin eslint-loader cannot find.  From the lockfile it is
# simply there, and asserting it here is what keeps that wrong diagnosis from being
# rediscovered.
#
# --ignore-scripts, here and nowhere else by default: node-sass 4.14.1's postinstall
# reaches node-gyp 3.8.0 for python2, and no binary for node 20's ABI was ever
# published -- for a dependency this tree imports from zero .scss files.  A
# submission still gets its scripts run; `build` withholds them only after an
# install has already failed, and records that it did.
RUN set -eu; \
    d=$(mktemp -d); cd "$d"; \
    cp /opt/statea-manifest/statea-package.json package.json; \
    cp /opt/statea-manifest/statea-yarn.lock yarn.lock; \
    printf 'yarn-offline-mirror "%s"\n' "$SRB_YARN_MIRROR" > .yarnrc; \
    yarn install --offline --frozen-lockfile --ignore-scripts \
        --non-interactive --no-progress >/dev/null; \
    for p in vue vue-router vuex @vue/cli-service eslint-plugin-prettier; do \
      test -d "node_modules/$p" \
        || { echo "FATAL: offline install produced no $p"; exit 1; }; \
    done; \
    echo "State A installs offline ($(ls node_modules | wc -l) entries)"; \
    cd /; rm -rf "$d"

# --- The browser -------------------------------------------------------------
# The same Playwright and the same Chromium the agent could observe with, at the
# same path. Installed at /opt rather than globally because the harness modules
# are ESM: `import "playwright"` from /opt/harness resolves only by the loader
# walking up to /opt/node_modules.
RUN printf '{"name":"swerefactor-tooling","private":true,"version":"1.0.0"}\n' > /opt/package.json \
 && cd /opt \
 && NPM_CONFIG_OFFLINE=false npm i --loglevel=error playwright@1.49.1 \
 && /opt/node_modules/.bin/playwright install --with-deps chromium

# ESM resolution walks up the directory chain and nothing else: no NODE_PATH, no
# global root. From /tests/behavioural/lib/harness that chain is /tests/behavioural,
# /tests, / -- and /opt is not on it, which is why the same `import "playwright"`
# that resolves from the agent's /opt/harness would fail here. One symlink rather
# than a second 300MB install of the same browser driver.
# mkdir first: the suite is copied in below, so /tests does not exist yet.
RUN mkdir -p /tests && ln -s /opt/node_modules /tests/node_modules

# --- The suite's Python ------------------------------------------------------
# A test runner and nothing else: no HTTP client, no framework. This stage's
# Python never speaks to the app -- Node drives both bundles and writes two JSON
# files, and every module here compares them.
COPY requirements-runner.txt /opt/requirements-runner.txt
RUN python3 -m pip install --break-system-packages \
        --require-hashes --no-deps -r /opt/requirements-runner.txt \
 && python3 -c "import pytest, pytest_timeout; print('runner stack ok')"

# --- The harness -------------------------------------------------------------
COPY --from=infra /opt/swerefactor /opt/swerefactor
ENV PYTHONPATH=/opt/swerefactor

# --- The suite ---------------------------------------------------------------
# Copied last so iterating on a module does not rebuild the cache or the browser.
COPY . /tests/behavioural

RUN chmod +x /tests/behavioural/lib/run-pytest.sh /tests/behavioural/modules/*/run.sh

# The observation harness must be the one the agent was given, or "both sides were
# driven the same way" is a sentence about two different drivers. The digest list
# is generated from environment/harness/ and shipped in data/; `swerefactor validate`
# checks this copy of it against the environment's, and this line checks the files
# against it.
RUN cd /tests/behavioural/lib/harness \
 && sha256sum -c /tests/behavioural/data/harness-core.sha256 \
 && echo "harness core matches the copy the agent was given"

# The State A bundle, checked here rather than only at run time: an image whose
# tarball is truncated should fail to build, not fail every submission.
RUN cd /tests/behavioural/data && sha256sum -c oracle-dist.tgz.sha256

# The manifest is the file most likely to be edited and least likely to be
# tested, so a malformed weight, a duplicate id or a module directory that does
# not exist fails the build rather than the run.
RUN python3 -c "\
import sys; from swerefactor import config; \
s = config.Suite.load('/tests/behavioural/suite.toml'); \
missing = [m.id for m in s.modules if not (s.root / m.dir).is_dir()]; \
sys.exit(f'suite.toml names missing module dir(s): {missing}') if missing else None; \
groups = {m.env.get('SRB_GROUP', '') for m in s.modules if m.command[-1] == 'run.sh'}; \
print(f'suite: {len(s.modules)} modules, weight {s.weight_total:g}, groups {sorted(groups)}')"

# The scenario corpus has to import, and its group names have to be the ones
# suite.toml dispatches on. A module whose SRB_GROUP matches no scenario would run
# zero checks and report a rate of zero -- a silent failure that looks like a
# broken submission.
RUN node --input-type=module -e "\
import {SCENARIOS} from '/tests/behavioural/lib/harness/scenarios.mjs'; \
import {readFileSync} from 'node:fs'; \
const groups = new Set(SCENARIOS.map(s => s.group)); \
const toml = readFileSync('/tests/behavioural/suite.toml', 'utf8'); \
const declared = [...toml.matchAll(/SRB_GROUP = \"([a-z]*)\"/g)].map(m => m[1]).filter(Boolean); \
const unknown = declared.filter(g => !groups.has(g)); \
if (unknown.length) throw new Error('suite.toml dispatches on unknown group(s): ' + unknown); \
const uncovered = [...groups].filter(g => !declared.includes(g)); \
if (uncovered.length) throw new Error('no module scores group(s): ' + uncovered); \
console.log(\`corpus: \${SCENARIOS.length} scenarios in \${groups.size} groups, all scored\`)"

# The oracle drive, at build time, over the whole corpus. This is the expensive
# line in this file and it earns it twice over: it proves Chromium runs in this
# image, that the harness can serve and drive the State A bundle here, and that
# all 100 scenarios complete cleanly -- the exact precondition `build` treats as a
# grader-side fault. A Chromium that cannot start in this image would otherwise
# surface as every submission scoring zero.
#
# Nothing is kept. No expectation is recorded, no observation is baked into a
# layer: only the fact that it worked.
RUN set -eu; \
    mkdir -p /tmp/smoke; \
    tar -C /tmp/smoke -xzf /tests/behavioural/data/oracle-dist.tgz; \
    cd /tests/behavioural; \
    node lib/harness/observe.mjs /tmp/smoke/dist-stateA /tmp/smoke/out.json \
        --nonce buildsmoke; \
    python3 -c "\
import json, sys; \
d = json.load(open('/tmp/smoke/out.json')); \
bad = [s['id'] for s in d['scenarios'] if not s.get('observed') or s.get('stepErrors')]; \
sys.exit(f'State A does not drive cleanly in this image: {bad}') if bad else None; \
n = sum(len(s['observed']['nodes']) for s in d['scenarios']); \
print(f'oracle smoke: {len(d[\"scenarios\"])} scenarios, {n} nodes observed')"; \
    rm -rf /tmp/smoke

RUN mkdir -p /logs/verifier

WORKDIR /tests/behavioural
