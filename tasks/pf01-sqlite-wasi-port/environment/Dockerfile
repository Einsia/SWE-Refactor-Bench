# syntax=docker/dockerfile:1
#
# pf01 -- agent environment.
#
# Builds State A: the SQLite 3.31.1 source distribution, which builds and runs
# perfectly well here for the host, and does not build at all for wasm32-wasi.  That
# second fact is the task, and environment/verify_environment.sh asserts both of them at
# image build time rather than leaving the premise to a README.
#
# The image is built with network access; the trial container runs without it, so the
# whole cross toolchain, the runtime, the cross-built zlib and a native reference build
# are already on disk when the agent starts.
FROM debian:bookworm-slim@sha256:7b140f374b289a7c2befc338f42ebe6441b7ea838a042bbd5acbfca6ec875818

# Digest of the vendored State A payload.  A mismatch fails the image build rather than
# handing the agent a different tree than the verifier scores.
ARG ORIGINAL_SHA256=928d7a4dba8359bbac94e515a75e7898fe66f732a308eee1520b495356e407e1

# The pinned toolchain.  Fixed release artifacts, never "latest": the verifier image
# installs the same four and asserts the same versions, so a submission built against
# one clang is not compared against a binary built by another.  environment/SHA256SUMS
# carries the same digests with their provenance.
ARG WASI_SDK_URL=https://github.com/WebAssembly/wasi-sdk/releases/download/wasi-sdk-24/wasi-sdk-24.0-x86_64-linux.tar.gz
ARG WASI_SDK_SHA256=c6c38aab56e5de88adf6c1ebc9c3ae8da72f88ec2b656fb024eda8d4167a0bc5
ARG WASMTIME_URL=https://github.com/bytecodealliance/wasmtime/releases/download/v24.0.0/wasmtime-v24.0.0-x86_64-linux.tar.xz
ARG WASMTIME_SHA256=27b4dff2ec7ab3148c73504f029f281bb78e0cea45d978f74e9f8c1d5585f8e6
ARG ZLIB_URL=https://zlib.net/fossils/zlib-1.2.11.tar.gz
ARG ZLIB_SHA256=c3e5e9fdd5004dcb542feda5ee4f0ff0744628baf8ed2dd5d66f8ca1197cb1a1

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=UTC

