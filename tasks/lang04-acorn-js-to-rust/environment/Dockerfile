# syntax=docker/dockerfile:1
#
# Agent environment for lang04-acorn-js-to-rust.
#
# Hands the solver State A -- acorn 8.14.0, in JavaScript, buildable and testable
# -- plus the Rust toolchain the port targets.  Everything is pinned by digest or
# by exact version, because a task whose environment drifts is a task whose
# scores cannot be compared across months.
#
# The repository is placed at /workspace/repo with a single-commit git baseline
# and no upstream history: the port is the work, and a solver who can read the
# next commit is not doing it.

FROM rust@sha256:3914072ca0c3b8aad871db9169a651ccfce30cf58303e5d6f2db16d1d8a7e58f AS base

ARG DEBIAN_SNAPSHOT=20251020T000000Z
ARG NODE_VERSION=22.20.0
ARG NODE_SHA256=00bbd05e306ea68b6e13e17360d0e2f680b493ef95f2fea1c4296ff7437530bc

SHELL ["/bin/bash", "-euo", "pipefail", "-c"]
ENV DEBIAN_FRONTEND=noninteractive \
    TZ=UTC \
    LC_ALL=C.UTF-8 \
    LANG=C.UTF-8

# Point apt at a frozen snapshot so a rebuild six months from now resolves the
# same bytes.  Check-Valid-Until has to be off: a snapshot's Release file is, by
# construction, past its expiry.
RUN rm -f /etc/apt/sources.list.d/*; \
    printf '%s\n' \
      "deb http://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT} bookworm main" \
      "deb http://snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT} bookworm-security main" \
      "deb http://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT} bookworm-updates main" \
      > /etc/apt/sources.list; \
    printf 'Acquire::Check-Valid-Until "false";\n' \
      > /etc/apt/apt.conf.d/99no-check-valid-until; \
    printf 'Acquire::Retries "5";\n' > /etc/apt/apt.conf.d/99retries

RUN apt-get -qq update \
 && apt-get -qq install -y --no-install-recommends \
      ca-certificates=20230311+deb12u1 \
      xz-utils=5.4.1-1 \
      make=4.3-4.1 \
      python3=3.11.2-1+b1 \
      file=1:5.44-3 \
 && rm -rf /var/lib/apt/lists/*

# Node from the official dist tarball rather than the distro: the distro's
# nodejs moves within a snapshot's lifetime, and the reference implementation's
# behaviour is the thing being frozen here.
RUN cd /tmp \
 && curl -fsSLO "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz" \
 && echo "${NODE_SHA256}  node-v${NODE_VERSION}-linux-x64.tar.xz" | sha256sum -c - \
 && tar -xJf "node-v${NODE_VERSION}-linux-x64.tar.xz" -C /usr/local --strip-components=1 \
      --exclude='*/CHANGELOG.md' --exclude='*/README.md' --exclude='*/LICENSE' \
 && rm -f "node-v${NODE_VERSION}-linux-x64.tar.xz" \
 && node --version && npm --version

# Assert the toolchain is what the contract names.  grep -Fx so a version that
# merely contains the string does not pass.
RUN rustc --version | cut -d' ' -f2 | grep -Fx '1.90.0' \
 && cargo --version | cut -d' ' -f2 | grep -Fx '1.90.0' \
 && node --version | grep -Fx "v${NODE_VERSION}" \
 && python3 --version | cut -d' ' -f2 | grep -Fx '3.11.2'

# Non-root solver, fixed uid/gid so a collected artifact tree has stable
# ownership regardless of the host.
RUN groupadd --gid 2000 agent \
 && useradd --uid 2000 --gid 2000 --create-home --shell /bin/bash agent

# ---------------------------------------------------------------------------
# State A
# ---------------------------------------------------------------------------
FROM base AS state-a

ARG REPO_SHA256=11ba84c50c3ef2d63cfd665e7128f6c78d5cdc6d2bd4a0b3127fe8f34a0f019e
ARG REPO_SIZE=1190550
ARG REPO_FILES=116

COPY original.tar.gz /opt/swerefactor/repo.tar.gz
COPY source-contract.json /opt/swerefactor/source-contract.json
COPY buildtools-package.json /opt/swerefactor/buildtools/package.json
COPY buildtools-package-lock.json /opt/swerefactor/buildtools/package-lock.json
COPY verify_environment.py /opt/swerefactor/verify_environment.py

# Verify the archive before trusting it.  Size as well as digest: a truncated
# download that happens to be a valid gzip is a real failure mode.
RUN echo "${REPO_SHA256}  /opt/swerefactor/repo.tar.gz" | sha256sum -c - \
 && test "$(stat -c '%s' /opt/swerefactor/repo.tar.gz)" = "${REPO_SIZE}"

RUN install -d -m 0755 /workspace \
 && tar -xzf /opt/swerefactor/repo.tar.gz -C /workspace \
      --no-same-owner --no-same-permissions \
 && test -d /workspace/repo \
 && test "$(find /workspace/repo -type f | wc -l)" = "${REPO_FILES}" \
 && test -z "$(find /workspace/repo \( -type l -o -type b -o -type c -o -type p -o -type s \) -print -quit)"

# No repository-management data reached the solver.  This is the check that
# keeps the upstream history out of reach.
RUN for name in .git .github .gitlab .gitignore .gitattributes .gitmodules \
                .mailmap .hg .svn .bzr _darcs CVS .agit .travis.yml; do \
      if find /workspace/repo -name "$name" -print -quit | grep -q .; then \
        echo "repository-management data in State A: $name" >&2; exit 1; \
      fi; \
    done \
 && test -z "$(find /workspace/repo -name '*.pyc' -o -name '*.orig' -o -name '*.rej' -print -quit)"

