# Delivery environment for fw03. Built once by Harbor; every trial gets a fresh
# container from this image, so no state carries between attempts.
#
# What the image guarantees before the agent is handed instruction.md:
#   - /workspace/repo holds the frozen State A source and nothing else
#   - it is a git repository with exactly one commit and no upstream history
#   - the State B toolchain installs and builds with no network at all
#   - the State A production bundle is available, read-only, as an oracle
#
# What the image deliberately withholds: the State B dependency closure contains
# no Vue package of any kind, so the retired stack cannot be reinstalled. That is
# enforced by npm's own cache lookup, not by a check that could be edited.

FROM node@sha256:968ca0550acc7589a8b1324401ec6e39ace53b2c82d2aed3a278e9ff491c2b1c

ENV DEBIAN_FRONTEND=noninteractive \
    NPM_CONFIG_CACHE=/opt/npm-cache \
    NPM_CONFIG_OFFLINE=true \
    NPM_CONFIG_AUDIT=false \
    NPM_CONFIG_FUND=false \
    NPM_CONFIG_UPDATE_NOTIFIER=false \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright-browsers

RUN apt-get update && apt-get install -y --no-install-recommends \
      git ca-certificates jq python3 \
 && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# 1. The State B dependency closure, minted into an npm cache.
#
# Installed from a lockfile with integrity hashes, so versions are pinned and
# tamper-evident. The temporary tree is discarded; only the cache is kept, and it
# is the sole source of packages from here on.
# ---------------------------------------------------------------------------
COPY allowlist-package.json /opt/allowlist/package.json
COPY allowlist-package-lock.json /opt/allowlist/package-lock.json
RUN cd /opt/allowlist \
 && NPM_CONFIG_OFFLINE=false npm ci --ignore-scripts --loglevel=error \
 && rm -rf /opt/allowlist/node_modules \
 && npm cache verify >/dev/null

# `npm ci` reads resolved tarball URLs straight out of the lockfile, so it never
# fetches a package document. That leaves the cache able to install only from a
# lockfile -- and the task requires writing a new manifest, for which no lockfile
# exists yet. So resolve the same closure a second time *without* a lockfile,
# which is what populates the package documents (and any tarball a range-based
# resolution reaches that the lockfile did not pin).
#
# Both passes read the same cache from here on, so resolution is fixed at image
# build time and identical in the agent container and the verifier.
RUN set -eu; d=$(mktemp -d); cd "$d"; \
    cp /opt/allowlist/package.json .; \
    NPM_CONFIG_OFFLINE=false npm install --ignore-scripts --loglevel=error; \
    cd /; rm -rf "$d"; \
    echo "package documents cached for lockfile-free resolution"

# The allowlist is the contract; assert the retired stack never entered the cache.
RUN set -eu; \
    for pkg in vue vue-router vuex vue-template-compiler vue-axios \
               @vue/cli-service @vue/cli-plugin-babel @vue/cli-plugin-pwa \
               @vue/compat vuera veaury petite-vue alpinejs preact; do \
      if grep -rqsl "\"name\":\"$pkg\"" /opt/npm-cache/_cacache/index-v5 2>/dev/null; then \
        echo "FATAL: retired package '$pkg' is present in the offline cache"; exit 1; \
      fi; \
    done; \
    echo "cache holds no retired framework package"

# Chromium, so the agent can observe a rendered page the same way the grader does.
#
# Playwright is installed at /opt, not globally: the harness modules are ESM, and
# ESM ignores NODE_PATH and the global root. Resolving `import "playwright"` from
# /opt/harness/*.mjs works only because the loader walks up to /opt/node_modules.
RUN printf '{"name":"swerefactor-tooling","private":true,"version":"1.0.0"}\n' > /opt/package.json \
 && cd /opt \
 && NPM_CONFIG_OFFLINE=false npm i --loglevel=error playwright@1.49.1 \
 && /opt/node_modules/.bin/playwright install --with-deps chromium

# ---------------------------------------------------------------------------
# 2. The frozen repository.
#
# The checksum is verified before extraction, and the extracted tree is asserted
# free of version-control metadata: the task is to migrate the code that is here,
# not to mine the upstream project's later history for the answer.
# ---------------------------------------------------------------------------
WORKDIR /workspace
COPY original.tar.gz original.sha256 /workspace/
RUN sha256sum -c original.sha256 \
 && tar -xzf original.tar.gz \
 && rm -f original.tar.gz original.sha256 \
 && test -d /workspace/repo \
 && if find /workspace/repo -name .git -o -name .hg -o -name .svn \
         -o -name '*.orig' -o -name '*.rej' | grep -q .; then \
      echo "FATAL: version-control metadata present in the delivered tree"; exit 1; \
    fi \
 && echo "delivered tree carries no history"

