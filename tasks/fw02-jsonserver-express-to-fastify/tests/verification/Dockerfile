# =============================================================================
# SWERefactorBench -- fw02-jsonserver-express-to-fastify
# Stage 3 image: the verification probe.
#
#     docker build -t srb/fw02-verification:1 tasks/fw02-.../tests/verification
#
# Six models, an hour each, trying to write a test State A passes and the
# submission fails. So this is the only image in the task that has to run BOTH
# applications: json-server 0.17.4 on Express 4 with its whole connect middleware
# stack, and the submission on whatever it declares. It therefore carries two
# registry mirrors, and run-candidate.sh chooses per tree.
#
# That is not a loophole. The mirrors are separate directories with separate npm
# caches, a submission is installed only from the delivery mirror -- the same one
# stage 2 uses, with no packument for any of the 54 retired names -- and the build
# proves below that express is not installable through the submission's half. What
# this image adds is the ability to ask State A a question at grading time, which
# is the whole point of the stage.
#
# The candidate's own test stack is a third, separate thing: one venv, built once,
# shared by both trees. pytest and the standard library, no HTTP client. A
# candidate running under two different pytests would not be comparing two
# services, and a client that decompressed a body for it would hide the
# differences most worth finding here.
#
# At runtime the harness provides:
#
#     /workspace/repo    the submission                            (read-only)
#     /opt/original      State A, unpacked from original.tar.gz    (read-only)
#     /logs/verifier     where the stage result JSON is written
#
# The stage copies both trees before installing or serving anything, so what stage
# 2 measured is not what stage 3 mutates, and what the adversary reads does not
# change under it between one try_test and the next.
#
# Network: reaches the model API for the six rounds and the adjudicator, and
# nothing else. Both installs go to a loopback mirror, and candidate tests are
# handed SRB_NETWORK=denied -- a statement of intent rather than a sandbox, since a
# test that needs the internet is not measuring this migration.
#
# Same base digest as the environment image and as stage 2, so the two services
# this stage compares run on the same Node the agent developed against.
# =============================================================================

ARG INFRA_IMAGE=swerefactor/infra:1
FROM ${INFRA_IMAGE} AS infra

