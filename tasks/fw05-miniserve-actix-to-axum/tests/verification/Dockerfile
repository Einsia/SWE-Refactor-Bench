# =============================================================================
# SWERefactorBench -- fw05-miniserve-actix-to-axum
# Stage 3 image: the verification probe.
#
#     docker build -t srb/fw05-verification:1 tasks/fw05-.../tests/verification
#
# Six rounds. Each is an independent attempt to write one test the original
# passes and the submission fails; a round that finds nothing pays the submission
# ten points. Reached only if stage 1 passed and stage 2 took full marks, so
# what is being measured here is not whether the port works -- that is already
# settled -- but how much scrutiny it survives.
#
# This is the one image in the task that must build BOTH trees, and it is the
# reason the whole registry apparatus exists twice:
#
#   /opt/srb/registry-baseline   401 crates. Includes actix-web and its
#                                companions, because the original cannot compile
#                                without them.
#   /opt/srb/registry            384 crates. The delivery registry, the same
#                                bytes the agent had, with the eighteen retired
#                                crates absent from both payloads and index.
#
# So the constraint the other grading image states -- that no actix crate should
# exist anywhere in it -- is deliberately not stated here, and could not be. A
# stage that asserts "the original passes this test" has to be able to build the
# original. Nothing leaks by it either: stage 3's premise is that the adversary
# reads the original, and the harness mounts it at /opt/original regardless.
#
# What does still hold, and is asserted below, is that the DELIVERY registry
# refuses the retired stack at resolution time. A submission that still needs
# actix cannot compile on the submission side however much actix sits in the
# other registry, because cargo is pointed at one registry per role through one
# CARGO_HOME per role and the two are never crossed.
#
# There is no oracle here either. /opt/oracle/miniserve exists only in the agent
# image; a submission that delegates to it fails stage 2 outright and never
# arrives.
#
# At runtime the harness provides:
#
#     /workspace/repo    the submission                        (read-write copy)
#     /opt/original      State A, unpacked from original.tar.gz     (read-only)
#     /logs/verifier     where the stage result JSON is written
#
# and the stage runner stages its own per-role copy of each tree under an opaque
# token, which is what run-candidate.sh is handed.
#
# Pinned by digest, like the other two images, so the grader cannot drift.
# =============================================================================

ARG INFRA_IMAGE=swerefactor/infra:1
FROM ${INFRA_IMAGE} AS infra

