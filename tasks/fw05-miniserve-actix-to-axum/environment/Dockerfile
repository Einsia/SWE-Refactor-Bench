# =============================================================================
# SWERefactorBench -- fw05-miniserve-actix-to-axum
# Agent environment image.
#
# Rust 1.77.2 is the era of miniserve 0.27.1 and the last toolchain that
# compiles its pinned `time 0.3.34` at all: Rust 1.80 changed type inference in
# a way that breaks that crate. Pinned by digest so the image is reproducible.
#
# What gets set up, in order:
#
#   1. Verify and unpack original.tar.gz to /workspace/repo, then commit it as the
#      single commit of a fresh git repository. No upstream history, tags or
#      remotes -- for a project that really was migrated later, upstream history
#      would be a straight answer key.
#   2. Fetch the BASELINE local-registry from crates.io against the pins in
#      warmup/, then derive the DELIVERY registry from it by applying the retired
#      list and check the result against the same registry-manifest.json the
#      verifier checks, so the two environments cannot drift apart.
#   3. Build the read-only State A oracle from the FULL registry inside a
#      throwaway CARGO_HOME, prove it serves, then delete both the full registry
#      and that CARGO_HOME so no actix source survives anywhere in the image.
#   4. Warm the build cache for the target stack -- which doubles as proof that
#      the delivery registry can satisfy an axum build.
#   5. Assert that the delivery registry refuses actix and that the shipped
#      baseline therefore does not build.
#
# The central constraint: actix-web and its 16 ecosystem crates do not exist in
# the delivery registry, so `cargo build` on the shipped source stops during
# resolution. Migrating is not a matter of taste here; nothing compiles until it
# is done.
# =============================================================================

