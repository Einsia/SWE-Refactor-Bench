# =============================================================================
# SWERefactorBench -- fw05-miniserve-actix-to-axum
# Stage 2 image: the behavioural suite.
#
#     docker build -t srb/fw05-behavioural:1 tasks/fw05-.../tests/behavioural
#
# Build context is this directory, so the suite is in the image and nothing else
# is. The agent container never sees it.
#
# What makes this a measurement rather than a claim:
#
#   * The delivery registry is rebuilt here from warmup/, the same two lock files
#     and the same manifest the agent's environment was built from -- and with the
#     same ban list, so the seventeen retired actix crates in the closure are
#     never downloaded at all. Not deleted in a later layer: never fetched. A
#     submission that still needs actix cannot compile here for the same reason it
#     could not compile there.
#
#   * There is no oracle. /opt/oracle/miniserve exists only in the agent image,
#     and this is the reason it does. A submission that copies it to
#     target/release/miniserve, symlinks to it, execs it or proxies to it passes
#     everything the agent can run locally and then finds nothing here, failing
#     all 612 cases. Delegation is the cheat this task is built to catch, and the
#     shape of these two images is how it is caught -- not a string check.
#
#   * The build module compiles from the submitted source into a target directory
#     outside the tree, having first removed any `miniserve` binary that was
#     already there. Nothing the agent left in target/ is graded.
#
# At runtime the harness provides:
#
#     /workspace/repo    the submission                        (read-write copy)
#     /opt/original      State A, unpacked from original.tar.gz     (read-only)
#     /logs/verifier     where the stage result JSON is written
#
# /opt/original is unpacked by the harness for every stage. This suite does not
# read it: stage 2's ground truth is the frozen recording in data/, captured from
# State A once, so that a comparison cannot depend on a build that happens at
# grading time. Stage 1 and stage 3 are the stages that want both trees.
#
# Pinned by digest, like the environment image, so the grader cannot drift.
# =============================================================================

ARG INFRA_IMAGE=swerefactor/infra:1
FROM ${INFRA_IMAGE} AS infra

FROM rust@sha256:83101f6985c93e1e6501b3375de188ee3d2cbb89968bcc91611591f9f447bd42
# = rust:1.77.2-bookworm, byte for byte the toolchain the agent had. A submission
# compiled by a different rustc is a different measurement: 1.80 changed type
# inference in a way that breaks the pinned `time 0.3.34`, so a newer toolchain
# would fail builds that are correct, and an older one would accept code the agent
# could not have written.

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

# --- The suite's own interpreter ---------------------------------------------
# pytest, plus brotli and zstandard; requirements-suite.txt argues both the
# inclusions and the omissions. The harness speaks HTTP over `socket` and `ssl`
# from the standard library, because every client library normalises something
# this task grades -- but it must still be able to undo the two content encodings
# miniserve is built to produce, because the recording stores their decoded form.
COPY requirements-suite.txt /opt/pins/requirements-suite.txt

# The decoders are exercised here, not just imported. `normalize.decompress`
# catches a missing module and records it as a body that failed to decode, so an
# absent decoder does not announce itself: it turns into 72 ordinary-looking
# comparison failures in `encodings` against a recording that has those same
# bodies decoded. This layer is the only place that difference is cheap to see,
# and a round trip is what proves the wheel that installed actually works -- an
# `import` succeeds against a broken extension often enough to be worth not
# trusting.
RUN python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --upgrade "pip==24.3.1" \
 && /opt/venv/bin/pip install --no-cache-dir --require-hashes \
        -r /opt/pins/requirements-suite.txt \
 && /opt/venv/bin/python -c "import pytest; print('runner stack:', pytest.__version__)" \
 && /opt/venv/bin/python -c "\
import brotli, zstandard; \
s = b'miniserve compresses with both of these' * 32; \
assert brotli.decompress(brotli.compress(s)) == s, 'brotli round trip'; \
assert zstandard.ZstdDecompressor().decompressobj().decompress(\
    zstandard.ZstdCompressor().compress(s)) == s, 'zstd round trip'; \
print('content decoders:', brotli.__version__, zstandard.__version__)"

ENV VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:/usr/local/cargo/bin:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin

# --- The delivery registry ---------------------------------------------------
# Built with --ban, so the retired crates are skipped before the first fetch, and
# with --pin against the DELIVERY manifest, so the result must equal the registry
# the agent had payload for payload and digest for digest. The environment builds
# the full registry and then retires from it, because it needs actix once to
# compile the oracle; `retire` is pure omission of payloads and index entries, so
# banning up front lands on the same bytes by a shorter route -- and this image
# never holds an actix .crate at any point in its history.
#
# warmup/ is copied to a build-only path and deleted at the end. lock-target.lock
# is the SUPERSET lock naming both frameworks; nothing that names actix should
# survive into a grading image, even unread.
COPY data/warmup /tmp/srb-warmup
COPY data/manifest-target.toml /opt/srb/manifest-target.toml
COPY data/scripts/srb-registry-tool /usr/local/bin/srb-registry-tool

RUN set -eu; \
    chmod +x /usr/local/bin/srb-registry-tool; \
    mkdir -p /opt/srb; \
    python3 /tmp/srb-warmup/build-registry.py \
        --lock /tmp/srb-warmup/lock-baseline.lock \
        --lock /tmp/srb-warmup/lock-target.lock \
        --pin  /tmp/srb-warmup/registry-manifest-baseline.json \
        --out  /opt/srb/registry; \
    cp /tmp/srb-warmup/retired-crates.txt              /opt/srb/retired-crates.txt; \
    cp /tmp/srb-warmup/registry-manifest.json          /opt/srb/registry-manifest.json; \
    cp /tmp/srb-warmup/registry-manifest-baseline.json /opt/srb/registry-manifest-baseline.json; \
    srb-registry-tool verify --registry /opt/srb/registry \
                             --manifest /opt/srb/registry-manifest-baseline.json; \
    cp -a /opt/srb/registry /opt/srb/registry-pruned; \
    srb-registry-tool retire --registry /opt/srb/registry-pruned \
                             --retired  /opt/srb/retired-crates.txt; \
    srb-registry-tool verify --registry /opt/srb/registry-pruned \
                             --manifest /opt/srb/registry-manifest.json; \
    srb-registry-tool assert-retired --registry /opt/srb/registry-pruned \
                                     --retired  /opt/srb/retired-crates.txt

# Two registries, and which is which is the point:
#
#   /opt/srb/registry         COMPLETE -- 401 payloads, the actix family
#                             included. cargo resolves against this one, so this
#                             is what stage 2 compiles and grades.
#   /opt/srb/registry-pruned  the agent's delivery registry, reproduced: the same
#                             bytes with the 18 retired names omitted, 384
#                             payloads. Compiled against by nothing; read once,
#                             unscored.
#
# It was one registry, built with --ban, and that made the suite ungradable for
# the one submission whose behaviour defines every expectation in it. State A
# depends on actix-web; `build` runs first and every module needs its binary; a
# module that cannot resolve rates 0. So the repository this suite recorded itself
# from could not be compiled by the suite, and a failure here stopped meaning
# "behaviour changed".
#
# Withholding actix from the *agent* is what enforces the migration, and that is
# untouched -- it lives in environment/, which still derives its delivery registry
# by retiring from the full one. A submission that quietly kept actix could never
# have been compiled or tested while the work was being done.
#
# The pruned copy is derived from the complete one by omission, exactly as the
# environment image derives its delivery registry, rather than warmed a second
# time with --ban: same bytes by construction, so the two registries differ in
# the retirement and in nothing else.
#
# Four independent statements of the same constraint, because it is the one the
# whole task rests on: --pin checks every payload's digest against the baseline
# manifest, `verify` checks the complete registry's index entry count, the second
# `verify` checks the pruned copy against the DELIVERY manifest -- which is how
# "the pruned copy equals the registry the agent had, payload for payload" is
# stated -- and `assert-retired` checks that each retired name resolves to nothing
# there. Any one passing while another fails would mean the two registries and the
# two manifests have diverged.

# Source replacement lives in the global cargo config, exactly as it does in the
# agent image. The submission must stay free to rewrite the repo's own
# .cargo/config.toml -- the baseline uses it for Windows rustflags -- without
# being able to cut itself off from, or reach past, the only crate source here.
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
    > "${CARGO_HOME:-/usr/local/cargo}/config.toml"