FROM node@sha256:6c74791e557ce11fc957704f6d4fe134a7bc8d6f5ca4403205b2966bd488f6b3

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=UTC \
    NODE_ENV= \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=0 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        file \
        jq \
        procps \
        python3 \
        python3-pip \
        python3-venv \
        util-linux \
    && rm -rf /var/lib/apt/lists/*

# --- The candidate's test stack, one version for both trees -------------------
# Built once here rather than per tree, which is the one simplification this task
# gets over the Python-subject tasks: the service is Node, the candidate is
# Python, and the two never share an environment. So there is nothing a tree could
# do to this venv and no reason to rebuild it 24 times.
COPY requirements-candidate.txt /opt/pins/requirements-candidate.txt

RUN python3 -m venv /opt/venv-candidate \
 && /opt/venv-candidate/bin/pip install --no-cache-dir --require-hashes \
        -r /opt/pins/requirements-candidate.txt \
 && /opt/venv-candidate/bin/python -c \
        "import pytest, pytest_timeout; print('candidate pytest', pytest.__version__)" \
 # The absence is worth asserting, because it is the reason the list is short: a
 # candidate that could import httpx would get a transparently decompressed body
 # and a normalised header dict, and would then be unable to see most of what it
 # is here to look for.
 && ! /opt/venv-candidate/bin/python -c "import httpx" 2>/dev/null \
 && ! /opt/venv-candidate/bin/python -c "import requests" 2>/dev/null \
 && echo "candidate venv: pytest + stdlib only"

# --- The harness's own interpreter -------------------------------------------
# Runs the six rounds and the adjudicator. Kept apart from the venv that runs
# candidate code: the process orchestrating the stage must not share an
# environment with code an adversary wrote. swerefactor itself needs only the
# standard library, so this venv installs nothing.
RUN python3 -m venv /opt/venv \
 && /opt/venv/bin/python -c "print('harness venv ok')"

ENV VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin

# --- Two registry mirrors ----------------------------------------------------
# Built from the same two lockfiles and the same ban list the environment image
# and stage 2 use, so State A here is bit-identical to the oracle the agent
# developed against, and "installs in the grader" cannot diverge between stages.
# Network is available during this build; it is not available when the mirrors are
# used.
COPY data/build-mirror.mjs     /opt/srb/build-mirror.mjs
COPY data/serve-registry.mjs   /opt/srb/serve-registry.mjs
COPY data/lock-baseline.json   /opt/srb/lock-baseline.json
COPY data/lock-target.json     /opt/srb/lock-target.json
COPY data/manifest-target.json /opt/srb/manifest-target.json
COPY data/retired-packages.txt /opt/srb/retired-packages.txt
COPY data/db.seed.json         /opt/srb/db.seed.json
COPY data/routes.json          /opt/srb/routes.json
COPY data/srb-npm              /usr/local/bin/srb-npm

# The same disjointness check the other two images make: a name that is both
# retired and required by State B would surface later as an unexplained 404
# during warming. Fail here instead, naming it.
RUN node -e ' \
    const fs = require("fs"); \
    const banned = new Set(fs.readFileSync("/opt/srb/retired-packages.txt", "utf8") \
      .split("\n").map((l) => l.replace(/#.*$/, "").trim()).filter(Boolean)); \
    const lock = JSON.parse(fs.readFileSync("/opt/srb/lock-target.json", "utf8")); \
    const clash = []; \
    for (const key of Object.keys(lock.packages || {})) { \
      if (!key) continue; \
      const name = key.split("node_modules/").pop(); \
      if (banned.has(name)) clash.push(name); \
    } \
    if (clash.length) { \
      console.error("FATAL: the retired list bans names the State B closure needs: " \
                    + [...new Set(clash)].sort().join(" ")); \
      process.exit(1); \
    } \
    console.log("ban list and target closure are disjoint (" + banned.size + " retired)"); \
  '

# Baseline first, unbanned: this is the only place in the benchmark where express
# is installable, and it exists so State A can be booted and asked a question.
# Delivery second, with the ban applied, from both lockfiles: identical to stage
# 2's mirror by construction, because it is the same command.
RUN chmod +x /usr/local/bin/srb-npm \
 && node /opt/srb/build-mirror.mjs \
        /opt/srb/lock-baseline.json \
        /opt/npm-registry-baseline \
 && node /opt/srb/build-mirror.mjs \
        --ban /opt/srb/retired-packages.txt \
        /opt/srb/lock-baseline.json /opt/srb/lock-target.json \
        /opt/npm-registry \
 && test -e /opt/npm-registry-baseline/express.json \
 && test -e /opt/npm-registry-baseline/body-parser.json \
 && test ! -e /opt/npm-registry/express.json \
 && test ! -e /opt/npm-registry/body-parser.json \
 && test ! -e /opt/npm-registry/middie.json \
 && test ! -e '/opt/npm-registry/@fastify/express.json' \
 && test -e /opt/npm-registry/fastify.json \
 && du -sh /opt/npm-registry-baseline /opt/npm-registry

# --- Two npm caches ----------------------------------------------------------
# One per mirror, and this is not an optimisation. npm caches registry responses,
# and a cache shared between the two roles would be a cache in which the
# baseline's express packument had already been stored -- so the submission's
# install could be served, from a local file, a package its own mirror answers 404
# for. Separate caches make that impossible rather than improbable.
#
# Warming them is the optimisation, and a large one: State A's closure is 1064
# packages and six rounds share one install of it, but the first candidate of
# the stage pays for that install inside its own timeout. Warming also proves in
# the build that each mirror can satisfy the closure it is for -- a failure here
# costs a build, where the same failure at grading time would cost six rounds.
#
# The baseline manifest is derived from the lockfile's own root entry rather than
# shipped as a second file: `npm ci` requires the two to agree, and the way to
# guarantee they agree is to not have two.
RUN mkdir -p /opt/warm-baseline \
 && node -e ' \
    const fs = require("fs"); \
    const lock = JSON.parse(fs.readFileSync("/opt/srb/lock-baseline.json", "utf8")); \
    const root = lock.packages[""]; \
    if (!root) { console.error("lock-baseline.json has no root package entry"); process.exit(1); } \
    fs.writeFileSync("/opt/warm-baseline/package.json", JSON.stringify(root, null, 2)); \
    console.log("baseline manifest: " + Object.keys(root.dependencies || {}).length \
                + " deps, " + Object.keys(root.devDependencies || {}).length + " dev"); \
  ' \
 && cp /opt/srb/lock-baseline.json /opt/warm-baseline/package-lock.json \
 && cd /opt/warm-baseline \
 && SRB_REGISTRY_ROOT=/opt/npm-registry-baseline npm_config_cache=/opt/npm-cache-baseline \
        srb-npm ci \
 && test -d /opt/warm-baseline/node_modules/express \
 && test -x /opt/warm-baseline/node_modules/.bin/babel \
 && rm -rf /opt/warm-baseline

RUN mkdir -p /opt/warm \
 && cp /opt/srb/manifest-target.json /opt/warm/package.json \
 && cp /opt/srb/lock-target.json     /opt/warm/package-lock.json \
 && cd /opt/warm \
 && SRB_REGISTRY_ROOT=/opt/npm-registry npm_config_cache=/opt/npm-cache \
        srb-npm ci \
 && test -d /opt/warm/node_modules/fastify \
 && rm -rf /opt/warm

# The submission's half of this image must refuse the retired stack, warm cache
# and all. If it did not, a submission could reinstall Express here and be graded
# on the old implementation -- and unlike stage 2, this image has an express
# packument sitting on the same disk, so the assertion is worth making after the
# caches exist rather than before.
RUN mkdir -p /opt/assert && cd /opt/assert \
 && for pkg in express body-parser middie @fastify/express; do \
        printf '{"name":"a","version":"1.0.0","dependencies":{"%s":"*"}}\n' "$pkg" \
            > package.json; \
        if SRB_REGISTRY_ROOT=/opt/npm-registry npm_config_cache=/opt/npm-cache \
                srb-npm install --package-lock-only > /tmp/x.log 2>&1; then \
            echo "FATAL: $pkg is installable through the delivery mirror" >&2; \
            exit 1; \
        fi; \
        grep -qi '404' /tmp/x.log \
            && echo "delivery mirror refuses $pkg (404)"; \
        rm -f package-lock.json; \
    done \
 # And the baseline half must still be able to install it, or the original cannot
 # boot and every round reports "does not pass on the original".
 && printf '{"name":"a","version":"1.0.0","dependencies":{"express":"^4.17.1"}}\n' \
        > package.json \
 && SRB_REGISTRY_ROOT=/opt/npm-registry-baseline \
        npm_config_cache=/opt/npm-cache-baseline \
        srb-npm install --package-lock-only > /tmp/y.log 2>&1 \
 && echo "baseline mirror still serves express" \
 && cd / && rm -rf /opt/assert /tmp/x.log /tmp/y.log

ENV SRB_RETIRED_PACKAGES=/opt/srb/retired-packages.txt \
    SRB_NPM=/usr/local/bin/srb-npm

# --- The shared grader -------------------------------------------------------
COPY --from=infra /opt/swerefactor /opt/swerefactor
ENV PYTHONPATH=/opt/swerefactor

# --- The probe ---------------------------------------------------------------
# Copied last so iterating on the prompt does not rebuild two mirrors.
COPY . /tests/verification

RUN chmod +x /tests/verification/run-candidate.sh \
 && bash -n /tests/verification/run-candidate.sh \
 # The grader's data has to be where run-candidate.sh looks for it. Both trees are
 # booted against these, and a missing seed would make every round fail on both
 # trees for a reason no report would explain.
 && test -f /opt/srb/db.seed.json \
 && test -f /opt/srb/routes.json \
 && node -e 'const d=require("/opt/srb/db.seed.json"); \
             const keys=Object.keys(d).filter(k=>!k.startsWith("$")); \
             if (keys.length < 5) { console.error("seed has too few resources"); process.exit(1); } \
             console.log("seed: " + keys.length + " resources -- " + keys.join(" "))' \
 && node -e 'const r=require("/opt/srb/routes.json"); \
             const n=Object.keys(r).length; \
             if (!n) { console.error("routes.json declares no rules"); process.exit(1); } \
             console.log("routes: " + n + " rewrite rule(s)")' \
 # A scope-less probe is unwinnable and a mismatched adversary count creates points
 # nobody can earn. Checked here as well as by `swerefactor validate`, because this is
 # the image that would otherwise discover it six rounds in.
 && /opt/venv/bin/python /tests/verification/check-probe.py

RUN mkdir -p /opt/original /workspace /logs/verifier

WORKDIR /tests/verification
