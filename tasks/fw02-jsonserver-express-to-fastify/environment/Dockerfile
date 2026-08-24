# =============================================================================
# SWERefactorBench -- fw02-jsonserver-express-to-fastify
# Agent environment image.
#
# Build-time responsibilities (network IS available during build; the trial
# itself runs with network_mode = "no-network"):
#
#   1. Verify + extract the frozen State A repo snapshot to /workspace/repo.
#   2. Initialise a git repo containing exactly ONE baseline commit.
#   3. Build two offline npm registry mirrors from pinned lockfiles:
#        - /opt/npm-registry-baseline : the complete State A closure, Express
#                                      included.  Used once, to install the
#                                      read-only reference oracle, then deleted.
#        - /opt/npm-registry          : the delivery mirror.  Everything the
#                                      migrated project may need, and NOT ONE of
#                                      the 54 retired packages.  This is also
#                                      the verifier's mirror.
#   4. Install the frozen State A oracle at /opt/oracle (read-only), build it,
#      and prove it serves.
#   5. Warm the npm cache for the delivery closure so the agent's first install
#      is fast, and prove `express` is unobtainable.
#
# /workspace/repo ships WITHOUT node_modules, exactly as State A's dependencies
# are unobtainable from the delivery mirror: `npm install` on the shipped
# manifest fails with a 404 naming express.  That is the constraint, discovered
# in the first minute rather than at grading time.
#
# The image pins its base by digest so it can never drift.
# =============================================================================

FROM node@sha256:6c74791e557ce11fc957704f6d4fe134a7bc8d6f5ca4403205b2966bd488f6b3

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=UTC \
    NODE_ENV=