# --- State A's manifest, for the warm cache only -----------------------------
# The suite's own authenticated copy of the baseline. It is here for one narrow
# purpose: the warm cache below has to cover miniserve's OWN dependencies -- clap,
# comrak, grass, qrcodegen and the rest -- and manifest-target.toml lists only the
# new stack that replaces actix. Those names are in State A's Cargo.toml and
# nowhere else the grader can reach at image-build time.
#
# Digest and size are asserted inline rather than read from a shipped .sha256, so
# the assertion cannot be satisfied by editing the file beside it. It is the same
# digest environment/original.sha256 records; `swerefactor validate` fails the task
# if the two copies of the tarball ever differ at all.
#
# Only Cargo.toml is kept. The rest of State A is not extracted: this stage grades
# behaviour against a recording made before any submission existed, and has no
# business holding the sources it is grading a rewrite of.
COPY data/original.tar.gz /run/baseline/original.tar.gz

RUN set -eu; \
    echo '90291a2e51704fd74f2096a13e4051e0a799db530b16e311d3ed150763302b87  /run/baseline/original.tar.gz' \
      | sha256sum -c -; \
    test "$(stat -c '%s' /run/baseline/original.tar.gz)" = "156558"; \
    tar --extract --file=/run/baseline/original.tar.gz --directory=/run/baseline \
        --no-same-owner --no-same-permissions repo/Cargo.toml; \
    grep -q '^name = "miniserve"' /run/baseline/repo/Cargo.toml; \
    grep -q '^actix-web' /run/baseline/repo/Cargo.toml; \
    cp /run/baseline/repo/Cargo.toml /opt/srb/baseline-Cargo.toml; \
    rm -rf /run/baseline; \
    echo "baseline manifest extracted; the rest of State A stays in the tarball"

# --- The warm target cache ---------------------------------------------------
# The same closure crate the environment warms /workspace/repo/target with, built
# with the baseline's release profile so the artefacts are reusable. This is a
# fairness measure, not a speed one: the agent developed and timed against a warm
# cache, so grading a cold build would measure a different thing, and would put a
# correct submission near the module's timeout for reasons that have nothing to do
# with the migration -- `lto = true` with `codegen-units = 1` over 340-odd crates
# is where nearly all of that time goes.
#
# --baseline folds in State A's own dependencies and --retired drops the actix
# ones while folding, so the cache covers both halves of what a submission
# compiles: the new stack it moved to, and the dependencies it kept.
#
# The crate is named srb-closure and produces no binary called `miniserve`, which
# is asserted below -- a stale one here would hand a required check to a submission
# that compiled nothing. The build module removes it as well.
RUN set -eu; \
    srb-registry-tool closuretoml --deps     /opt/srb/manifest-target.toml \
                                  --baseline /opt/srb/baseline-Cargo.toml \
                                  --retired  /opt/srb/retired-crates.txt \
                                  --out      /opt/closure/Cargo.toml; \
    ! grep -qi 'actix' /opt/closure/Cargo.toml; \
    cd /opt/closure; \
    if ! CARGO_TARGET_DIR=/opt/srb/warm-target cargo build --offline --release \
            > /tmp/warm.log 2>&1; then \
        echo 'FATAL: the delivery registry cannot build its own closure' >&2; \
        tail -40 /tmp/warm.log >&2; \
        exit 1; \
    fi; \
    grep -c 'Compiling' /tmp/warm.log | sed 's/^/warm cache: units compiled: /'; \
    rm -f /opt/srb/warm-target/release/srb-closure; \
    test ! -e /opt/srb/warm-target/release/miniserve; \
    test ! -e /opt/srb/warm-target/debug/miniserve; \
    chmod -R a+rwX /opt/srb/warm-target; \
    du -sh /opt/srb/warm-target | sed 's/^/warm cache: /'; \
    cd /; rm -rf /opt/closure /tmp/warm.log

ENV SRB_WARM_TARGET=/opt/srb/warm-target