# One commit, no parents: `git diff` is useful to the agent, `git log` reveals
# nothing about how the upstream project actually evolved.
RUN cd /workspace/repo \
 && git init -q -b main \
 && git config user.email task@swerefactor.local \
 && git config user.name SWERefactor \
 && git add -A \
 && git commit -q -m "State A: Vue 2 SPA at the frozen upstream revision" \
 && git tag baseline \
 && test "$(git rev-list --count HEAD)" = "1" \
 && test -z "$(git status --porcelain)"

# ---------------------------------------------------------------------------
# 3. The State A oracle: the production bundle built from exactly this source.
#
# Read-only. The agent may run it and observe it, which is how the behavioural
# contract is meant to be discovered, but cannot alter what it observes.
# ---------------------------------------------------------------------------
COPY oracle-dist.tgz oracle-dist.tgz.sha256 /tmp/
RUN cd /tmp && sha256sum -c oracle-dist.tgz.sha256 \
 && mkdir -p /opt/oracle \
 && tar -C /opt/oracle -xzf oracle-dist.tgz \
 && mv /opt/oracle/dist-stateA /opt/oracle/dist \
 && rm -f /tmp/oracle-dist.tgz /tmp/oracle-dist.tgz.sha256 \
 && test -f /opt/oracle/dist/index.html \
 && chmod -R a-w /opt/oracle

# The observation harness, shared verbatim with the grader: the same server, the
# same mock data, the same DOM reduction. What is NOT here is the list of screens
# the grader visits, or any recorded result -- deciding what to compare is work.
COPY harness /opt/harness
COPY bin/ /usr/local/bin/
RUN chmod +x /usr/local/bin/swerefactor-* \
 && node --input-type=module -e 'import("/opt/harness/server.mjs").then(m=>{if(!m.startServer)throw new Error("harness broken")})' \
 && chmod -R a-w /opt/harness

# ---------------------------------------------------------------------------
# 4. Prove the delivered toolchain works offline, then leave no trace of it.
#
# A throwaway React + Vite project is installed and built from the cache alone,
# with networking already irrelevant. If this step passes, a failure the agent
# hits later is theirs, not the image's.
# ---------------------------------------------------------------------------
RUN set -eu; d=$(mktemp -d); cd "$d"; \
    cp /opt/allowlist/package.json .; \
    cp /opt/allowlist/package-lock.json .; \
    npm ci --offline --loglevel=error; \
    mkdir -p src; \
    printf '%s\n' '<!DOCTYPE html><html><body><div id="root"></div>' \
      '<script type="module" src="/src/m.jsx"></script></body></html>' > index.html; \
    printf '%s\n' 'import {createRoot} from "react-dom/client";' \
      'createRoot(document.getElementById("root")).render(<div id="app">ok</div>);' > src/m.jsx; \
    printf '%s\n' 'import {defineConfig} from "vite";' \
      'import react from "@vitejs/plugin-react";' \
      'export default defineConfig({plugins:[react()]});' > vite.config.mjs; \
    npx vite build --logLevel error; \
    test -f dist/index.html; \
    cd /; rm -rf "$d"; \
    echo "offline React + Vite toolchain verified"

# The agent writes a new manifest, so its first install has no lockfile to read.
# Assert that path works too, from a hand-written manifest that names only what
# a minimal port needs -- and that `vue` is still refused from the same cache.
RUN set -eu; d=$(mktemp -d); cd "$d"; \
    printf '%s\n' '{"name":"t","version":"1.0.0","private":true,' \
      '"dependencies":{"react":"18.3.1","react-dom":"18.3.1",' \
      '"react-router-dom":"6.28.0","zustand":"5.0.1","axios":"0.19.0",' \
      '"date-fns":"1.30.1","marked":"0.7.0"},' \
      '"devDependencies":{"vite":"5.4.11","@vitejs/plugin-react":"4.3.4",' \
      '"vite-plugin-pwa":"0.21.1"}}' > package.json; \
    npm install --offline --loglevel=error; \
    test -f package-lock.json; \
    test -d node_modules/react-router-dom; \
    if npm install --offline --no-save vue@2.6.10 >/dev/null 2>&1; then \
      echo "FATAL: the cache installed vue"; exit 1; \
    fi; \
    cd /; rm -rf "$d"; \
    echo "lockfile-free offline install verified; vue still refused"

WORKDIR /workspace/repo