FROM rust@sha256:83101f6985c93e1e6501b3375de188ee3d2cbb89968bcc91611591f9f447bd42
# = rust:1.77.2-bookworm, byte for byte the toolchain the agent had, and the one
# stage 2 graded with. Both trees are compiled by the same rustc here, which is
# what makes a difference between them a difference between the programs: 1.80
# changed type inference in a way that breaks the pinned `time 0.3.34`, so a
# newer toolchain would fail builds that are correct on both sides.

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=UTC \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=0 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    CARGO_TERM_COLOR=never \
    RUST_BACKTRACE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl jq file procps binutils python3 python3-venv git tzdata \
    && rm -rf /var/lib/apt/lists/*

# --- The candidate's interpreter ---------------------------------------------
# pytest, pytest-timeout, and nothing else. requirements-candidate.txt argues the
# omissions at length; the short version is that every HTTP client library in the
# index normalises at least one thing a candidate is hired to observe.
COPY requirements-candidate.txt /opt/pins/requirements-candidate.txt

RUN python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --upgrade "pip==24.3.1" \
 && /opt/venv/bin/pip install --no-cache-dir --require-hashes \
        -r /opt/pins/requirements-candidate.txt \
 && /opt/venv/bin/python -c "import pytest; print('candidate stack:', pytest.__version__)"

ENV VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:/usr/local/cargo/bin:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin

# =============================================================================
# The two registries
#
# Built the way the environment builds them, which is once and then by omission:
# the baseline is fetched and pinned against registry-manifest-baseline.json, and
# the delivery registry is a copy of it with the retired crates removed from
# payloads and index alike. Building the delivery one independently with --ban
# would land on the same bytes -- stage 2 does exactly that -- but here the
# baseline is needed anyway, so `retire` from a copy is both cheaper and a
# stronger statement: the two registries are provably the same registry minus
# eighteen names, rather than two fetches that happen to agree.
# =============================================================================
COPY data/warmup            /tmp/srb-warmup
COPY data/manifest-target.toml /opt/srb/manifest-target.toml
COPY data/scripts/srb-registry-tool /usr/local/bin/srb-registry-tool
COPY data/scripts/srb-sample-tree   /usr/local/bin/srb-sample-tree

RUN set -eu; \
    chmod +x /usr/local/bin/srb-registry-tool /usr/local/bin/srb-sample-tree; \
    mkdir -p /opt/srb; \
    python3 /tmp/srb-warmup/build-registry.py \
        --lock /tmp/srb-warmup/lock-baseline.lock \
        --lock /tmp/srb-warmup/lock-target.lock \
        --pin  /tmp/srb-warmup/registry-manifest-baseline.json \
        --out  /opt/srb/registry-baseline; \
    cp /tmp/srb-warmup/retired-crates.txt              /opt/srb/retired-crates.txt; \
    cp /tmp/srb-warmup/registry-manifest.json          /opt/srb/registry-manifest.json; \
    cp /tmp/srb-warmup/registry-manifest-baseline.json /opt/srb/registry-manifest-baseline.json; \
    srb-registry-tool verify --registry /opt/srb/registry-baseline \
                             --manifest /opt/srb/registry-manifest-baseline.json

RUN set -eu; \
    cp -a /opt/srb/registry-baseline /opt/srb/registry; \
    srb-registry-tool retire --registry /opt/srb/registry \
                             --retired  /opt/srb/retired-crates.txt; \
    srb-registry-tool verify --registry /opt/srb/registry \
                             --manifest /opt/srb/registry-manifest.json; \
    srb-registry-tool assert-retired --registry /opt/srb/registry \
                                     --retired  /opt/srb/retired-crates.txt

# Three independent statements of the same constraint, because it is the one the
# whole task rests on: --pin checked every payload's digest on the way in,
# `verify` additionally checks the index entry count, and `assert-retired` checks
# that each retired name resolves to nothing. Any one passing while another failed
# would mean a registry and its manifest have diverged.

# =============================================================================
# One CARGO_HOME per registry
#
# Cargo's source replacement is global: it lives in $CARGO_HOME/config.toml and
# names exactly one local-registry path. Two registries therefore mean two homes,
# and run-candidate.sh selects between them by role.
#
# They are a pair with their target directories and cannot be crossed over. A
# fingerprint records the absolute path of every source file it was compiled from,
# and those paths run through $CARGO_HOME/registry/src/ -- so a cache warmed under
# one home is worthless under another, and cargo would silently rebuild ~320
# crates while the first candidate of the round sat in the linker.
#
# The default /usr/local/cargo config is left pointing at the delivery registry,
# so anything in this image that runs cargo without choosing gets the restricted
# one rather than the permissive one.
# =============================================================================
RUN set -eu; \
    for pair in "/opt/cargo-baseline:/opt/srb/registry-baseline" \
                "/opt/cargo-delivery:/opt/srb/registry" \
                "/usr/local/cargo:/opt/srb/registry"; do \
        home="${pair%%:*}"; reg="${pair##*:}"; \
        mkdir -p "$home"; \
        printf '%s\n' \
            '# SWERefactorBench: the only crate source reachable from this home.' \
            '[source.crates-io]' \
            'replace-with = "srb-mirror"' \
            '' \
            '[source.srb-mirror]' \
            "local-registry = \"$reg\"" \
            '' \
            '[net]' \
            'offline = true' \
            '' \
            '[term]' \
            'color = "never"' \
            > "$home/config.toml"; \
        printf 'cargo home %s -> %s\n' "$home" "$reg"; \
    done

# =============================================================================
# State A, for the warm caches and for one assertion
#
# Digest and size are checked inline rather than read from a shipped .sha256, so
# the assertion cannot be satisfied by editing the file beside it. It is the same
# digest environment/original.sha256 records; `swerefactor validate` fails the task
# if the two copies of the tarball ever differ at all.
#
# Extracted here and deleted three steps down. It must not survive: the harness
# mounts the real State A at /opt/original for every stage, and a second copy at
# a fixed path would be one more way for a candidate to answer "which tree am I
# on" by comparing files instead of by observing behaviour.
# =============================================================================
COPY data/original.tar.gz /run/baseline/original.tar.gz

RUN set -eu; \
    echo '90291a2e51704fd74f2096a13e4051e0a799db530b16e311d3ed150763302b87  /run/baseline/original.tar.gz' \
      | sha256sum -c -; \
    test "$(stat -c '%s' /run/baseline/original.tar.gz)" = "156558"; \
    tar --extract --file=/run/baseline/original.tar.gz --directory=/run/baseline \
        --no-same-owner --no-same-permissions; \
    test -f /run/baseline/repo/Cargo.toml; \
    grep -q '^name = "miniserve"' /run/baseline/repo/Cargo.toml; \
    grep -q '^actix-web' /run/baseline/repo/Cargo.toml; \
    cp /run/baseline/repo/Cargo.toml /opt/srb/baseline-Cargo.toml; \
    rm -f /run/baseline/original.tar.gz; \
    echo "State A extracted for the warm caches"

# --- The baseline warm cache, and the proof that the baseline builds -----------
# The original, compiled offline from the baseline registry in its own cargo home.
# This is two things at once and the second is the more valuable:
#
#   * A warm cache. Stage 3 rebuilds the original at grading time, and a cold
#     release build of this crate -- `lto = true`, `codegen-units = 1`, ~320
#     crates -- is minutes. The first candidate of the stage would otherwise pay
#     it and would quite possibly be killed by its own timeout, which reads as
#     "the candidate does not pass on the original" and discards a whole round for
#     a reason that has nothing to do with the submission.
#
#   * An assertion. If the baseline registry cannot compile State A offline, every
#     one of the six rounds fails identically at run time and reports nothing
#     about the submission. Failing here costs a build instead.
#
# --locked, because State A's Cargo.lock is exactly lock-baseline.lock and the
# registry was pinned from it. A resolution that needed to move is a broken
# registry, not a lockfile nit.
#
# The binary is then deleted, and asserted gone. Not housekeeping: a prebuilt
# original sitting at a fixed path is a byte-for-byte comparison away from telling
# a candidate which tree it is on, and stage 2 removes its equivalent for the
# closely related reason that a stale binary would hand a free pass to a
# submission that compiled nothing. The object files stay, which is the whole
# point -- what is being avoided is the compile, not the link.
RUN set -eu; \
    cd /run/baseline/repo; \
    if ! env CARGO_HOME=/opt/cargo-baseline \
             CARGO_TARGET_DIR=/opt/srb/warm-baseline \
             RUSTFLAGS= \
             cargo build --offline --locked --release --bin miniserve \
             > /tmp/warm-baseline.log 2>&1; then \
        echo 'FATAL: the baseline registry cannot build State A offline' >&2; \
        tail -40 /tmp/warm-baseline.log >&2; \
        exit 1; \
    fi; \
    grep -c 'Compiling' /tmp/warm-baseline.log \
      | sed 's/^/baseline warm cache: units compiled: /'; \
    /opt/srb/warm-baseline/release/miniserve --version; \
    rm -f /opt/srb/warm-baseline/release/miniserve \
          /opt/srb/warm-baseline/debug/miniserve; \
    test ! -e /opt/srb/warm-baseline/release/miniserve; \
    chmod -R a+rwX /opt/srb/warm-baseline; \
    du -sh /opt/srb/warm-baseline | sed 's/^/baseline warm cache: /'; \
    cd /; rm -f /tmp/warm-baseline.log

# --- The delivery warm cache --------------------------------------------------
# The submission cannot be built at image time -- it does not exist yet -- so this
# is the same synthetic closure crate stage 2 warms with: the union of the target
# stack and State A's own dependencies, with the retired names dropped, compiled
# under the baseline's release profile so the artefacts are reusable.
#
# --baseline folds in miniserve's own dependencies (clap, comrak, grass, qrcodegen
# and the rest, which manifest-target.toml does not list because it lists only
# what replaces actix), and --retired drops the actix ones while folding.
#
# srb-closure produces no binary named `miniserve`, which is asserted: a stale one
# in a shared target directory would be handed to a candidate as though the
# submission had produced it.
RUN set -eu; \
    srb-registry-tool closuretoml --deps     /opt/srb/manifest-target.toml \
                                  --baseline /opt/srb/baseline-Cargo.toml \
                                  --retired  /opt/srb/retired-crates.txt \
                                  --out      /opt/closure/Cargo.toml; \
    ! grep -qi 'actix' /opt/closure/Cargo.toml; \
    cd /opt/closure; \
    if ! env CARGO_HOME=/opt/cargo-delivery \
             CARGO_TARGET_DIR=/opt/srb/warm-target \
             RUSTFLAGS= \
             cargo build --offline --release > /tmp/warm-target.log 2>&1; then \
        echo 'FATAL: the delivery registry cannot build its own closure' >&2; \
        tail -40 /tmp/warm-target.log >&2; \
        exit 1; \
    fi; \
    grep -c 'Compiling' /tmp/warm-target.log \
      | sed 's/^/delivery warm cache: units compiled: /'; \
    rm -f /opt/srb/warm-target/release/srb-closure; \
    test ! -e /opt/srb/warm-target/release/miniserve; \
    test ! -e /opt/srb/warm-target/debug/miniserve; \
    chmod -R a+rwX /opt/srb/warm-target; \
    du -sh /opt/srb/warm-target | sed 's/^/delivery warm cache: /'; \
    cd /; rm -rf /opt/closure /tmp/warm-target.log /run/baseline

# State A is gone from the image now. What is left of it is object files in the
# baseline warm cache, which are its dependencies rather than its own code.
RUN test ! -e /run/baseline && test ! -e /opt/srb/warm-baseline/release/miniserve \
    && echo "State A sources removed; only dependency artefacts remain"

# =============================================================================
# The delivery registry still refuses the retired stack
#
# This is the assertion that matters most in this image, because this is the one
# image where actix is present on disk. It has to be unreachable from the
# submission's cargo home anyway.
#
# Resolution, not compilation: the resolver's own "no matching package named" is
# the error a submission cannot route around, since there is no flag that
# re-enables a crate with no index entry. Run twice per crate -- once from
# /opt/cargo-delivery, which is what run-candidate.sh uses for the submission,
# and once from the default /usr/local/cargo, which is what anything careless
# would use.
#
# Then a control, from /opt/cargo-baseline, where the same crate must RESOLVE.
# Without one the loop would pass just as happily against an empty registry, or a
# typo in a source-replacement path.
#
# The control takes actix-web's dependency line verbatim out of State A's own
# manifest rather than spelling one here, and that is not tidiness. A throwaway
# crate asking for a bare `actix-web = "4"` does not resolve from the baseline
# registry and should not: actix-web's default features include `cookies`,
# miniserve sets `default-features = false` and never enables it, so `cookie` was
# never in the lockfile this registry was pinned from and has no index entry. The
# registry holds State A's closure exactly, not actix-web's every configuration --
# which is the same property that makes the delivery registry a real gate. A
# hand-written control would have to restate miniserve's feature list and would
# rot the first time it changed.
# =============================================================================
RUN set -eu; \
    mkdir -p /opt/assert/src && cd /opt/assert; \
    printf 'fn main() {}\n' > src/main.rs; \
    for crate_name in actix-web actix-files actix-multipart actix-web-httpauth awc; do \
        printf '[package]\nname = "a"\nversion = "0.0.0"\nedition = "2021"\npublish = false\n\n[dependencies]\n%s = "4"\n' \
            "$crate_name" > Cargo.toml; \
        for home in /opt/cargo-delivery /usr/local/cargo; do \
            rm -f Cargo.lock; \
            if env CARGO_HOME="$home" CARGO_TARGET_DIR=/opt/assert-target \
                   cargo generate-lockfile --offline > /tmp/a.log 2>&1; then \
                echo "FATAL: $crate_name is obtainable from $home" >&2; \
                exit 1; \
            fi; \
            grep -qi 'no matching package named' /tmp/a.log \
                || { echo "FATAL: $crate_name failed for an unexpected reason under $home" >&2; \
                     tail -10 /tmp/a.log >&2; exit 1; }; \
        done; \
        printf 'delivery registry refuses %s (both homes)\n' "$crate_name"; \
    done; \
    rm -f Cargo.lock; \
    dep=$(grep '^actix-web = ' /opt/srb/baseline-Cargo.toml) \
      || { echo 'FATAL: State A manifest has no actix-web line to control against' >&2; \
           exit 1; }; \
    printf '[package]\nname = "a"\nversion = "0.0.0"\nedition = "2021"\npublish = false\n\n[dependencies]\n%s\n' \
        "$dep" > Cargo.toml; \
    env CARGO_HOME=/opt/cargo-baseline CARGO_TARGET_DIR=/opt/assert-target \
        cargo generate-lockfile --offline > /tmp/a.log 2>&1 \
      || { echo 'FATAL: the baseline registry cannot resolve State A own actix-web line,' >&2; \
           echo '       so the refusals above prove nothing about the ban' >&2; \
           tail -20 /tmp/a.log >&2; exit 1; }; \
    grep -q 'name = "actix-web"' Cargo.lock \
      || { echo 'FATAL: resolve succeeded without actix-web in the lockfile' >&2; exit 1; }; \
    echo 'control: the baseline registry resolves State A own actix-web line'; \
    cd /; rm -rf /opt/assert /opt/assert-target /tmp/a.log

# warmup/ has served its purpose and lock-target.lock is the one file in it that
# names actix on fifty lines. Removed for tidiness rather than for secrecy: this
# image holds a whole actix registry by design, and the substring assertion stage
# 2 makes here would be a lie if it were repeated. What is asserted instead is the
# property that has to hold -- no retired name resolves from the DELIVERY registry
# -- for all eighteen names rather than for the five resolution was tested against.
#
# The baseline side is asserted narrowly and deliberately so. Seventeen of the
# eighteen are in the baseline index; `awc` is not, and should not be. It is
# actix's HTTP client, miniserve never depended on it, so it was never in the
# lockfile the registry was pinned from -- it is on the retired list pre-emptively,
# to stop a submission reaching for actix's client on its own. So "the two
# registries are the same minus eighteen" is not true, and an assertion saying so
# fails on a registry that is completely correct. What is asserted instead is that
# the four crates State A's manifest names directly are present, because a baseline
# registry without those is not building the original at all -- and the count is
# reported rather than pinned, so the next person to add a pre-emptive ban does not
# have to come back here.
RUN set -eu; \
    rm -rf /tmp/srb-warmup; \
    count=$(grep -cve '^[[:space:]]*$' -e '^#' /opt/srb/retired-crates.txt || true); \
    test "${count:-0}" -ge 18 \
        || { echo "FATAL: retired list holds only ${count:-0} names; the ban was a no-op" >&2; \
             exit 1; }; \
    leaked=""; in_baseline=0; \
    while read -r name; do \
        case "$name" in ''|\#*) continue;; esac; \
        if [ -n "$(find /opt/srb/registry/index -type f -name "$name" -print -quit)" ]; then \
            leaked="$leaked index:$name"; \
        fi; \
        if [ -n "$(find /opt/srb/registry -maxdepth 1 -type f -name "$name-*.crate" -print -quit)" ]; then \
            leaked="$leaked payload:$name"; \
        fi; \
        if [ -n "$(find /opt/srb/registry-baseline/index -type f -name "$name" -print -quit)" ]; then \
            in_baseline=$((in_baseline + 1)); \
        fi; \
    done < /opt/srb/retired-crates.txt; \
    if [ -n "$leaked" ]; then \
        echo "FATAL: retired crate reachable in the delivery registry:$leaked" >&2; \
        exit 1; \
    fi; \
    for name in actix-web actix-files actix-multipart actix-web-httpauth; do \
        test -n "$(find /opt/srb/registry-baseline/index -type f -name "$name" -print -quit)" \
            || { echo "FATAL: the baseline registry has no $name, which State A names" >&2; \
                 echo "       directly -- so it cannot be building the original at all" >&2; \
                 exit 1; }; \
    done; \
    printf 'delivery registry: no index entry and no payload for any of the %s retired crates\n' "$count"; \
    printf 'baseline registry: %s of %s retired crates present, including all four State A names\n' \
        "$in_baseline" "$count"; \
    rm -f /opt/srb/baseline-Cargo.toml /opt/srb/manifest-target.toml

# --- Inputs the harness reads, not the submission ----------------------------
# Byte copies of the environment's -- `swerefactor validate` fails the task if they
# ever differ. They are read from here rather than from either staged tree, so a
# candidate is measuring both binaries against the same TLS material, the same
# credentials and the same serve root. A submission that shipped its own copy of
# any of them would otherwise be choosing what it is compared on.
RUN mkdir -p /opt/srb/tls /opt/srb/auth
COPY data/tls            /opt/srb/tls
COPY data/auth           /opt/srb/auth
COPY data/tree-spec.json /opt/srb/tree-spec.json

# --- The reference serve tree -------------------------------------------------
# Materialised once here and copied per test by srbtarget.build_tree, rather than
# replayed from the spec on every use. Two reasons, and the second is not about
# speed: the tree carries names with embedded newlines, quotes and a literal
# `%20`, and mtimes pinned to 2021-06-15T12:34:56Z -- so building it once and
# verifying it once means every candidate in every round serves the same bytes,
# and a candidate cannot get a divergence by accidentally building a different
# tree for the two roles.
#
# The count and one hostile name are asserted, because a spec that silently
# produced an empty directory would make most candidates pass on both sides and
# read as "the submission is fine".
RUN set -eu; \
    srb-sample-tree --root /opt/srb/tree-reference \
                    --spec /opt/srb/tree-spec.json > /dev/null; \
    entries=$(find /opt/srb/tree-reference -mindepth 1 -printf 'x\0' \
                | tr -dc '\0' | wc -c); \
    test "$entries" -eq 136 \
        || { echo "FATAL: reference tree has $entries entries, not 136" >&2; exit 1; }; \
    for pattern in '*%20*' '*"*' '*\\*'; do \
        test -n "$(find /opt/srb/tree-reference -maxdepth 1 -name "$pattern" -print -quit)" \
            || { echo "FATAL: no top-level name matching $pattern; the escaping cases are unreachable" >&2; \
                 exit 1; }; \
    done; \
    nl_hits=$(find /opt/srb/tree-reference -maxdepth 1 -printf '%f\0' \
                | tr -dc '\0\n' | tr -d '\0' | wc -c); \
    test "$nl_hits" -ge 1 \
        || { echo 'FATAL: no name contains a literal newline; header-safety cases are unreachable' >&2; \
             exit 1; }; \
    test -L /opt/srb/tree-reference/links/dangling \
        || { echo 'FATAL: the dangling symlink is missing; --no-symlinks is ungradeable' >&2; \
             exit 1; }; \
    test -f /opt/srb/tree-reference/withindex/index.html; \
    test -f /opt/srb/tree-reference/README.md; \
    test "$(stat -c '%Y' /opt/srb/tree-reference/withindex/index.html)" = "1623760496" \
        || { echo 'FATAL: mtimes are not pinned; Last-Modified and ETag become ungradeable' >&2; \
             exit 1; }; \
    test "$(stat -c '%Y' /opt/srb/tree-reference/dira)" = "1623760496"; \
    chmod -R a+rX /opt/srb/tree-reference; \
    printf 'reference tree: %s entries, mtimes pinned, hostile names present\n' "$entries"

# 136, not the 133 the spec lists: three directories -- very/, very/deeply/ and
# very/deeply/nested/ -- come into being as parents of a declared file rather than
# being declared themselves. Counted with NUL separators, because six of these
# names contain a literal newline and `find | wc -l` reports 142 for the same tree.
# That is worth a comment rather than a silent fix: a candidate that counts listing
# rows by splitting on newlines will get 142 too, on both sides, and will not notice
# until it tries to assert against one of the six.

ENV SRB_WARM_BASELINE=/opt/srb/warm-baseline \
    SRB_WARM_TARGET=/opt/srb/warm-target \
    SRB_TREE_REFERENCE=/opt/srb/tree-reference \
    SRB_TREE_SPEC=/opt/srb/tree-spec.json \
    SRB_TLS_DIR=/opt/srb/tls \
    SRB_AUTH_FILE=/opt/srb/auth/auth-file.txt

# --- The harness -------------------------------------------------------------
COPY --from=infra /opt/swerefactor /opt/swerefactor
ENV PYTHONPATH=/opt/swerefactor

# --- The probe ---------------------------------------------------------------
# Copied last so iterating on the prompt does not rebuild two registries.
COPY . /tests/verification

RUN set -eu; \
    chmod +x /tests/verification/run-candidate.sh; \
    cd /tests/verification; \
    /opt/venv/bin/python check-probe.py; \
    PYTHONPATH=/tests/verification/lib /opt/venv/bin/python -c "import srbtarget"

# check-probe.py is where the six rounds, the scope and the timeout bound are
# checked against the contract; its own docstring argues each one. The import is
# separate and cheap: srbtarget is the only thing every candidate in every round
# depends on, and a syntax error in it would present as six rounds that all
# find nothing -- which is a sixty-point gift to the submission.

RUN find /tests/verification/lib -name '*.py' -print0 \
      | xargs -0 /opt/venv/bin/python -m py_compile \
 && find /tests/verification -name '__pycache__' -type d -prune -exec rm -rf {} + \
 && echo "candidate library compiles"

WORKDIR /tests/verification
