# syntax=docker/dockerfile:1
# =============================================================================
# SWERefactorBench -- pf01-sqlite-wasi-port
# Stage 2 image: the behavioural suite.
#
#     docker build -t swerefactor/pf01-behavioural:1 \
#         tasks/pf01-sqlite-wasi-port/tests/behavioural
#
# The build context is this directory, so the suite is in the image and nothing
# else is.  The agent container never sees it.
#
# The toolchain list below is duplicated verbatim from environment/Dockerfile, and
# has to be: the submission writes ./build-wasi.sh in the agent container and this
# image runs it, so a tool present on one side and missing on the other is a
# submission that builds for the agent and fails for the scorer through no fault of
# its own.
#
# What this image freezes at build time
# -------------------------------------
# The reference.  State A is unpacked from data/original.tar.gz -- byte-identical
# to the payload the agent was handed -- and native sqlite3 3.31.1 is built from it
# by the same documented recipe the agent image used.  The result and its
# provenance live at /opt/reference.
#
# That is deliberate, and it is the more important half of this file's design.  A
# reference built at *run* time would mean a broken compiler, a full disk or a slow
# link on grading day produces a stage where all 2,653 comparisons fail -- which is
# indistinguishable, in the result file, from a submission that ported nothing.
# Freezing it here turns that whole class of accident into a `docker build`
# failure, before any submission is involved and before any number is published.
#
# At runtime the harness provides:
#
#     /workspace/repo    the submission                        (read-write copy)
#     /opt/original      State A, unpacked from original.tar.gz     (read-only)
#     /logs/verifier     where the stage result JSON is written
#
# /opt/original is not what the reference was built from and is not used by this
# stage.  It is mounted for every stage uniformly; here the equivalent bytes are
# already in the image, verified by digest.
# =============================================================================

ARG INFRA_IMAGE=swerefactor/infra:1
FROM ${INFRA_IMAGE} AS infra

FROM debian:bookworm-slim@sha256:7b140f374b289a7c2befc338f42ebe6441b7ea838a042bbd5acbfca6ec875818

# Digest of the vendored State A payload.  The same ARG and the same value as the
# agent image: a mismatch fails this build rather than scoring a submission
# against a reference built from a different tree.
ARG ORIGINAL_SHA256=928d7a4dba8359bbac94e515a75e7898fe66f732a308eee1520b495356e407e1

# The pinned toolchain.  Fixed release artifacts, never "latest".  Same URLs and
# same digests as environment/SHA256SUMS.
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
# The agent image's list, verbatim, and it has to stay that way: the submission
# writes ./build-wasi.sh in that image and this one runs it, so a tool present
# there and missing here fails a submission for the verifier's reason.  `git` is on
# the list for exactly that reason and for no other -- nothing in this stage uses
# it, but the agent image installs it for the baseline snapshot, so a build script
# that shells out to it would build for its author and not for the scorer.
#
# tcl is not optional: SQLite's build system is Tcl, and generating the
# amalgamation -- which this stage requires, because generating it is where the
# source-list edits are exercised -- does not work without tclsh.
#
# zlib1g-dev is the host zlib, distinct from the cross-built wasm one: this stage
# builds the reference shell with SQLITE_HAVE_ZLIB, and bookworm ships libz.so.1
# without a header or a linker symlink, so `gcc -lz` fails without it.
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

ENV WASI_SDK_PATH=/opt/wasi-sdk \
    WASI_DEPS=/opt/wasi-deps \
    PATH=/opt/wasi-sdk/bin:/opt/wasmtime:/usr/local/bin:/usr/bin:/bin

# The exact toolchain the platform expectations were measured against.  A silently
# updated clang or wasmtime would produce a module scored against measurements from
# a different one.
RUN set -eu; \
    clang --version | head -1; \
    wasmtime --version; \
    clang --version | head -1 | grep -q '18\.1\.2-wasi-sdk' \
      || { echo "FATAL: expected clang 18.1.2-wasi-sdk" >&2; exit 1; }; \
    wasmtime --version | grep -q '24\.0\.0' \
      || { echo "FATAL: expected wasmtime 24.0.0" >&2; exit 1; }

