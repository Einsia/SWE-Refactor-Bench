# syntax=docker/dockerfile:1
# =============================================================================
# SWERefactorBench -- pf01-sqlite-wasi-port
# Stage 3 image: the verification probe.
#
#     docker build -t swerefactor/pf01-verification:1 \
#         tasks/pf01-sqlite-wasi-port/tests/verification
#
# Six models, an hour each, trying to write a test that native SQLite 3.31.1
# passes and the wasm port fails.  So this is the only image that has to build BOTH
# trees at grading time: the original with configure/make/gcc, the submission with
# its own build-wasi.sh under wasi-sdk.  It therefore carries both toolchains, and
# the cross-built zlib the submission links against.
#
# That is not a loophole.  Stage 1 has already judged, by reading, whether the POSIX
# layer left; stage 2 has already measured how much of the port works, in an image
# where the reference is frozen.  What this image adds is the ability to ask 3.31.1
# a question at grading time, which is the entire point of the stage: a candidate's
# claim that "the original does X" is worth nothing unless the original is right
# there to demonstrate it.
#
# Both builds happen at run time, from copies the harness makes of the two trees.
# Freezing the native side into the image would have been cheaper and it would have
# been worse: "the original passes this candidate" then stops being a claim about
# the tree the adversary actually read.
#
# The one thing that IS frozen here is a fixed native 3.31.1 at /opt/native-3311,
# built from data/original.tar.gz.  It is not the reference for anything -- it is a
# second reader, offered to candidates identically on both sides of every
# comparison, for handing a file the build under test wrote to an implementation
# that had no part in writing it.
#
# At runtime the harness provides:
#
#     /workspace/repo    the submission                            (read-only)
#     /opt/original      State A, unpacked from original.tar.gz    (read-only)
#     /logs/verifier     where the stage result JSON is written
#
# The stage copies both trees before building anything, so what stage 2 measured is
# not what stage 3 mutates.
#
# Network: reaches the model API for the six rounds and the adjudicator, and
# nothing else.  Every toolchain and both source trees are already here.
# =============================================================================

ARG INFRA_IMAGE=swerefactor/infra:1
FROM ${INFRA_IMAGE} AS infra

FROM debian:bookworm-slim@sha256:7b140f374b289a7c2befc338f42ebe6441b7ea838a042bbd5acbfca6ec875818

# Digest of the vendored State A payload.  The same ARG and the same value as the
# agent image and the behavioural image: a mismatch fails this build rather than
# handing six adversaries a cross-reader built from a different tree.
ARG ORIGINAL_SHA256=928d7a4dba8359bbac94e515a75e7898fe66f732a308eee1520b495356e407e1

# The pinned toolchain.  Fixed release artifacts, never "latest".  Same URLs and
# same digests as environment/SHA256SUMS, because the point of building the
# submission again here is to build the same thing stage 2 measured.
ARG WASI_SDK_URL=https://github.com/WebAssembly/wasi-sdk/releases/download/wasi-sdk-24/wasi-sdk-24.0-x86_64-linux.tar.gz
ARG WASI_SDK_SHA256=c6c38aab56e5de88adf6c1ebc9c3ae8da72f88ec2b656fb024eda8d4167a0bc5
ARG WASMTIME_URL=https://github.com/bytecodealliance/wasmtime/releases/download/v24.0.0/wasmtime-v24.0.0-x86_64-linux.tar.xz
ARG WASMTIME_SHA256=27b4dff2ec7ab3148c73504f029f281bb78e0cea45d978f74e9f8c1d5585f8e6
ARG ZLIB_URL=https://zlib.net/fossils/zlib-1.2.11.tar.gz
ARG ZLIB_SHA256=c3e5e9fdd5004dcb542feda5ee4f0ff0744628baf8ed2dd5d66f8ca1197cb1a1

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=UTC \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONHASHSEED=0