# Each registry must have its role at *resolution* time, not merely in its file
# listing. This compiles nothing; it runs the resolver.
#
# The complete registry has to resolve State A's retired dependencies -- not one
# name at a time, but the four of them together, with State A's own version
# requirements and its own feature choices, because that is the resolve `build`
# performs before every other module runs. A registry that refuses it cannot grade
# the recording's own author. The requirements come out of baseline-Cargo.toml via
# `retiredprobe` rather than being written here: `= "4"` was true of actix-web and
# false of actix-files 0.6.5, actix-multipart 0.6 and actix-web-httpauth 0.8, and a
# literal in a Dockerfile is a second copy of a fact that has somewhere else to
# live. `retiredprobe` also drops the retired names State A never pinned -- the
# list bans routes back to the old stack as well as the old stack itself, so `awc`
# is on it and in no lockfile, and demanding a registry resolve it would assert
# something about the ban list rather than about whether State A can be graded.
#
# The pruned copy must refuse every name on the list, all 18, asked at `*` so no
# version choice can satisfy it. The refusal has to be the resolver's own "no
# matching package named" -- the error a submission cannot route around, because no
# flag re-enables a crate with no index entry.
RUN set -eu; \
    mkdir -p /opt/assert; \
    printf '%s\n' \
        '[source.crates-io]' \
        'replace-with = "srb-pruned"' \
        '' \
        '[source.srb-pruned]' \
        'local-registry = "/opt/srb/registry-pruned"' \
        '' \
        '[net]' \
        'offline = true' \
        > /opt/assert/pruned-config.toml; \
    srb-registry-tool retiredprobe --baseline /opt/srb/baseline-Cargo.toml \
                                   --retired  /opt/srb/retired-crates.txt \
                                   --out      /opt/assert/probe/Cargo.toml; \
    cd /opt/assert/probe; \
    if ! CARGO_TARGET_DIR=/opt/assert-target \
         cargo generate-lockfile --offline > /tmp/c.log 2>&1; then \
        echo 'FATAL: the COMPLETE registry cannot resolve the dependencies State A' >&2; \
        echo '       pins and this stage retires. State A could not be graded here.' >&2; \
        sed -n '1,20p' /opt/assert/probe/Cargo.toml >&2; \
        tail -20 /tmp/c.log >&2; exit 1; \
    fi; \
    grep -c '^name = ' Cargo.lock \
        | sed 's/^/complete: State A'"'"'s retired dependencies resolve, packages in lock: /'; \
    cd /opt/assert; rm -rf /opt/assert/probe; \
    mkdir -p /opt/assert/one/src; \
    printf 'fn main() {}\n' > /opt/assert/one/src/main.rs; \
    cd /opt/assert/one; \
    checked=0; \
    while read -r name; do \
        case "$name" in ''|\#*) continue;; esac; \
        printf '[package]\nname = "a"\nversion = "0.0.0"\nedition = "2021"\npublish = false\n\n[dependencies]\n"%s" = "*"\n' \
            "$name" > Cargo.toml; \
        rm -f Cargo.lock; \
        if CARGO_HOME=/opt/assert-cargo CARGO_TARGET_DIR=/opt/assert-target \
           cargo generate-lockfile --offline \
                --config /opt/assert/pruned-config.toml > /tmp/a.log 2>&1; then \
            echo "FATAL: $name is obtainable from the PRUNED registry at some version" >&2; \
            exit 1; \
        fi; \
        grep -qi 'no matching package named' /tmp/a.log \
            || { echo "FATAL: the pruned registry refused $name for an unexpected reason" >&2; \
                 tail -10 /tmp/a.log >&2; exit 1; }; \
        checked=$((checked + 1)); \
    done < /opt/srb/retired-crates.txt; \
    test "$checked" -gt 0; \
    printf 'pruned: the resolver finds no version of any of the %s retired crates\n' "$checked"; \
    cd /; rm -rf /opt/assert /opt/assert-target /opt/assert-cargo /tmp/a.log /tmp/c.log