# --- toolchain ----------------------------------------------------------------
# This list is duplicated verbatim in the verifier images.  It has to be: the submission
# writes ./build-wasi.sh here and the verifier runs it there, so any tool present on one
# side and missing on the other is a submission that builds for the agent and fails for
# the scorer through no fault of its own.
#
# `git` is here for the baseline snapshot at the foot of this file and for nothing else.
# The verifier images install it anyway, and only because this one does: a build script
# that shelled out to it would otherwise be the exact failure the paragraph above
# describes.
#
# tcl is not optional and is easy to overlook.  SQLite's build system is Tcl: lemon,
# mkkeywordhash and mksqlite3c.tcl all run under tclsh, and generating the amalgamation
# -- which the verifier requires, because generating it is where the source-list edits
# are exercised -- does not work without it.
#
# zlib1g-dev is the host zlib, and it is not the same thing as the cross-built one a
# hundred lines below.  bookworm ships libz.so.1 with the base system but no header and
# no linker symlink, so `gcc -lz` fails on a runtime-only image; the reference shell is
# built with SQLITE_HAVE_ZLIB (see scripts/build-native-oracle.sh for why that flag is
# not optional), which needs zlib.h at compile time and libz at link time for the host
# architecture.  /opt/wasi-deps/lib/libz.a is wasm32 and gcc cannot link it.
#
# gcc is unpinned within bookworm, whose major version is frozen at 12 for the life of
# the release, so security point updates do not break the image while the compiler the
# task was authored against stays put.  The assertion below makes that mechanical.
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
      tcl \
      xz-utils \
      zlib1g-dev \
 && rm -rf /var/lib/apt/lists/*

RUN ln -snf /usr/share/zoneinfo/UTC /etc/localtime && echo UTC > /etc/timezone

RUN set -eu; \
    major="$(gcc -dumpversion | cut -d. -f1)"; \
    if [ "$major" != "12" ]; then \
      echo "FATAL: expected gcc 12 (bookworm), got $(gcc --version | head -1)" >&2; \
      exit 1; \
    fi

# --- cross toolchain ----------------------------------------------------------
# Verified before unpacking, in both images, from the same digests.
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

ENV WASI_SDK_PATH=/opt/wasi-sdk \
    WASI_DEPS=/opt/wasi-deps \
    PATH=/opt/wasi-sdk/bin:/opt/wasmtime:/usr/local/bin:/usr/bin:/bin

# The clang and wasmtime versions the expectations were recorded against.  Pinned by
# digest above, asserted here, because a toolchain that silently changed would produce
# a module scored against expectations from a different one.
RUN set -eu; \
    clang --version | head -1; \
    wasmtime --version; \
    clang --version | head -1 | grep -q '18\.1\.2-wasi-sdk' \
      || { echo "FATAL: expected clang 18.1.2-wasi-sdk" >&2; exit 1; }; \
    wasmtime --version | grep -q '24\.0\.0' \
      || { echo "FATAL: expected wasmtime 24.0.0" >&2; exit 1; }

# --- cross-built zlib ---------------------------------------------------------
# Load-bearing for the shell's `.archive` command, and invisible to
# `pragma compile_options`, which is exactly why it is built here rather than left for
# the submission to discover it needs.  Same recipe and same prefix in both images: the
# submission links against this libz.a and the verifier relinks against the one it has,
# so two different files would fail a submission for the verifier's reason.
COPY scripts/build-wasi-zlib.sh /opt/scripts/build-wasi-zlib.sh
RUN set -eu; \
    curl -fsSL -o /tmp/zlib.tar.gz "${ZLIB_URL}"; \
    echo "${ZLIB_SHA256}  /tmp/zlib.tar.gz" | sha256sum -c -; \
    chmod +x /opt/scripts/build-wasi-zlib.sh; \
    /opt/scripts/build-wasi-zlib.sh /tmp/zlib.tar.gz /opt/wasi-deps; \
    rm -f /tmp/zlib.tar.gz; \
    test -f /opt/wasi-deps/lib/libz.a; \
    test -f /opt/wasi-deps/include/zlib.h

# --- no existing wasm port on the image ---------------------------------------
# A prebuilt wasm SQLite would be both a spoiler and a shortcut past the whole task.
# Native sqlite3 is a different matter and is deliberately provided further down: it is
# State A's own behaviour, which the port is required to preserve, and a wasm module
# cannot invoke it in any case -- WASI has no process spawn, so the capability the
# import section grants is the capability the module has.
RUN set -eu; \
    if find / -xdev -name '*.wasm' -print -quit 2>/dev/null | grep -q .; then \
      echo "FATAL: image ships a .wasm file:" >&2; \
      find / -xdev -name '*.wasm' 2>/dev/null | head -5 >&2; \
      exit 1; \
    fi; \
    for forbidden in wasm-pack emscripten emcc wasi-libc-sqlite; do \
      if command -v "$forbidden" >/dev/null 2>&1; then \
        echo "FATAL: image ships $forbidden" >&2; exit 1; \
      fi; \
    done

# --- unpack State A -----------------------------------------------------------
WORKDIR /opt/payload
COPY original.tar.gz ./
RUN echo "${ORIGINAL_SHA256}  original.tar.gz" | sha256sum -c -

RUN mkdir -p /workspace \
 && tar -xzf original.tar.gz -C /opt/payload \
 && mv /opt/payload/repo /workspace/repo \
 && rm -f /opt/payload/original.tar.gz \
 && rmdir /opt/payload

# The archive must carry no version-control data.  SQLite is developed in Fossil and its
# later versions are public: an agent that could read history could read a later
# platform layer instead of writing one.  `.fossil-settings/` is on this list for that
# reason, and it is why the payload is three files short of the upstream zip.  That
# shortfall is load-bearing rather than incidental: `tool/mksourceid` checks the
# manifest inventory and appends `alt1` to SQLITE_SOURCE_ID when a listed file is
# missing, so the reference built from this payload reports the same suffix a correct
# port reports.  See scripts/build-native-oracle.sh.
#
# `manifest` and `manifest.uuid` are *not* on this list, and both look exactly like
# fossil debris.  Both are build inputs: tool/mksourceid reads `manifest` to derive
# SQLITE_SOURCE_ID, and ext/fts5/tool/mkfts5c.tcl:59 reads `manifest.uuid` for the hash
# it stamps into fts5.c.  Neither reveals anything about a later version -- one is a
# listing of this checkout, the other its hash -- and both are in state_a_files.txt, so
# forbidding either would fail this build against the payload it is checking.
RUN set -eu; \
    for forbidden in .git .gitignore .gitmodules .gitattributes .github \
                     .fossil-settings .hg .svn; do \
      if [ -e "/workspace/repo/${forbidden}" ]; then \
        echo "FATAL: archive leaked ${forbidden}" >&2; exit 1; \
      fi; \
    done; \
    if find /workspace/repo \( -name '.git*' -o -name '.fossil*' -o -name '.hg' \
                            -o -name '.svn' \) -print -quit | grep -q .; then \
      echo "FATAL: archive contains nested version-control data" >&2; exit 1; \
    fi

# State A is source only.  `manifest` stays -- it is a plain file listing that upstream
# ships and that `tool/mksourceid` reads, not version-control metadata -- and it is
# load-bearing: SQLITE_SOURCE_ID is derived from it at build time.
RUN set -eu; \
    test -f /workspace/repo/manifest; \
    test -f /workspace/repo/VERSION; \
    if find /workspace/repo \( -name '*.o' -o -name '*.a' -o -name '*.so' \
                            -o -name '*.lo' -o -name '*.wasm' \) -print -quit | grep -q .; then \
      echo "FATAL: archive contains prebuilt binaries" >&2; exit 1; \
    fi; \
    if [ -e /workspace/repo/sqlite3.c ]; then \
      echo "FATAL: archive contains a pre-generated amalgamation" >&2; exit 1; \
    fi; \
    for handover in build-wasi.sh sqlite3.wasm; do \
      if [ -e "/workspace/repo/${handover}" ]; then \
        echo "FATAL: ${handover} already exists; producing it is part of the task" >&2; \
        exit 1; \
      fi; \
    done

# The tree matches the inventory the verifier's clean step and closure gates read, file
# for file.  Same file, same digests, in both images.
COPY state_a_files.txt /opt/state_a_files.txt
RUN python3 - <<'PY'
import hashlib, pathlib, sys

root = pathlib.Path("/workspace/repo")
expected = {}
for line in pathlib.Path("/opt/state_a_files.txt").read_text("utf-8").splitlines():
    if not line.strip():
        continue
    digest, _, rel = line.partition("  ")
    expected[rel] = digest

actual = {
    p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
    for p in root.rglob("*")
    if p.is_file()
}
if actual != expected:
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    bad = sorted(k for k in set(expected) & set(actual) if expected[k] != actual[k])
    print("FATAL: State A tree does not match the inventory", file=sys.stderr)
    for label, items in (("missing", missing), ("unexpected", extra), ("altered", bad)):
        if items:
            print(f"  {label}: {len(items)} -> {items[:5]}", file=sys.stderr)
    raise SystemExit(1)
print(f"State A verified: {len(actual)} files against state_a_files.txt")
PY

# --- state the debt mechanically ---------------------------------------------
# Four claims, all checked here so that a change to the base image or the wasi-sdk
# cannot silently alter the task's premise: the native toolchain builds State A, the
# cross toolchain works and its output runs, State A does not compile for wasm32-wasi,
# and the port is reachable through SQLITE_OS_OTHER=1.  See the script for why each one
# has to be asserted rather than assumed.
#
# Every claim is about the *environment*: this toolchain, this sysroot, this source
# tree.  None is about a submission, and the script is deleted in the layer that runs
# it, so the image the agent receives contains no way to ask "would I pass".  The only
# reader of its output is whoever built the image.
COPY verify_environment.sh /opt/verify_environment.sh
RUN chmod +x /opt/verify_environment.sh \
 && WASI_SDK_PATH=/opt/wasi-sdk WASMTIME_BIN=/opt/wasmtime/wasmtime \
    /opt/verify_environment.sh /workspace/repo \
 && rm -f /opt/verify_environment.sh

# --- a native reference build -------------------------------------------------
# State A's own behaviour, as a binary, so "preserve existing behaviour" is something
# the agent can diff against instead of guess at.  Built from the same source in
# /workspace/repo by the same script the verifier uses for its oracle -- one documented
# recipe, so a disagreement between the two is a bug in the recipe and not a surprise.
#
# It reports SQLITE_SOURCE_ID ending in `alt1`, and so does a correct port: `mksourceid`
# appends one fixed suffix whenever the manifest inventory fails, and it fails here
# because the payload drops three manifest-listed files, and in the port because
# src/os_unix.c is required to be gone.  Same string, different reasons, which is what
# makes this build useful as a reference.
COPY scripts/build-native-oracle.sh /opt/scripts/build-native-oracle.sh
RUN set -eu; \
    chmod +x /opt/scripts/build-native-oracle.sh; \
    mkdir -p /opt/oracle; \
    /opt/scripts/build-native-oracle.sh /workspace/repo /opt/oracle/sqlite3; \
    test -x /opt/oracle/sqlite3

# --- tools on PATH ------------------------------------------------------------
# wasi-run pins the invocation contract in one place.  A WASI guest reaches only the
# directories the host preopens for it, so "does my port work" is not a question about
# the port alone -- the same module passes or fails depending on how it was launched,
# and the verifier launches it exactly this way.
COPY scripts/wasi-run scripts/wasm-imports.py /usr/local/bin/
RUN chmod +x /usr/local/bin/wasi-run /usr/local/bin/wasm-imports.py \
 && ln -sf /opt/oracle/sqlite3 /usr/local/bin/sqlite3-native

# Recorded rather than described, so a reader of a trial log can tell which toolchain
# produced it.
RUN { echo "# pf01 toolchain, recorded at image build time"; \
      gcc --version | head -1; \
      clang --version | head -1; \
      wasmtime --version; \
      echo "tclsh $(echo 'puts [info patchlevel]' | tclsh)"; \
      python3 --version; \
      make --version | head -1; \
      echo "native reference: $(/opt/oracle/sqlite3 --version)"; \
    } > /opt/toolchain.txt \
 && cat /opt/toolchain.txt

# --- baseline snapshot --------------------------------------------------------
# One commit, no remote, so the agent can diff its own work and can read nothing else.
# The history contains State A and stops.
COPY gitignore.baseline /workspace/repo/.gitignore
RUN set -eu; \
    cd /workspace/repo; \
    git init -q -b main; \
    git config user.email "task@swerefactorbench.invalid"; \
    git config user.name  "SWERefactorBench"; \
    git config commit.gpgsign false; \
    git add -A; \
    git -c commit.gpgsign=false commit -q \
        -m "State A: SQLite 3.31.1 (upstream source distribution, unmodified)"; \
    git tag state-a; \
    test "$(git rev-list --count HEAD)" = "1"; \
    test -z "$(git remote)"

ENV PF02_STATE_A_TAG=state-a
WORKDIR /workspace/repo
