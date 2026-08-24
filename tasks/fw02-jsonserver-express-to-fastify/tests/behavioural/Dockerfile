# =============================================================================
# SWERefactorBench -- fw02-jsonserver-express-to-fastify
# Stage 2 image: the behavioural suite.
#
#     docker build -t srb/fw02-behavioural:1 tasks/fw02-.../tests/behavioural
#
# The build context is this directory, so the suite is in the image and nothing
# else is. The agent container never sees it.
#
# What makes this a measurement rather than a claim: the image carries its own
# registry mirror -- built from the same pinned lockfiles the agent's was, with
# the retirement left in rather than applied -- and the `install` module copies
# the submission out of /workspace/repo, drops every build artefact that arrived
# with it, and installs from that mirror before any other module runs. A vendored
# node_modules cannot survive the trip, and a submission whose package.json does
# not resolve cannot borrow a working install from the environment image.
#
# There are two mirrors here, and which one is which is the whole point:
#
#   /opt/npm-registry          COMPLETE -- Express and the other 53 retired
#                              distributions included. `install` resolves against
#                              this one, so this is what stage 2 grades.
#   /opt/npm-registry-pruned   the agent's mirror reproduced, the 54 removed.
#                              Installed from by nothing; read once, unscored.
#
# The complete one is what grades because State A is the repository every recorded
# expectation in this suite came from, and State A depends on Express. A stage that
# cannot install State A cannot show its own expectations are satisfiable, and a
# failure in it stops meaning "behaviour changed" and starts meaning "was not
# handed Express".
#
# Withholding Express from the *agent* is what enforces the migration, and that is
# untouched: it lives in environment/, whose mirror is built with --ban. A
# submission that quietly kept Express could never have been installed, started or
# tested while the work was being done. Giving the grader the old stack costs
# nothing, because the grader is not where the constraint has to bite -- and
# whether the retirement actually happened is stage 1's question, asked with both
# trees open, where a reviewer can see the difference between "still imports
# Express" and "cannot resolve Express".
#
# At runtime the harness provides:
#
#     /workspace/repo    the submission                      (read-write copy)
#     /logs/verifier     where the stage result JSON is written
#
# Note what is *not* mounted: State A's source. This stage talks to a running
# server and never reads a repository, so it has no use for it. Stage 1 gets both
# trees and cannot start either; stage 3 gets both trees and both builds. The
# capability split is the design.
#
# Same base digest as the environment image, so the submission runs on the Node
# it was developed against.
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
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        jq \
        procps \
        file \
        python3 \
        python3-pip \
        python3-venv \
    && rm -rf /var/lib/apt/lists/*

# --- The grader's own Python stack, hash-verified ----------------------------
# The suite speaks HTTP through the standard library, so this is pytest and its
# four dependencies and nothing else -- no httpx, no requests. Nothing installed
# here can drift into the behaviour being graded, because the grader's stack and
# the submission's stack never meet: one is Python talking to a socket, the other
# is Node answering on it.
COPY requirements-grader.txt /opt/pins/requirements-grader.txt

RUN python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --require-hashes \
        -r /opt/pins/requirements-grader.txt \
 && /opt/venv/bin/python -c "import pytest, pytest_timeout; print('pytest', pytest.__version__)"

ENV PATH=/opt/venv/bin:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin

# --- The registry mirror -----------------------------------------------------
# Rebuilt from the same two lockfiles and the same ban list the environment image
# used, so "installs in the agent container" and "installs in the grader" cannot
# diverge. Network is available during this build; it is not available when the
# mirror is used.
COPY data/build-mirror.mjs     /opt/srb/build-mirror.mjs
COPY data/serve-registry.mjs   /opt/srb/serve-registry.mjs
COPY data/lock-baseline.json   /opt/srb/lock-baseline.json
COPY data/lock-target.json     /opt/srb/lock-target.json
COPY data/manifest-target.json /opt/srb/manifest-target.json
COPY data/retired-packages.txt /opt/srb/retired-packages.txt
COPY data/srb-npm              /usr/local/bin/srb-npm

# The same disjointness check the environment image makes: a name that is both
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

COPY data/prune-mirror.mjs /opt/srb/prune-mirror.mjs

# Two mirrors, from one warming pass.
#
#   /opt/npm-registry          COMPLETE: every version either lockfile resolves,
#                              Express included. What `install` resolves against,
#                              and therefore what stage 2 grades.
#   /opt/npm-registry-pruned   the same bytes with the 54 retired distributions
#                              removed. The agent's mirror, reproduced. Installed
#                              from by nothing; read once, unscored.
#
# The pruned copy is taken from the complete one rather than warmed a second time
# with --ban, because two warming passes can fetch different bytes for a version
# that was re-published between them, and the two mirrors would then disagree
# about more than the retirement.
RUN chmod +x /usr/local/bin/srb-npm \
 && node /opt/srb/build-mirror.mjs \
        /opt/srb/lock-baseline.json /opt/srb/lock-target.json \
        /opt/npm-registry \
 && test -e /opt/npm-registry/express.json \
 && test -e /opt/npm-registry/fastify.json \
 && cp -a /opt/npm-registry /opt/npm-registry-pruned \
 && node /opt/srb/prune-mirror.mjs \
        /opt/npm-registry-pruned /opt/srb/retired-packages.txt \
 && test ! -e /opt/npm-registry-pruned/express.json \
 && test ! -e /opt/npm-registry-pruned/body-parser.json \
 && test ! -e /opt/npm-registry-pruned/@fastify/express.json \
 && test ! -e /opt/npm-registry-pruned/_tarballs/express \
 && test ! -e /opt/npm-registry-pruned/_tarballs/body-parser \
 && test -e /opt/npm-registry/_tarballs/express \
 && test -e /opt/npm-registry-pruned/fastify.json \
 && test -e /opt/npm-registry-pruned/_tarballs/fastify \
 && du -sh /opt/npm-registry /opt/npm-registry-pruned

# Warm the cache so grading is not dominated by unpacking, and prove in the build
# that this mirror can satisfy the target closure. The mirror serves live
# packuments on loopback, so a submission's own version ranges resolve against it
# the same way they resolved in the agent container -- the warm cache is a speed
# measure, not part of the resolution.
RUN mkdir -p /opt/warm \
 && cp /opt/srb/manifest-target.json /opt/warm/package.json \
 && cp /opt/srb/lock-target.json     /opt/warm/package-lock.json \
 && cd /opt/warm \
 && npm config set cache /opt/npm-cache --global \
 && srb-npm ci \
 && test -d /opt/warm/node_modules/fastify \
 && rm -rf /opt/warm/node_modules /opt/warm/package.json /opt/warm/package-lock.json \
 # And State A's own closure, for the same reason: the identity case -- State A
 # submitted unchanged -- is the one every scored expectation here was recorded
 # from, and it should not be the slowest install the suite ever does.
 && cp /opt/srb/lock-baseline.json /opt/warm/package-lock.json \
 && node -e ' \
    const fs = require("fs"); \
    const lock = JSON.parse(fs.readFileSync("/opt/warm/package-lock.json", "utf8")); \
    fs.writeFileSync("/opt/warm/package.json", JSON.stringify({ \
      name: lock.name || "warm-baseline", version: lock.version || "1.0.0", \
      dependencies: (lock.packages && lock.packages[""] \
        && lock.packages[""].dependencies) || {}, \
      devDependencies: (lock.packages && lock.packages[""] \
        && lock.packages[""].devDependencies) || {}, \
    })); \
  ' \
 && cd /opt/warm && srb-npm ci \
 && test -d /opt/warm/node_modules/express \
 && echo "warm: the complete mirror satisfies State A as well as State B" \
 && cd / && rm -rf /opt/warm

# Each mirror must have exactly the role it was built for, and the assertion is
# two-sided because both halves have been wrong at least once.
#
# The complete mirror must resolve Express. If it did not, State A -- the
# repository every recorded expectation in this suite came from -- could not be
# installed, `install` is required, and the whole stage would score 0 for the
# unmodified repository. Measured before this was fixed: exactly that.
#
# The pruned mirror must refuse it, because it is the claim that the agent's
# environment withheld the old stack while the work was being done. It is
# installed from by nothing here; the enforcement it stands for happens in the
# environment image, whose mirror is built with --ban.
#
# This probes *range resolution*, which the warm step above does not: `ci` reads
# pinned versions straight out of a lockfile, so it would still pass against a
# mirror whose packuments had their version lists pruned down to one entry. Here
# npm has to pick a version out of a range, the way an install from a manifest
# does.
#
# Both ranges are read out of the lockfile that declares them. A literal typed
# here is a second copy of a fact that lives somewhere else, and it went stale
# exactly as you would expect: this step asked for fastify "^4.0.0" against a
# closure that had moved to 5.10.0, and the build died as though the mirror were
# broken.
RUN set -eu; \
    mkdir -p /opt/assert; cd /opt/assert; \
    range() { node -pe 'const l=JSON.parse(require("fs").readFileSync(process.argv[1],"utf8"));const r=(l.packages[""].dependencies||{})[process.argv[2]];if(!r){console.error("no root dependency "+process.argv[2]+" in "+process.argv[1]);process.exit(1)}r' "$1" "$2"; }; \
    express_range=$(range /opt/srb/lock-baseline.json express); \
    fastify_range=$(range /opt/srb/lock-target.json fastify); \
    probe() { \
        rm -rf node_modules package-lock.json; \
        printf '{"name":"a","version":"1.0.0","dependencies":{"%s":"%s"}}\n' "$1" "$2" \
            > package.json; \
    }; \
    probe express "$express_range"; \
    if ! srb-npm install --package-lock-only > /tmp/p.log 2>&1; then \
        echo "FATAL: express $express_range does not resolve in the COMPLETE mirror;" >&2; \
        echo "       State A is the repository every recorded expectation came from," >&2; \
        echo "       install is a required module, and the stage would score 0 for it" >&2; \
        tail -20 /tmp/p.log >&2; exit 1; \
    fi; \
    echo "complete mirror resolves express $express_range (State A can be installed)"; \
    probe fastify "$fastify_range"; \
    if ! srb-npm install --package-lock-only > /tmp/p.log 2>&1; then \
        echo "FATAL: fastify $fastify_range does not resolve in the COMPLETE mirror" >&2; \
        tail -20 /tmp/p.log >&2; exit 1; \
    fi; \
    echo "complete mirror resolves fastify $fastify_range (State B can be installed)"; \
    for retired in express @fastify/express; do \
        probe "$retired" '*'; \
        if SRB_REGISTRY_ROOT=/opt/npm-registry-pruned \
           srb-npm install --package-lock-only > /tmp/p.log 2>&1; then \
            echo "FATAL: $retired is installable from the PRUNED mirror," >&2; \
            echo "       so the pruned copy does not reproduce the agent's environment" >&2; \
            exit 1; \
        fi; \
        if ! grep -qiE '404|not found' /tmp/p.log; then \
            echo "FATAL: the pruned mirror refused $retired for an unexpected reason;" >&2; \
            echo "       it has to be absent, not broken" >&2; \
            tail -20 /tmp/p.log >&2; exit 1; \
        fi; \
        echo "pruned mirror has no $retired (404)"; \
    done; \
    cd /; rm -rf /opt/assert /tmp/p.log

ENV SRB_REGISTRY_ROOT=/opt/npm-registry \
    SRB_REGISTRY_PRUNED=/opt/npm-registry-pruned \
    SRB_RETIRED_PACKAGES=/opt/srb/retired-packages.txt \
    SRB_NPM=/usr/local/bin/srb-npm \
    npm_config_cache=/opt/npm-cache

# --- The shared grader -------------------------------------------------------
COPY --from=infra /opt/swerefactor /opt/swerefactor
ENV PYTHONPATH=/opt/swerefactor

# --- The suite ---------------------------------------------------------------
# Copied last so iterating on a module does not rebuild the mirror.
COPY . /tests/behavioural

RUN chmod +x /tests/behavioural/lib/run-pytest.sh /tests/behavioural/modules/*/run.sh \
 && test -f /tests/behavioural/data/recorded-responses.json \
 && test -f /tests/behavioural/data/db.seed.json \
 && test -f /tests/behavioural/data/routes.json \
 # The request list and the recording have to agree, and they agree by
 # fingerprint rather than by count -- editing a path without re-recording State
 # A changes the fingerprint and fails here rather than at grading time.
 && /opt/venv/bin/python -c "\
import json, sys; sys.path.insert(0, '/tests/behavioural/lib'); \
from harness import exchanges; \
rec = json.load(open('/tests/behavioural/data/recorded-responses.json')); \
want = exchanges.fingerprint(); \
got = rec.get('exchanges_fingerprint'); \
sys.exit(f'recording is for a different request list: {got} != {want}') if got != want else None; \
n = sum(len(s.cases) for s in exchanges.SESSIONS); \
print(f'exchanges: {n} cases in {len(exchanges.SESSIONS)} sessions, fingerprint {want}')" \
 # The manifest is the thing most likely to be edited and least likely to be
 # tested, so it is checked at build time: a malformed weight, a duplicate id or
 # a module directory that does not exist fails the build, not the run.
 && /opt/venv/bin/python -c "\
import sys; from swerefactor import config; \
s = config.Suite.load('/tests/behavioural/suite.toml'); \
missing = [m.id for m in s.modules if not (s.root / (m.dir or f'modules/{m.id}')).is_dir()]; \
sys.exit(f'suite.toml names missing module dir(s): {missing}') if missing else None; \
print(f'suite: {len(s.modules)} modules, weight {s.weight_total:g}')"

WORKDIR /tests/behavioural