# warmup/ has served its purpose. lock-target.lock names actix on fifty lines and
# has no business in an image whose whole job is to grade its absence.
#
# The assertion below is about *names*, and deliberately not about the string
# "actix" appearing anywhere under /opt/srb. Four crates that legitimately belong
# to the closure -- maud, matchit, rstest and rstest_macros -- mention actix
# inside their index JSON, in an optional dependency or a feature table that
# nothing here turns on. That is not a leak: an optional dependency is not
# resolved unless its feature is enabled, and there would be no index entry to
# resolve it to even then, which the step above has just proven crate by crate.
# A substring grep fails on those four, and the next person to hit it deletes the
# assertion instead of reading it. What has to hold is narrower and checkable, and
# it now has two halves:
#
#   PRUNED    no retired name has an index entry or a payload.
#   COMPLETE  every retired name State A's own lockfile pins does have both. This
#             half is what makes State A gradable, and it was silently false until
#             the registry was split.
#
# The second half is deliberately not "every retired name". The list also bans
# names no lockfile here ever resolved -- `awc`, the actix HTTP client, is in
# neither manifest -- because the ban covers routes back to the old stack as well
# as the old stack itself. Requiring those to be present would demand the mirror
# hold crates nothing has ever needed. The baseline manifest is the authority for
# which is which: 401 payloads against the delivery manifest's 384.
#
# What is deleted from the image at the end is warmup/. The pruned registry stays:
# it is read once, unscored, by the `build` module.
RUN set -eu; \
    rm -rf /tmp/srb-warmup; \
    count=$(grep -cve '^[[:space:]]*$' -e '^#' /opt/srb/retired-crates.txt || true); \
    test "${count:-0}" -ge 18 \
        || { echo "FATAL: retired list holds only ${count:-0} names; the ban was a no-op" >&2; \
             exit 1; }; \
    leaked=""; missing=""; owed=0; \
    while read -r name; do \
        case "$name" in ''|\#*) continue;; esac; \
        if [ -n "$(find /opt/srb/registry-pruned/index -type f -name "$name" -print -quit)" ]; then \
            leaked="$leaked index:$name"; \
        fi; \
        if [ -n "$(find /opt/srb/registry-pruned -maxdepth 1 -type f -name "$name-*.crate" -print -quit)" ]; then \
            leaked="$leaked payload:$name"; \
        fi; \
        grep -q "\"$name-[0-9]" /opt/srb/registry-manifest-baseline.json || continue; \
        owed=$((owed + 1)); \
        if [ -z "$(find /opt/srb/registry/index -type f -name "$name" -print -quit)" ]; then \
            missing="$missing index:$name"; \
        fi; \
    done < /opt/srb/retired-crates.txt; \
    if [ -n "$leaked" ]; then \
        echo "FATAL: retired crate reachable in the PRUNED registry:$leaked" >&2; \
        exit 1; \
    fi; \
    if [ -n "$missing" ]; then \
        echo "FATAL: retired crate absent from the COMPLETE registry:$missing" >&2; \
        echo "       State A's lockfile pins these; the complete registry is what" >&2; \
        echo "       grades State A, so a gap here scores the recording's own author 0" >&2; \
        exit 1; \
    fi; \
    printf 'pruned: no index entry and no payload for any of the %s retired crates\n' "$count"; \
    printf 'complete: all %s of them that State A pins are present\n' "$owed"; \
    rm -f /opt/srb/baseline-Cargo.toml

# --- Inputs the harness reads, not the submission ----------------------------
# The TLS material, the credentials file and the sample-tree spec are byte
# copies of the environment's -- `swerefactor validate` fails the task if they
# ever differ. They are read from here rather than from the submitted tree, so a
# submission cannot choose what its TLS and --auth-file sessions are measured
# against, or what directory it is asked to serve.
RUN mkdir -p /opt/srb/tls /opt/srb/auth
COPY data/tls            /opt/srb/tls
COPY data/auth           /opt/srb/auth
COPY data/tree-spec.json /opt/srb/tree-spec.json

# --- The harness -------------------------------------------------------------
COPY --from=infra /opt/swerefactor /opt/swerefactor
ENV PYTHONPATH=/opt/swerefactor

# --- The suite ---------------------------------------------------------------
# Copied last so iterating on a module does not rebuild the registry.
COPY . /tests/behavioural

RUN set -eu; \
    chmod +x /tests/behavioural/lib/run-pytest.sh /tests/behavioural/modules/*/run.sh; \
    test -f /tests/behavioural/data/responses.json.gz; \
    cd /tests/behavioural; \
    PYTHONPATH=/tests/behavioural/lib:/opt/swerefactor \
    SRB_GOLDEN=/tests/behavioural/data/responses.json.gz \
    /opt/venv/bin/python lib/self_check.py

# lib/self_check.py is where the four assertions and the reasons for them live.
# Briefly: the recording is the one these assertions were written against; every
# case is graded by exactly one module; every module imports a battery if and only
# if it has cases on that battery's axis; and suite.toml names fourteen module
# directories that exist. Each has a failure mode that looks like success at
# grading time, which is why they run here.

# Byte-compiling every module is the cheapest possible syntax check, and it also
# means the first pytest process of a grading run is not the thing that discovers
# a typo in lib/.
RUN find /tests/behavioural/lib /tests/behavioural/modules -name '*.py' -print0 \
      | xargs -0 /opt/venv/bin/python -m py_compile \
 && find /tests/behavioural -name '__pycache__' -type d -prune -exec rm -rf {} + \
 && echo "every module and library file compiles"

WORKDIR /tests/behavioural