# --- toolchain ----------------------------------------------------------------
# Stage 2's list, plus python3-venv for the candidate stack below.  Stage 2's list
# is the agent image's list, `git` included, for the reason stated there: this stage
# also runs the submission's build script, so the two images have to offer it the
# same tools.  tcl is not optional: SQLite's build system is Tcl and generating the
# amalgamation -- which both builds do -- does not work without tclsh.  zlib1g-dev is
# the host zlib, not the cross-built wasm one: both native builds here pass
# SQLITE_HAVE_ZLIB, and bookworm's base libz.so.1 has no header and no linker symlink.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      binutils \
      build-essential \
      ca-certificates \
      curl \
      file \
      git \
      make \
      pkg-config \
      python3 \
      python3-venv \
      tcl \
      xz-utils \
      zlib1g-dev \
 && rm -rf /var/lib/apt/lists/* \
 && ln -snf /usr/share/zoneinfo/UTC /etc/localtime && echo UTC > /etc/timezone

RUN set -eu; \
    major="$(gcc -dumpversion | cut -d. -f1)"; \
    if [ "$major" != "12" ]; then \
      echo "FATAL: expected gcc 12 (bookworm), got $(gcc --version | head -1)" >&2; \
      exit 1; \
    fi

# --- cross toolchain ----------------------------------------------------------
RUN set -eu; \
    curl -fsSL -o /tmp/wasi-sdk.tar.gz "${WASI_SDK_URL}"; \
    echo "${WASI_SDK_SHA256}  /tmp/wasi-sdk.tar.gz" | sha256sum -c -; \
    mkdir -p /opt/wasi-sdk; \
    tar -xzf /tmp/wasi-sdk.tar.gz -C /opt/wasi-sdk --strip-components=1; \
    rm -f /tmp/wasi-sdk.tar.gz; \
    test -x /opt/wasi-sdk/bin/clang; \
    test -d /opt/wasi-sdk/share/wasi-sysroot

RUN set -eu; \
    curl -fsSL -o /tmp/wasmtime.tar.xz "${WASMTIME_URL}"; \
    echo "${WASMTIME_SHA256}  /tmp/wasmtime.tar.xz" | sha256sum -c -; \
    mkdir -p /opt/wasmtime; \
    tar -xJf /tmp/wasmtime.tar.xz -C /opt/wasmtime --strip-components=1; \
    rm -f /tmp/wasmtime.tar.xz; \
    test -x /opt/wasmtime/wasmtime

# /opt/wasmtime/wasmtime is one of the two absolute paths run-candidate.sh writes
# into spec.json, and lib/srbsqlite.py builds every wasm argv around it.  It is on
# PATH as well because a submission's build script may invoke it for its own
# checks, and stage 2's PATH is the one it was written against.
ENV WASI_SDK_PATH=/opt/wasi-sdk \
    WASI_DEPS=/opt/wasi-deps \
    PATH=/opt/wasi-sdk/bin:/opt/wasmtime:/usr/local/bin:/usr/bin:/bin

# The exact toolchain stage 2 measured against.  A silently updated clang would
# build the submission differently here than there, and a round could then find a
# divergence that belongs to the compiler.
RUN set -eu; \
    clang --version | head -1; \
    wasmtime --version; \
    clang --version | head -1 | grep -q '18\.1\.2-wasi-sdk' \
      || { echo "FATAL: expected clang 18.1.2-wasi-sdk" >&2; exit 1; }; \
    wasmtime --version | grep -q '24\.0\.0' \
      || { echo "FATAL: expected wasmtime 24.0.0" >&2; exit 1; }

# --- the probe's own inputs ---------------------------------------------------
# data/ holds the State A payload and the build recipes, byte-identical to
# environment/'s copies and to the behavioural stage's.  build-native-oracle.sh is
# needed twice: here, to build the cross-reader, and at grading time, to build the
# pre-migration tree the harness mounted.
COPY data /tests/verification/data

# --- cross-built zlib ---------------------------------------------------------
# The submission links libz.a from $WASI_DEPS, so a stage that meant to rebuild the
# submission has to carry it.  Same recipe and same prefix as the agent image: a
# different libz here would fail a build for the verifier's reason.
RUN set -eu; \
    curl -fsSL -o /tmp/zlib.tar.gz "${ZLIB_URL}"; \
    echo "${ZLIB_SHA256}  /tmp/zlib.tar.gz" | sha256sum -c -; \
    chmod +x /tests/verification/data/scripts/build-wasi-zlib.sh; \
    /tests/verification/data/scripts/build-wasi-zlib.sh /tmp/zlib.tar.gz /opt/wasi-deps; \
    rm -f /tmp/zlib.tar.gz; \
    test -f /opt/wasi-deps/lib/libz.a; \
    test -f /opt/wasi-deps/include/zlib.h

# --- the cross-reader ---------------------------------------------------------
# A fixed native 3.31.1, built from the shipped payload, at the second absolute
# path spec.json names.  srbsqlite exposes it as NATIVE_3311 and
# Sandbox.native(), and the prompt tells adversaries to use it in both directions.
#
# It is not a reference and nothing is scored against it.  Its job is to be an
# implementation that had no part in writing a file: a candidate that wants to know
# whether the port's database is a valid 3.31.1 database asks this binary, on both
# trees, and compares the two answers to each other rather than to a constant.
# Which is why it is built here rather than at run time -- it must be the same
# binary on both sides of every comparison, and the same one in all three reruns.
#
# The digest is checked twice on purpose.  ORIGINAL_SHA256 is pinned in this file,
# so swapping the payload *and* its digest file still fails; original.sha256 is the
# file the rest of the benchmark verifies against, so checking it here proves the
# shipped digest file describes the shipped payload.
#
# Stage 2 additionally requires the source id to end in `alt1`.  That gate is
# deliberately not copied: it is there because two of the behavioural cases are the
# source id itself, so a reference reporting the canonical tail would fail them for
# every submission.  Nothing here is scored against this binary, and the recipe
# already refuses to produce anything but 3.31.1 -- it checks the date and the
# leading 60 hex digits of the id and accepts only the two tails that tree can
# legitimately report.  The version check below is a second reading of the same
# fact, kept because it costs one query and it is the property that actually
# matters here: what file format this binary speaks.
RUN set -eu; \
    cd /tests/verification/data; \
    echo "${ORIGINAL_SHA256}  original.tar.gz" | sha256sum -c -; \
    sha256sum -c original.sha256; \
    mkdir -p /opt/native-3311; \
    tar -xzf original.tar.gz -C /opt/native-3311 \
        --no-same-owner --no-same-permissions; \
    test -d /opt/native-3311/repo; \
    test -f /opt/native-3311/repo/src/os_unix.c; \
    chmod +x scripts/build-native-oracle.sh; \
    scripts/build-native-oracle.sh /opt/native-3311/repo /opt/native-3311/sqlite3; \
    test -x /opt/native-3311/sqlite3; \
    ver=$(/opt/native-3311/sqlite3 :memory: 'select sqlite_version();'); \
    if [ "$ver" != "3.31.1" ]; then \
      echo "FATAL: the cross-reader reports $ver, not 3.31.1." >&2; \
      echo "  It is handed files the build under test wrote, at a format version" >&2; \
      echo "  the task pins.  A different version reads them differently." >&2; \
      exit 1; \
    fi; \
    # The source tree has served its purpose.  Removed so the image holds one
    # native binary rather than a second copy of State A -- the tree under test
    # arrives on a mount at run time, and two candidate trees on disk is one more
    # thing a reader of a transcript has to disambiguate.
    rm -rf /opt/native-3311/repo

# --- the candidate's test stack -----------------------------------------------
# pytest and pytest-timeout, at one set of versions for both trees.  A candidate
# that ran under different pytest versions on the two trees would not be comparing
# the two implementations.  Nothing else: a candidate imports srbsqlite and the
# standard library, and everything it needs beyond that it expresses as SQL, as a
# dot-command, or as bytes it writes into the sandbox.
COPY requirements-candidate.txt /opt/pins/requirements-candidate.txt

RUN set -eu; \
    python3 -m venv /opt/venv; \
    /opt/venv/bin/pip install --no-cache-dir --upgrade "pip==24.3.1"; \
    /opt/venv/bin/pip install --no-cache-dir \
        --require-hashes -r /opt/pins/requirements-candidate.txt; \
    /opt/venv/bin/python -c "import pytest, pytest_timeout; print('candidate stack ok')"

# --- the harness --------------------------------------------------------------
# Runs the six rounds and the adjudicator.  The venv above is what candidates
# run in; this is the system python.  The process orchestrating the stage does not
# share an interpreter with code an adversary wrote.
COPY --from=infra /opt/swerefactor /opt/swerefactor
ENV PYTHONPATH=/opt/swerefactor

# --- the probe ----------------------------------------------------------------
# Copied last so that editing the prompt or the helper does not rebuild two
# toolchains and the cross-reader.
COPY . /tests/verification

RUN set -eu; \
    chmod +x /tests/verification/run-candidate.sh; \
    # run-candidate.sh execs this one at grading time, so the bit has to survive
    # whatever the tree travelled through to get here.
    chmod +x /tests/verification/data/scripts/build-native-oracle.sh; \
    bash -n /tests/verification/run-candidate.sh \
      || { echo "FATAL: run-candidate.sh does not parse" >&2; exit 1; }; \
    # Compiled by the interpreter that will import it, not by the harness's.
    /opt/venv/bin/python -m py_compile /tests/verification/lib/srbsqlite.py; \
    # A scope-less probe is unwinnable and a mismatched adversary count creates
    # points nobody can earn.  check-probe.py also cross-checks every helper the
    # prompt advertises against the helper module, because a prompt promising a
    # function that was renamed costs a round its first candidates.  Checked here as
    # well as by `swerefactor validate`, because this is the image that would
    # otherwise discover it six rounds in.
    python3 /tests/verification/check-probe.py; \
    # The payload has served its purpose: the cross-reader is built and the tree
    # under test arrives on a mount.  11 MB of it does not need to sit in the image,
    # and a second copy of State A is a second thing that can drift.  The digest
    # file stays -- it is small and it says what the cross-reader came from.
    rm -f /tests/verification/data/original.tar.gz \
          /tests/verification/Dockerfile /tests/verification/.dockerignore; \
    find /tests/verification -name __pycache__ -type d -prune -exec rm -rf {} +

# --- no wasm module may pre-exist ---------------------------------------------
# At grading time the only .wasm in this container is the one the submission's own
# build script produces under $SRB_WORK.  A module shipped in the image would be
# something a broken build could be silently measured through, and something a
# candidate could reach instead of the artefact under test.  Run after both COPYs,
# so it covers the probe directory as well as the toolchain.
RUN set -eu; \
    if find / -xdev -name '*.wasm' -print -quit 2>/dev/null | grep -q .; then \
      echo "FATAL: image ships a .wasm file:" >&2; \
      find / -xdev -name '*.wasm' 2>/dev/null | head -5 >&2; \
      exit 1; \
    fi

# The environment both builds and every candidate see.  Pinned rather than
# inherited: the shell reports the locale in `.dump` output, so a container that
# happened to have LANG set differently would produce a diff that says nothing
# about the port.  srbsqlite re-imposes its own copy on each child process, which
# is what makes a candidate's runs reproducible; these are for the builds.
ENV LANG=C \
    LC_ALL=C \
    TZ=UTC \
    PYTHONPATH=/opt/swerefactor

# Where the harness mounts the submission, State A and the log directory.  Created
# here so a bind mount lands on an existing directory owned by root.
RUN mkdir -p /opt/original /workspace /logs/verifier

WORKDIR /tests/verification