# --- the suite's own inputs ---------------------------------------------------
# data/ holds the State A payload and the build recipes, byte-identical to
# environment/'s copies -- the benchmark's layout check enforces that, and it is
# what makes "the reference was built from what the agent got" a fact rather than
# an intention.
COPY data /tests/behavioural/data

# --- cross-built zlib ---------------------------------------------------------
# Load-bearing for the shell's `.archive` command and invisible to
# `pragma compile_options`, which is why it is built here rather than left for the
# submission to discover it needs.  Same recipe and same prefix as the agent image:
# the submission links against that libz.a and this stage relinks against this one,
# so two different files would fail a submission for the verifier's reason.
RUN set -eu; \
    curl -fsSL -o /tmp/zlib.tar.gz "${ZLIB_URL}"; \
    echo "${ZLIB_SHA256}  /tmp/zlib.tar.gz" | sha256sum -c -; \
    chmod +x /tests/behavioural/data/scripts/build-wasi-zlib.sh; \
    /tests/behavioural/data/scripts/build-wasi-zlib.sh /tmp/zlib.tar.gz /opt/wasi-deps; \
    rm -f /tmp/zlib.tar.gz; \
    test -f /opt/wasi-deps/lib/libz.a; \
    test -f /opt/wasi-deps/include/zlib.h

# --- no wasm module may pre-exist ---------------------------------------------
# The submission's artefact is the only .wasm in this image.  A stray one would be
# something the `artifact` module might parse instead, and something a submission
# could conceivably be scored against instead of its own build.
RUN set -eu; \
    if find / -xdev -name '*.wasm' -print -quit 2>/dev/null | grep -q .; then \
      echo "FATAL: image ships a .wasm file:" >&2; \
      find / -xdev -name '*.wasm' 2>/dev/null | head -5 >&2; \
      exit 1; \
    fi

# --- the reference, frozen ----------------------------------------------------
# Unpack State A, verify it by digest, and build native sqlite3 from it with the
# documented recipe.  One recipe for the agent's reference and this one, so a
# disagreement between them is a bug in the recipe rather than a surprise on
# grading day.
#
# The id it reports ends in `alt1`, and so does a correct port's: mksourceid
# appends one fixed suffix whenever the manifest inventory fails, and it fails here
# because the payload drops three manifest-listed files and in the port because
# src/os_unix.c is required to be gone.  Same string, different reasons -- which is
# what lets the two source-id cases be measured against this binary instead of
# written down as constants in the suite.
#
# The digest is checked twice on purpose.  ORIGINAL_SHA256 is pinned in this
# file, so swapping the payload *and* its digest file still fails; original.sha256
# is the file the rest of the benchmark verifies against, so checking it here
# proves the shipped digest file describes the shipped payload.
RUN set -eu; \
    cd /tests/behavioural/data; \
    echo "${ORIGINAL_SHA256}  original.tar.gz" | sha256sum -c -; \
    sha256sum -c original.sha256; \
    mkdir -p /opt/reference; \
    tar -xzf original.tar.gz -C /opt/reference \
        --no-same-owner --no-same-permissions; \
    test -d /opt/reference/repo; \
    mv /opt/reference/repo /opt/reference/src; \
    test -f /opt/reference/src/src/os_unix.c; \
    test -f /opt/reference/src/src/os_win.c; \
    chmod +x scripts/build-native-oracle.sh; \
    scripts/build-native-oracle.sh /opt/reference/src /opt/reference/sqlite3; \
    test -x /opt/reference/sqlite3; \
    # The recipe accepts either source-id tail, because a local experiment may pass a
    # pristine upstream checkout.  This image may not: two of the cases are the
    # source id itself, and a reference reporting the canonical tail would fail them
    # for every submission, correct ones included.
    id=$(/opt/reference/sqlite3 :memory: 'select sqlite_source_id();'); \
    case "$id" in \
      *837balt1) : ;; \
      *) echo "FATAL: the reference reports '$id'." >&2; \
         echo "  The behavioural suite measures the two source-id cases against this" >&2; \
         echo "  binary, and a correct port reports the alt1 tail.  A reference that" >&2; \
         echo "  does not was built from something other than the shipped payload." >&2; \
         exit 1 ;; \
    esac