# --- OS packages -------------------------------------------------------------
# curl: so the agent can drive its own server over HTTP (node:slim has none).
# git: required for the baseline snapshot commit.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        jq \
        procps \
        less \
        vim-tiny \
        file \
        xxd \
        python3 \
    && rm -rf /var/lib/apt/lists/*

# =============================================================================
# 1. Unpack the frozen State A snapshot into /workspace/repo.
#
# The tarball is content-addressed: the build refuses to continue if the
# snapshot does not match its recorded digest, so every trial in every run
# starts from bit-identical sources.
# =============================================================================
COPY original.tar.gz /opt/snapshot/original.tar.gz
COPY original.sha256 /opt/snapshot/original.sha256

RUN cd /opt/snapshot \
 && sha256sum -c original.sha256 \
 && mkdir -p /workspace \
 && tar xzf original.tar.gz -C /workspace \
 && test -f /workspace/repo/package.json \
 && test -f /workspace/repo/src/server/router/plural.js \
 && test -f /workspace/repo/serve.sh \
 && test -f /workspace/repo/db.seed.json \
 && test ! -e /workspace/repo/node_modules \
 && ! find /workspace/repo -name '.git' -o -name '.hg' -o -name '.svn' | grep -q . \
 && cp -a /workspace/repo /opt/oracle

# =============================================================================
# 2. Initialise a git repo holding exactly one baseline commit.
#
# The agent gets `git diff` / `git stash` / `git checkout -- .` against State A
# and nothing else.  There is no history to mine and no remote to fetch.
# =============================================================================
RUN cd /workspace/repo \
 && git init -q -b main \
 && git config user.email "bench@swerefactor.local" \
 && git config user.name  "SWERefactorBench" \
 && git config commit.gpgsign false \
 && git add -A \
 && git commit -q -m "Baseline snapshot: json-server 0.17.4 (Express 4)" \
 && git tag baseline \
 && test "$(git rev-list --count HEAD)" = "1"

# =============================================================================
# 3. Build the two registry mirrors.
#
# Both are built from lockfiles pinned at construction time, so the image build
# resolves nothing: every (name, version) is fixed, and every tarball is fetched
# by its recorded URL.  The delivery mirror additionally applies the ban list --
# a banned package has no packument at all, so npm reports a hard 404.
# =============================================================================
COPY build-mirror.mjs     /opt/srb/build-mirror.mjs
COPY serve-registry.mjs   /opt/srb/serve-registry.mjs
COPY lock-baseline.json   /opt/srb/lock-baseline.json
COPY lock-target.json     /opt/srb/lock-target.json
COPY manifest-target.json /opt/srb/manifest-target.json
COPY retired-packages.txt /opt/srb/retired-packages.txt

# The ban list and the target closure must not intersect: a retired name that is
# also something State B needs would make the target manifest uninstallable, and
# the symptom would be an unexplained 404 during cache warming rather than a
# named contradiction. Check it here, where the message can say which name.
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

RUN node /opt/srb/build-mirror.mjs \
        /opt/srb/lock-baseline.json \
        /opt/npm-registry-baseline \
 && node /opt/srb/build-mirror.mjs \
        --ban /opt/srb/retired-packages.txt \
        /opt/srb/lock-baseline.json /opt/srb/lock-target.json \
        /opt/npm-registry \
 && test ! -e /opt/npm-registry/express.json \
 && test ! -e /opt/npm-registry/middie.json \
 && test ! -e /opt/npm-registry/@fastify/express.json \
 && test -e /opt/npm-registry-baseline/express.json \
 && test -e /opt/npm-registry/fastify.json \
 && du -sh /opt/npm-registry-baseline /opt/npm-registry

# =============================================================================
# 4. The reference oracle: json-server 0.17.4 on Express 4, read-only.
#
# Installed from the BASELINE mirror -- the only place Express exists in this
# image -- with its own node_modules and its own build output.  It keeps
# answering exactly as State A did no matter how /workspace/repo is rewritten,
# so it is the behavioural ground truth the agent diffs against.
# =============================================================================
COPY srb-npm /usr/local/bin/srb-npm
RUN chmod +x /usr/local/bin/srb-npm

RUN cd /opt/oracle \
 && SRB_REGISTRY_ROOT=/opt/npm-registry-baseline srb-npm ci \
 && ./node_modules/.bin/babel src -d lib \
 && test -f lib/cli/bin.js \
 && node -e "const s=require('/opt/oracle/lib/server'); \
             const app=s.create(); \
             if (typeof app.listen !== 'function') throw new Error('oracle broken'); \
             console.log('oracle express app ok')" \
 && rm -rf /opt/oracle/.git

# Prove the oracle actually serves before freezing it: a broken oracle would
# silently degrade every task it is supposed to arbitrate.
RUN set -eu; \
    cd /opt/oracle; \
    db=$(mktemp -t oracle-boot-XXXXXX.json); cp db.seed.json "$db"; \
    node ./lib/cli/bin.js "$db" --host 127.0.0.1 --port 8899 \
        --routes /opt/oracle/routes.json --quiet & \
    pid=$!; \
    ok=0; \
    for _ in $(seq 1 60); do \
        if curl -sf http://127.0.0.1:8899/posts/1 > /dev/null 2>&1; then ok=1; break; fi; \
        sleep 0.5; \
    done; \
    if [ "$ok" = 1 ]; then \
        curl -s 'http://127.0.0.1:8899/posts?_limit=1' | head -c 80; echo; \
        curl -s -o /dev/null -w 'oracle /db -> %{http_code}\n' http://127.0.0.1:8899/db; \
    fi; \
    kill $pid 2>/dev/null || true; \
    rm -f "$db"; \
    test "$ok" = 1

RUN chmod -R a-w /opt/oracle \
 && rm -rf /opt/npm-registry-baseline

# =============================================================================
# 5. Warm the npm cache for the delivery closure.
#
# The agent's first install then unpacks from a local cache instead of walking
# the whole mirror, and -- more usefully -- this build step is where a mirror
# that cannot satisfy the target manifest would be caught, rather than in a
# trial container with no network.
# =============================================================================
RUN mkdir -p /opt/warm \
 && cp /opt/srb/manifest-target.json /opt/warm/package.json \
 && cp /opt/srb/lock-target.json     /opt/warm/package-lock.json \
 && cd /opt/warm \
 && npm config set cache /opt/npm-cache --global \
 && srb-npm ci \
 && test -d /opt/warm/node_modules/fastify \
 && node -e "const f=require('/opt/warm/node_modules/fastify'); \
             console.log('fastify', require('/opt/warm/node_modules/fastify/package.json').version)" \
 && rm -rf /opt/warm/node_modules /opt/warm/package.json /opt/warm/package-lock.json

# The delivery mirror must refuse the retired stack. If this assertion ever
# stopped holding, the task's central constraint would be unenforced.
RUN mkdir -p /opt/assert && cd /opt/assert \
 && printf '{"name":"a","version":"1.0.0","dependencies":{"express":"^4.17.1"}}\n' > package.json \
 && if srb-npm install --package-lock-only > /tmp/express-install.log 2>&1; then \
        echo 'FATAL: express is installable from the delivery mirror' >&2; \
        exit 1; \
    else \
        grep -qi '404' /tmp/express-install.log \
            && echo 'delivery mirror refuses express (404), as intended'; \
    fi \
 && cd / && rm -rf /opt/assert /tmp/express-install.log

# --- Helper scripts ----------------------------------------------------------
# json-server-oracle only. There is deliberately no self-check for the audit
# gates in this image: the gates are prose questions answered by a reviewer that
# reads both trees, and a script that claimed to predict that verdict would be
# either a rule list to satisfy -- the thing this task's gates exist to stop
# being -- or a lie about what stage 1 does.
COPY scripts/json-server-oracle /usr/local/bin/json-server-oracle
RUN chmod +x /usr/local/bin/json-server-oracle

ENV JSON_SERVER_ORACLE_URL=http://127.0.0.1:8899 \
    SRB_REGISTRY_ROOT=/opt/npm-registry \
    SRB_RETIRED_PACKAGES=/opt/srb/retired-packages.txt \
    npm_config_cache=/opt/npm-cache

WORKDIR /workspace/repo