FROM rust@sha256:83101f6985c93e1e6501b3375de188ee3d2cbb89968bcc91611591f9f447bd42
# = rust:1.77.2-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=UTC \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CARGO_TERM_COLOR=never \
    RUST_BACKTRACE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl jq file procps binutils python3 git tzdata \
    && rm -rf /var/lib/apt/lists/*

# The rust image puts the toolchain on PATH with ENV only, and /etc/profile
# overwrites PATH outright -- so `bash -lc 'cargo build'` reports "cargo: command
# not found" in an image that has cargo installed.  Agents do use login shells.
RUN printf 'export PATH="%s"\n' \
      '/usr/local/cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin' \
      > /etc/profile.d/10-rust.sh \
 && chmod 0644 /etc/profile.d/10-rust.sh \
 && bash -lc 'command -v cargo && cargo --version && command -v rustc'

# --- helper scripts ----------------------------------------------------------
# srb-sample-tree builds the deterministic directory miniserve is pointed at, so
# the agent can reproduce locally the tree it is graded against. The grader has
# its own copy of that logic and never calls this one -- a submission could
# replace it, and then it would be graded against a tree of its own choosing.
COPY scripts/srb-registry-tool  /usr/local/bin/srb-registry-tool
COPY scripts/srb-sample-tree    /usr/local/bin/srb-sample-tree
COPY scripts/miniserve-oracle   /usr/local/bin/miniserve-oracle
RUN chmod +x /usr/local/bin/srb-registry-tool /usr/local/bin/srb-sample-tree \
             /usr/local/bin/miniserve-oracle

# =============================================================================
# 1. Repo snapshot.
#
# The digest is checked before anything is unpacked: a swapped or truncated
# tarball would otherwise surface much later as a baffling compile error.
# =============================================================================
WORKDIR /workspace
COPY original.tar.gz /tmp/original.tar.gz
COPY original.sha256 /tmp/original.sha256

RUN set -eu; \
    cd /tmp && sha256sum -c original.sha256; \
    tar xzf /tmp/original.tar.gz -C /workspace; \
    test -f /workspace/repo/Cargo.toml; \
    test -f /workspace/repo/src/main.rs; \
    grep -q '^actix-web' /workspace/repo/Cargo.toml; \
    test ! -e /workspace/repo/.git; \
    rm -f /tmp/original.tar.gz /tmp/original.sha256

RUN set -eu; \
    cd /workspace/repo; \
    git init -q -b main; \
    git config user.email "task@swerefactorbench.invalid"; \
    git config user.name  "SWERefactorBench"; \
    git config commit.gpgsign false; \
    git add -A; \
    git commit -q -m "baseline: miniserve 0.27.1 on actix-web 4"; \
    test -z "$(git tag -l)"    || { echo "FATAL: tags leaked"   >&2; exit 1; }; \
    test -z "$(git remote)"    || { echo "FATAL: remote leaked" >&2; exit 1; }; \
    test "$(git rev-list --count HEAD)" = "1" \
        || { echo "FATAL: snapshot has more than one commit" >&2; exit 1; }; \
    echo "baseline commit: $(git rev-parse --short HEAD)"

# =============================================================================
# 2. Registries.
#
# Two registries are built here, both from crates.io, at image build time:
#
#   registry-full  the BASELINE closure -- both frameworks. Used once, in step 3,
#                  to compile the read-only State A oracle, then deleted.
#   registry       the DELIVERY closure -- baseline minus retired-crates.txt.
#                  This is the only one that survives into the shipped image and
#                  the only one the agent can resolve against.
#
# Neither is committed to the task folder. What is committed is warmup/: two lock
# files naming the closure, two manifests pinning every payload's sha256, the ban
# list, and the builder. A registry is 41M of crates.io's own bytes; the pins are
# 270K and say the same thing more strongly, because `--pin` fails the build on a
# digest that does not match rather than trusting whatever is on disk.
#
# warmup/ is copied to a build-only path and deleted at the end of this step. It
# must not survive: lock-target.lock is the SUPERSET lock naming both frameworks,
# and an agent who mistook it for a State B lockfile and installed it would fail
# the audit review on fifty lines of actix.
# =============================================================================
COPY warmup /tmp/srb-warmup

RUN set -eu; \
    python3 /tmp/srb-warmup/build-registry.py \
        --lock /tmp/srb-warmup/lock-baseline.lock \
        --lock /tmp/srb-warmup/lock-target.lock \
        --pin  /tmp/srb-warmup/registry-manifest-baseline.json \
        --out  /opt/srb/registry-full; \
    cp /tmp/srb-warmup/retired-crates.txt     /opt/srb/retired-crates.txt; \
    cp /tmp/srb-warmup/registry-manifest.json /opt/srb/registry-manifest.json
# manifest-target.toml is exposed to the agent as $SRB_TARGET_MANIFEST: it is the
# exact dependency block the closure was resolved from, so it is the answer to
# "which version, and with which features" -- the two things an offline registry
# makes unguessable.
#
# tree-spec.json is the deterministic directory miniserve is pointed at, so the
# agent can build the same tree it will be graded against.
COPY manifest-target.toml    /opt/srb/manifest-target.toml
COPY tree-spec.json          /opt/srb/tree-spec.json

# TLS certificates and the credentials file, outside /workspace/repo. Copies of
# the same files live in the verifier, and both harnesses read them from here
# rather than from the submission -- see the README beside each. The agent gets
# them too, so the same TLS and --auth-file invocations it is graded on can be
# reproduced locally against the oracle.
COPY tls                     /opt/srb/tls
COPY auth                    /opt/srb/auth

RUN set -eu; \
    cp -a /opt/srb/registry-full /opt/srb/registry; \
    srb-registry-tool retire --registry /opt/srb/registry \
                             --retired  /opt/srb/retired-crates.txt; \
    srb-registry-tool verify --registry /opt/srb/registry \
                             --manifest /opt/srb/registry-manifest.json

# Source replacement lives in the global cargo config, never in the repo. The
# agent has to be free to rewrite the repo's own .cargo/config.toml -- the
# baseline uses it for Windows rustflags -- without any risk of accidentally
# cutting themselves off from the only crate source in the image.
RUN printf '%s\n' \
    '# SWERefactorBench: the only crate source in this image.' \
    '[source.crates-io]' \
    'replace-with = "srb-mirror"' \
    '' \
    '[source.srb-mirror]' \
    'local-registry = "/opt/srb/registry"' \
    '' \
    '[net]' \
    'offline = true' \
    '' \
    '[term]' \
    'color = "never"' \
    > "${CARGO_HOME}/config.toml"

# =============================================================================
# 3. The State A oracle: miniserve 0.27.1 on actix-web 4, frozen and read-only.
#
# Built from the FULL registry in a throwaway CARGO_HOME so every trace of actix
# -- payloads, extracted sources, build artefacts -- can be deleted afterwards.
# What survives is one release binary. It keeps answering exactly as State A did
# no matter what happens to /workspace/repo, so it is the behavioural ground
# truth the agent diffs against.
#
# It is deliberately absent from the verifier image: a submission that shells
# out to the oracle rather than serving requests itself passes here and scores
# zero there.
# =============================================================================
RUN set -eu; \
    cp -a /workspace/repo /opt/oracle-build; \
    cd /opt/oracle-build; \
    rm -rf .git; \
    printf '[source.crates-io]\nreplace-with = "full"\n\n[source.full]\nlocal-registry = "/opt/srb/registry-full"\n\n[net]\noffline = true\n' \
        >> .cargo/config.toml; \
    CARGO_HOME=/opt/oracle-cargo cargo build --locked --offline --release --bin miniserve; \
    mkdir -p /opt/oracle; \
    install -m 0555 target/release/miniserve /opt/oracle/miniserve; \
    cd /; \
    rm -rf /opt/oracle-build /opt/oracle-cargo; \
    /opt/oracle/miniserve --version

# Prove the oracle serves before trusting it. A broken oracle would silently
# degrade every differential comparison it is supposed to arbitrate.
RUN set -eu; \
    srb-sample-tree --root /opt/oracle-root --spec /opt/srb/tree-spec.json > /dev/null; \
    /opt/oracle/miniserve /opt/oracle-root --index index.html --interfaces 127.0.0.1 \
        --port 8899 --enable-tar --enable-zip --readme > /tmp/oracle-boot.log 2>&1 & \
    ok=0; \
    for _ in $(seq 1 60); do \
        curl -sf http://127.0.0.1:8899/ > /dev/null 2>&1 && { ok=1; break; }; \
        sleep 0.5; \
    done; \
    if [ "$ok" = 1 ]; then \
        curl -s -o /dev/null -w 'oracle /            -> %{http_code}\n' http://127.0.0.1:8899/; \
        curl -s -o /dev/null -w 'oracle /test.txt    -> %{http_code}\n' http://127.0.0.1:8899/test.txt; \
        curl -s -o /dev/null -w 'oracle /?download   -> %{http_code}\n' 'http://127.0.0.1:8899/?download=tar'; \
        curl -s -o /dev/null -w 'oracle /nope        -> %{http_code}\n' http://127.0.0.1:8899/nope; \
    else \
        cat /tmp/oracle-boot.log >&2; \
    fi; \
    pkill -f '/opt/oracle/miniserve' 2>/dev/null || true; \
    rm -f /tmp/oracle-boot.log; \
    test "$ok" = 1

# The full registry has now served its only purpose. Deleting it is what makes
# the dependency gate real: after this line there is no copy of actix-web in the
# image in any form a build could consume.
#
# The warm-up inputs go with it. lock-target.lock names both frameworks and would
# be a misleading thing to leave lying around; the builder and the pins would let
# a submission reconstruct the baseline registry from the network, which the trial
# container has none of, but there is no reason to ship the recipe either.
RUN set -eu; \
    rm -rf /opt/srb/registry-full /tmp/srb-warmup; \
    leaks="$(find / -xdev \( -name 'actix-*' -o -name 'actix_*' \) \
             -not -path '/proc/*' -not -path '/sys/*' 2>/dev/null || true)"; \
    if [ -n "$leaks" ]; then \
        echo "FATAL: actix artefacts survive in the image:" >&2; \
        printf '%s\n' "$leaks" | head -20 >&2; \
        exit 1; \
    fi; \
    echo "no actix artefacts remain in the image"

# =============================================================================
# 4. Prove the whole delivery closure compiles, then warm the build cache.
#
# The closure build is the important half. A delivery registry that is missing a
# crate, or has one whose optional dependency is feature-gated off and therefore
# absent, must fail here -- at image build time, where it can be seen and fixed
# -- and not inside a no-network trial container with no way to report it.
#
# Note that failures are checked explicitly rather than piped into `tail`: a
# pipeline's exit status is its LAST command's, so `cargo build | tail` reports
# success even when the build failed, and `set -e` never fires.
#
# The closure covers the union of the target stack and the baseline's own
# dependencies -- including dev-dependencies, so the project's test suite can be
# compiled too -- and is built with miniserve's release profile, so the artefacts
# the release pass leaves in the repo's gitignored target/ are reusable by the
# agent's own `cargo build --release`. That is the warm cache.
#
# Only the release pass is kept. It is the slow one -- lto = true,
# codegen-units = 1, opt-level = 'z' -- and the one the verifier runs. The debug
# pass is a separate assertion (dev-dependencies
# and `cargo test` compile in debug) and is thrown away, since keeping it would
# add 1.3 GB to the image for a profile that recompiles quickly anyway.
# =============================================================================
RUN set -eu; \
    srb-registry-tool closuretoml --deps     /opt/srb/manifest-target.toml \
                                  --baseline /workspace/repo/Cargo.toml \
                                  --retired  /opt/srb/retired-crates.txt \
                                  --out      /opt/closure/Cargo.toml; \
    cd /opt/closure; \
    if ! CARGO_TARGET_DIR=/tmp/closure-dbg cargo build --offline > /tmp/closure.log 2>&1; then \
        echo 'FATAL: the delivery registry cannot build its own closure (debug)' >&2; \
        tail -40 /tmp/closure.log >&2; \
        exit 1; \
    fi; \
    grep -c 'Compiling' /tmp/closure.log | sed 's/^/closure: units compiled (debug): /'; \
    rm -rf /tmp/closure-dbg; \
    if ! CARGO_TARGET_DIR=/workspace/repo/target cargo build --offline --release \
            > /tmp/closure-rel.log 2>&1; then \
        echo 'FATAL: the delivery registry cannot build its own closure (release)' >&2; \
        tail -40 /tmp/closure-rel.log >&2; \
        exit 1; \
    fi; \
    grep -c 'Compiling' /tmp/closure-rel.log | sed 's/^/closure: units compiled (release): /'; \
    cd /; rm -rf /opt/closure /tmp/closure.log /tmp/closure-rel.log; \
    rm -f /workspace/repo/target/release/srb-closure; \
    du -sh /workspace/repo/target | sed 's/^/warm cache: /'

# Independent confirmation that the closure really is axum on hyper, exercising
# the feature set this migration needs: routing, static file serving,
# compression, auth, multipart uploads and rustls-backed TLS. This one compiles
# real axum code, so it fails if the crate is present but unusable.
COPY probe /opt/probe
RUN set -eu; \
    cd /opt/probe; \
    if ! CARGO_TARGET_DIR=/opt/probe-target cargo build --offline > /tmp/probe.log 2>&1; then \
        echo 'FATAL: the axum probe does not compile against the delivery registry' >&2; \
        tail -40 /tmp/probe.log >&2; \
        exit 1; \
    fi; \
    /opt/probe-target/debug/srb-probe; \
    cd /; rm -rf /opt/probe /opt/probe-target /tmp/probe.log


# The delivery registry must refuse the retired stack. If this ever stopped
# holding, the task's central constraint would be silently unenforced.
RUN set -eu; \
    mkdir -p /opt/assert/src && cd /opt/assert; \
    printf 'fn main() {}\n' > src/main.rs; \
    for crate_name in actix-web actix-files actix-multipart actix-web-httpauth; do \
        printf '[package]\nname = "a"\nversion = "0.0.0"\nedition = "2021"\npublish = false\n\n[dependencies]\n%s = "4"\n' \
            "$crate_name" > Cargo.toml; \
        rm -f Cargo.lock; \
        if CARGO_TARGET_DIR=/opt/assert-target cargo build --offline > /tmp/a.log 2>&1; then \
            echo "FATAL: $crate_name is obtainable from the delivery registry" >&2; \
            exit 1; \
        fi; \
        grep -qi 'no matching package named' /tmp/a.log \
            || { echo "FATAL: $crate_name failed for an unexpected reason" >&2; \
                 tail -10 /tmp/a.log >&2; exit 1; }; \
        printf 'delivery registry refuses %s\n' "$crate_name"; \
    done; \
    cd /; rm -rf /opt/assert /opt/assert-target /tmp/a.log

# And the shipped baseline must therefore not build. This is the gate the agent
# clears by migrating; worth proving at image build time rather than hoping it
# holds by accident.
RUN set -eu; \
    cp -a /workspace/repo /tmp/baseline-check; \
    cd /tmp/baseline-check; \
    if CARGO_TARGET_DIR=/tmp/baseline-target cargo build --offline --bin miniserve \
            > /tmp/baseline.log 2>&1; then \
        echo 'FATAL: the baseline builds without actix -- the gate is inert' >&2; \
        exit 1; \
    fi; \
    echo 'baseline does not build in the delivery environment, as intended:'; \
    grep -i 'no matching package named' /tmp/baseline.log | head -3 | sed 's/^/    /'; \
    cd /; rm -rf /tmp/baseline-check /tmp/baseline-target /tmp/baseline.log

# The agent's repo must be exactly as unpacked: warming touched only target/,
# which is gitignored.
RUN set -eu; \
    cd /workspace/repo; \
    dirty="$(git status --porcelain)"; \
    if [ -n "$dirty" ]; then \
        echo "FATAL: image build left the repo dirty:" >&2; \
        printf '%s\n' "$dirty" >&2; \
        exit 1; \
    fi; \
    echo "repo is clean at image-build time"

ENV MINISERVE_ORACLE_BIN=/opt/oracle/miniserve \
    MINISERVE_ORACLE_ROOT=/opt/oracle-root \
    SRB_REGISTRY_ROOT=/opt/srb/registry \
    SRB_RETIRED_CRATES=/opt/srb/retired-crates.txt \
    SRB_TARGET_MANIFEST=/opt/srb/manifest-target.toml \
    SRB_TREE_SPEC=/opt/srb/tree-spec.json \
    SRB_TLS_DIR=/opt/srb/tls \
    SRB_AUTH_FILE=/opt/srb/auth/auth-file.txt

WORKDIR /workspace/repo