# --- the harness --------------------------------------------------------------
COPY --from=infra /opt/swerefactor /opt/swerefactor

# lib/ is on the path so the modules import the suite's helpers by name;
# /opt/swerefactor is there for the runner's own imports.
ENV PYTHONPATH=/opt/swerefactor:/tests/behavioural/lib

# --- the suite ----------------------------------------------------------------
# Copied last so that editing a module does not rebuild the toolchain or the
# reference.
COPY . /tests/behavioural

# The payload has served its purpose -- the reference is built and /opt/original is
# mounted at run time -- so 11 MB of it does not need to sit in every graded image,
# and a second copy of State A inside the suite is a second thing that can drift.
# The digest file stays: it is small and it says what the reference came from.
RUN set -eu; \
    rm -f /tests/behavioural/data/original.tar.gz \
          /tests/behavioural/Dockerfile /tests/behavioural/.dockerignore; \
    chmod +x /tests/behavioural/modules/*/run.sh; \
    find /tests/behavioural -name __pycache__ -type d -prune -exec rm -rf {} +; \
    python3 -m compileall -q /tests/behavioural/lib >/dev/null

# Record what the reference is, so every module reads one file instead of asking
# the binary again, and so a reader of a stage log can tell what was compared.
RUN set -eu; \
    PYTHONPATH=/tests/behavioural/lib python3 -c "\
import json, pathlib, sys; \
sys.path.insert(0, '/tests/behavioural/lib'); \
import builds; \
p = builds.describe('/opt/reference/sqlite3'); \
pathlib.Path('/opt/reference/provenance.json').write_text(json.dumps(p, indent=1)); \
print('reference:', p['version'], p['source_id']); \
print('options:', len(p['compile_options']), 'reported'); \
sys.exit('the reference is not 3.31.1') if p['version'] != '3.31.1' else None"

# --- build-time self-checks ---------------------------------------------------
# Three things that are easy to break and would otherwise be discovered by a
# submission rather than by a build.
RUN set -eu; \
    # 1. The case set.  catalog.py pins an exact count and an exact operation
    #    count per module; a builder that silently lost cases fails here, where the
    #    message names the module, rather than at grading time where it would look
    #    like a short run.
    PYTHONPATH=/tests/behavioural/lib python3 -c "\
import sys; sys.path.insert(0, '/tests/behavioural/lib'); \
import catalog; \
sets = catalog.build_all(); \
print('cases:', {k: len(v) for k, v in sets.items()}); \
print('total:', catalog.TOTAL_CASES, 'operations:', catalog.TOTAL_OPERATIONS)"; \
    # 2. The manifest against the modules.  A duplicate id, a malformed weight or a
    #    module directory that does not exist fails the build, not the run.
    PYTHONPATH=/opt/swerefactor python3 -c "\
import sys; from swerefactor import config; \
s = config.Suite.load('/tests/behavioural/suite.toml'); \
missing = [m.id for m in s.modules if not (s.root / (m.dir or f'modules/{m.id}')).is_dir()]; \
sys.exit(f'suite.toml names missing module dir(s): {missing}') if missing else None; \
print(f'suite: {len(s.modules)} modules, weight {s.weight_total:g}')"; \
    # 3. suite.toml and catalog.py agree about which modules exist.  They are
    #    edited separately and a module declared in one and not the other is either
    #    an unscored case set or a module with nothing to run.
    PYTHONPATH=/opt/swerefactor:/tests/behavioural/lib python3 -c "\
import sys; sys.path.insert(0, '/tests/behavioural/lib'); \
from swerefactor import config; import catalog; \
s = config.Suite.load('/tests/behavioural/suite.toml'); \
declared = {m.id for m in s.modules}; \
cases = set(catalog.module_ids()); \
extra = cases - declared; \
sys.exit(f'catalog builds cases for undeclared module(s): {sorted(extra)}') if extra else None; \
print('case modules:', sorted(cases)); \
print('other modules:', sorted(declared - cases))"

# 4. The verdict contract, round-tripped through the runner's own reader.
#
# The failure this closes is silent and total.  `Check.from_dict` reads
# `verdict`; an entry carrying `{"ok": bool}` instead has none and reads as
# `error`.  A suite whose modules all compose their checks that way rates every
# module 0.0 and returns 0/40 for any submission whatsoever -- a perfect port
# included, and the reference compared against itself.  Nothing fails loudly: the
# result files are well-formed, the modules exit as expected, and the number is
# the absence of a key.  2,669 checks, one missing key.
#
# Asserted two ways, because either alone can be satisfied while the bug is back.
# The mapping is round-tripped through the real `Check` class rather than a local
# reimplementation of it, and every file that names $SRB_RESULT must go through
# report.py, since a new module writing json.dumps directly is how the shape gets
# reintroduced.
RUN set -eu; \
    PYTHONPATH=/opt/swerefactor:/tests/behavioural/lib python3 -c "\
import sys; sys.path.insert(0, '/tests/behavioural/lib'); \
import report; from swerefactor.result import Check; \
want = {'pass': [{'id': 'a', 'ok': True}, {'id': 'b', 'verdict': 'pass'}], \
        'fail': [{'id': 'c', 'ok': False}], \
        'skip': [{'id': 'd', 'verdict': 'skip', 'ok': False}], \
        'error': [{'id': 'e'}]}; \
bad = [(raw, Check.from_dict(out).verdict, expect) \
       for expect, raws in want.items() \
       for raw, out in zip(raws, report.normalise(raws)) \
       if Check.from_dict(out).verdict != expect]; \
sys.exit(f'report.normalise does not produce the runner\'s verdicts: {bad}') if bad else None; \
print('verdicts: ok/verdict ->', ', '.join(sorted(want)), 'via Check.from_dict')"; \
    for f in /tests/behavioural/lib/*.py; do \
      case $(basename "$f") in report.py) continue;; esac; \
      grep -q SRB_RESULT "$f" && { \
        echo "FATAL: $f names \$SRB_RESULT.  report.write is the only writer of" >&2; \
        echo "  the result file; a check written straight to json carries no" >&2; \
        echo "  verdict key and the runner scores it as an error, whatever the" >&2; \
        echo "  module decided.  Call report.write(result) instead." >&2; \
        exit 1; }; \
    done; \
    echo "result writers: report.py alone names \$SRB_RESULT"

# Every shell script the runner will exec has to parse.  A syntax error in a
# module's run.sh would otherwise surface as "the module wrote no checks".
RUN set -eu; \
    for script in /tests/behavioural/modules/*/run.sh; do \
      bash -n "$script" || { echo "FATAL: $script does not parse" >&2; exit 1; }; \
    done

# The environment the modules see.  It is pinned rather than inherited so that two
# runs of the same submission compare the same way: the shell reports the locale in
# `.dump` output, and a container that happened to have LANG set differently would
# produce a diff that says nothing about the port.  execute.py re-imposes these on
# each child process; setting them here means the modules themselves agree too.
ENV LANG=C \
    LC_ALL=C \
    TZ=UTC \
    PYTHONPATH=/opt/swerefactor:/tests/behavioural/lib \
    SRB_REFERENCE=/opt/reference/sqlite3

# Where the harness mounts the submission, State A and the log directory.  Created
# here so a bind mount lands on an existing directory owned by root.
RUN mkdir -p /opt/original /workspace /logs/verifier

WORKDIR /tests/behavioural
