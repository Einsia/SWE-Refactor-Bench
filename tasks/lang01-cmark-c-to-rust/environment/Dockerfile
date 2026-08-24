# SWERefactorBench - lang01-cmark-c-to-rust - agent environment
#
# State A is the pinned cmark 0.31.1 C implementation.  The image is built
# from a digest-pinned Rust base whose apt sources are re-pointed at the exact
# Debian snapshot that base image documents, so the toolchain closure is
# reproducible years from now.  The workspace that the agent receives carries
# no repository-management data of any kind: an agent that could read upstream
# history could read the migration out of a later commit instead of doing it.
FROM rust@sha256:3914072ca0c3b8aad871db9169a651ccfce30cf58303e5d6f2db16d1d8a7e58f AS toolchain

ARG DEBIAN_FRONTEND=noninteractive
ARG DEBIAN_SNAPSHOT=20251020T000000Z

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Pin the package closure to the snapshot recorded inside the base image, then
# install exactly the versions this task was calibrated against.
RUN set -eux; \
    test "$RUST_VERSION" = "1.90.0"; \
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
    apt-get -o Acquire::Retries=8 update; \
    apt-get install -y --no-install-recommends \
      cmake=3.25.1-1 \
      cmake-data=3.25.1-1 \
      libarchive13=3.6.2-1+deb12u3 \
      libjsoncpp25=1.9.5-4 \
      librhash0=1.4.3-3 \
      libuv1=1.44.2-1+deb12u1 \
      ninja-build=1.11.1-2~deb12u1; \
    rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*.deb /var/cache/apt/*.bin; \
    cmake --version | head -1 | grep -Fx 'cmake version 3.25.1'; \
    ninja --version; \
    rustc --version | grep -Fx 'rustc 1.90.0 (1159e78c4 2025-09-14)'; \
    cargo --version | grep -Fx 'cargo 1.90.0 (840b83a10 2025-07-30)'

# Cargo defaults to offline so a solver discovers the no-dependency rule at
# the first build rather than at submission time.
RUN set -eux; \
    install -d -m 0755 "$CARGO_HOME"; \
    printf '%s\n' \
      '[net]' \
      'offline = true' \
      '' \
      '[build]' \
      'incremental = false' \
      > "$CARGO_HOME/config.toml"; \
    chmod 0644 "$CARGO_HOME/config.toml"

RUN set -eux; \
    groupadd --gid 2000 agent; \
    useradd --uid 2000 --gid 2000 --create-home --shell /bin/bash agent; \
    install -d -m 0755 -o agent -g agent /workspace

# ---------------------------------------------------------------------------
# State A materialization.  The archive is authenticated before it is opened
# and the extracted tree is audited afterwards, so neither a substituted
# archive nor a stray VCS directory can reach the workspace.
#
# What survives into the agent's image lands in /opt/state-a: the source contract
# it builds against and the hash of the baseline commit.  Not /opt/swerefactor --
# that name belongs to the shared grader inside the three stage images, and an
# agent image should not carry a directory named after the thing marking it.
# ---------------------------------------------------------------------------
FROM toolchain AS state-a

COPY --chown=0:0 original.tar.gz /run/state-a/original.tar.gz
COPY --chown=0:0 source-contract.json /run/state-a/source-contract.json
COPY --chown=0:0 verify_environment.py /run/state-a/verify_environment.py

RUN set -eux; \
    echo '4a5466fa7ef20d7007b58fb217e7e7428201be6564f83c7bf76c17e004863a6c  /run/state-a/original.tar.gz' \
      | sha256sum -c -; \
    test "$(stat -c '%s' /run/state-a/original.tar.gz)" = "257372"; \
    install -d -m 0755 /workspace; \
    tar --extract --file=/run/state-a/original.tar.gz --directory=/workspace \
        --no-same-owner --no-same-permissions; \
    test -f /workspace/repo/CMakeLists.txt; \
    test "$(find /workspace/repo -type f | wc -l)" = "113"; \
    test -z "$(find /workspace/repo \
      \( -type l -o \( ! -type d ! -type f \) \) -print -quit)"; \
    test -z "$(find /workspace/repo \
      \( -name .git -o -name .github -o -name .gitignore \
         -o -name .gitattributes -o -name .gitmodules -o -name .hg \
         -o -name .svn -o -name .bzr -o -name _darcs -o -name CVS \
         -o -name .agit -o -name '*.pyc' \) -print -quit)"; \
    install -d -m 0755 /opt/state-a; \
    install -m 0444 /run/state-a/source-contract.json \
      /opt/state-a/source-contract.json

# Prove the delivered environment can build and exercise State A before any
# solver touches it, then discard every build artifact so the workspace the
# agent receives is exactly the source snapshot.  Nothing below reaches the
# network: cargo runs with --offline and the C build has no dependencies.
RUN set -eux; \
    cmake -S /workspace/repo -B /tmp/baseline \
      -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=ON \
      -DCMAKE_INSTALL_PREFIX=/tmp/baseline-prefix > /tmp/baseline-configure.log; \
    cmake --build /tmp/baseline --parallel "$(nproc)" > /tmp/baseline-build.log; \
    cmake --install /tmp/baseline > /tmp/baseline-install.log; \
    LD_LIBRARY_PATH=/tmp/baseline-prefix/lib \
      /tmp/baseline-prefix/bin/cmark --version | grep -F 'cmark 0.31.1'; \
    printf '# hi\n' | LD_LIBRARY_PATH=/tmp/baseline-prefix/lib \
      /tmp/baseline-prefix/bin/cmark | grep -Fx '<h1>hi</h1>'; \
    printf 'fn main() { println!("offline-ok"); }\n' > /tmp/probe.rs; \
    install -d -m 0755 /tmp/probe-crate/src; \
    printf '%s\n' '[package]' 'name = "probe"' 'version = "0.0.0"' \
      'edition = "2021"' '' '[lib]' 'crate-type = ["cdylib", "staticlib"]' \
      > /tmp/probe-crate/Cargo.toml; \
    printf '%s\n' '#[no_mangle] pub extern "C" fn probe_ok() -> i32 { 1 }' \
      > /tmp/probe-crate/src/lib.rs; \
    ( cd /tmp/probe-crate && cargo build --release --offline ); \
    test -f /tmp/probe-crate/target/release/libprobe.so; \
    test -f /tmp/probe-crate/target/release/libprobe.a; \
    /usr/bin/python3 -I /run/state-a/verify_environment.py \
      --repo /workspace/repo \
      --contract /opt/state-a/source-contract.json \
      --expect-files 113; \
    rm -rf /tmp/baseline /tmp/baseline-prefix /tmp/probe-crate /tmp/probe.rs \
      /tmp/baseline-configure.log /tmp/baseline-build.log \
      /tmp/baseline-install.log "$CARGO_HOME/registry" \
      /run/state-a; \
    test ! -e /run/state-a; \
    test "$(find /workspace/repo -type f | wc -l)" = "113"

# A single history-free baseline commit gives the solver a local diff surface.
# The verifier never trusts this repository: it holds its own copy of the
# baseline manifest and treats .git as untrusted agent-writable data.
RUN set -eux; \
    cd /workspace/repo; \
    git -c init.defaultBranch=main init -q .; \
    git config user.name 'SWERefactorBench'; \
    git config user.email 'bench@swerefactor.invalid'; \
    git config commit.gpgsign false; \
    git config gc.auto 0; \
    git add -A; \
    GIT_AUTHOR_DATE='2000-01-01T00:00:00 +0000' \
    GIT_COMMITTER_DATE='2000-01-01T00:00:00 +0000' \
    GIT_AUTHOR_NAME='SWERefactorBench' \
    GIT_COMMITTER_NAME='SWERefactorBench' \
    GIT_AUTHOR_EMAIL='bench@swerefactor.invalid' \
    GIT_COMMITTER_EMAIL='bench@swerefactor.invalid' \
      git commit -q -m 'State A: cmark 0.31.1 (C implementation)'; \
    test "$(git rev-list --count HEAD)" = "1"; \
    test -z "$(git status --porcelain)"; \
    git log --format=%H -1 > /opt/state-a/baseline-commit.txt; \
    chmod 0444 /opt/state-a/baseline-commit.txt; \
    chown -R agent:agent /workspace

ENV CARGO_NET_OFFLINE=true \
    RUST_BACKTRACE=1 \
    CMAKE_BUILD_PARALLEL_LEVEL=4 \
    TZ=UTC \
    LC_ALL=C.UTF-8 \
    LANG=C.UTF-8

WORKDIR /workspace/repo
USER agent