RUN chmod 0444 /opt/swerefactor/source-contract.json

# ---------------------------------------------------------------------------
# State A's toolchain, outside the repository
# ---------------------------------------------------------------------------
# rollup and its buble plugin, locked by integrity hash.  Installed at
# /workspace/node_modules -- not inside the repo -- so the git baseline stays
# clean, node's upward module resolution still finds it from anywhere under
# /workspace, and the tree Harbor collects carries no dependencies.
RUN cd /opt/swerefactor/buildtools \
 && npm ci --no-audit --no-fund --ignore-scripts \
 && install -d -m 0755 /workspace \
 && mv node_modules /workspace/node_modules \
 && rm -rf /root/.npm

# buble -- rollup's transpiler plugin -- depends on acorn 6.4.2, which would
# otherwise occupy the top-level `acorn` name and shadow the repository's own.
# Nesting it under its only consumer is exactly what npm does for a version
# conflict, and it frees the name for State A.
RUN install -d /workspace/node_modules/buble/node_modules \
 && for p in acorn acorn-dynamic-import acorn-jsx; do \
      test -d "/workspace/node_modules/$p" \
        && mv "/workspace/node_modules/$p" "/workspace/node_modules/buble/node_modules/$p"; \
    done \
 && test ! -e /workspace/node_modules/acorn \
 && node -e 'require("/workspace/node_modules/buble")' \
 && node -p 'require("/workspace/node_modules/buble/node_modules/acorn/package.json").version' | grep -Fx '6.4.2'

# State A's CommonJS entry points resolve `require("acorn")` through
# node_modules.  Upstream does it with in-repo symlinks; doing it out of tree
# keeps the submission free of symlinks and survives the port deleting acorn/.
RUN ln -s /workspace/repo/acorn /workspace/node_modules/acorn \
 && ln -s /workspace/repo/acorn-loose /workspace/node_modules/acorn-loose \
 && ln -s /workspace/repo/acorn-walk /workspace/node_modules/acorn-walk

ENV PATH=/workspace/node_modules/.bin:$PATH \
    NODE_PATH=/workspace/node_modules

# A login shell re-reads /etc/profile and drops the image's PATH, which would
# hide cargo from `bash -lc`.  Symlinks work regardless of how the harness or
# the solver's tooling starts a shell; the profile drop-in keeps `which` honest.
RUN printf 'export PATH=/usr/local/cargo/bin:/workspace/node_modules/.bin:$PATH\nexport NODE_PATH=/workspace/node_modules\n' \
      > /etc/profile.d/10-swerefactor.sh \
 && chmod 0644 /etc/profile.d/10-swerefactor.sh \
 && for t in cargo rustc rustup rustdoc; do \
      ln -sf "/usr/local/cargo/bin/$t" "/usr/local/bin/$t"; \
    done \
 && bash -lc 'cargo --version && rustc --version && node --version'

# Prove State A builds and its own suite passes, in this image, before the image
# is allowed to exist.  Then remove every artifact and re-assert the file count,
# so the solver's starting tree is exactly the snapshot.
RUN cd /workspace/repo \
 && npm run build --silent \
 && test -f acorn/dist/acorn.js && test -f acorn/dist/bin.js \
 && node test/run.js | tee /tmp/statea-tests.txt \
 && grep -q 'all passed' /tmp/statea-tests.txt \
 && printf 'let x = 1\n' | node acorn/bin/acorn --ecma2020 | head -3 \
 && rm -rf acorn/dist acorn-loose/dist acorn-walk/dist \
 && test "$(find /workspace/repo -type f | wc -l)" = "${REPO_FILES}"

# Prove the Rust toolchain builds offline with an empty registry, which is the
# environment the port has to build in.
RUN install -d /tmp/rustproof/src \
 && printf '[package]\nname="p"\nversion="0.0.0"\nedition="2021"\n' > /tmp/rustproof/Cargo.toml \
 && printf 'fn main(){println!("ok");}\n' > /tmp/rustproof/src/main.rs \
 && cd /tmp/rustproof \
 && CARGO_NET_OFFLINE=true cargo build --release --offline -q \
 && ./target/release/p | grep -Fx ok \
 && cd / && rm -rf /tmp/rustproof

# ---------------------------------------------------------------------------
# Baseline commit
# ---------------------------------------------------------------------------
# One commit, fixed identity, fixed dates.  Deterministic so the image digest
# does not change on rebuild, and single so there is no history to mine.
RUN cd /workspace/repo \
 && git init -q -b main \
 && git config user.name  'SWERefactorBench' \
 && git config user.email 'bench@swerefactor.invalid' \
 && git config commit.gpgsign false \
 && git config core.autocrlf false \
 && git add -A \
 && GIT_AUTHOR_DATE='2000-01-01T00:00:00+0000' \
    GIT_COMMITTER_DATE='2000-01-01T00:00:00+0000' \
    git commit -q -m 'acorn 8.14.0 baseline' \
 && test "$(git rev-list --count HEAD)" = "1" \
 && test -z "$(git status --porcelain)" \
 && git rev-parse HEAD > /opt/swerefactor/baseline-commit.txt \
 && git log --format='%H %ad %s' --date=iso >> /opt/swerefactor/baseline-commit.txt

RUN python3 /opt/swerefactor/verify_environment.py \
      --repo /workspace/repo \
      --node-modules /workspace/node_modules \
      --contract /opt/swerefactor/source-contract.json

RUN chown -R agent:agent /workspace \
 && chmod 0444 /opt/swerefactor/baseline-commit.txt

ENV CARGO_NET_OFFLINE=true \
    RUST_BACKTRACE=1 \
    CARGO_BUILD_JOBS=4 \
    CARGO_TERM_COLOR=never

WORKDIR /workspace/repo
USER agent
