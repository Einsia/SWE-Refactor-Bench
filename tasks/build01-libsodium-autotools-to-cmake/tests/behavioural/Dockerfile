# =============================================================================
# SWERefactorBench -- build01-libsodium-autotools-to-cmake
# Stage 2 image: the behavioural suite.
#
#     docker build -t swerefactor/build01-behavioural:1 \
#         tasks/build01-.../tests/behavioural
#
# Build context is this directory, so the suite is in the image and nothing else
# is.  The agent container never sees it.
#
# What this stage measures is *preserved capability*: the delivered build system
# installs the same tree, compiles the same 119 units with the same per-file ISA
# flags, exports the same 2689 symbols and passes the same 80 tests as State A.
# Every expectation in `data/` was recorded from State A, so State A has to be
# runnable here -- a stage whose own oracle cannot pass it cannot tell a
# regression from a baseline.  Autotools is therefore installed.
#
# It is not thereby *allowed*.  Whether the migration happened is stage 1's
# question, decided over both trees by a reviewer, and its gates are required:
# `autotools_retired` and `cmake_is_the_build` zero a submission that still
# builds with configure before this image is ever run.  What stays here is the
# recording: the `build` module puts exit-127 stubs for all thirteen Autotools
# names first on PATH for a CMake tree, so a CMake submission that secretly
# shells out to autoreconf does not fail a string check, it fails to configure,
# and the stub records which tool it wanted.
#
# The same pinned base as the agent image, because the ground truth this suite
# compares against (feature macros, ISA flags, generated headers, symbol tables)
# is compiler-dependent.  A different gcc would make State A's own answers wrong.
#
# At runtime the harness provides:
#
#     /workspace/repo    the submission                       (read-write copy)
#     /opt/original      State A, unpacked from original.tar.gz    (read-only)
#     /logs/verifier     where the stage result JSON is written
# =============================================================================

ARG INFRA_IMAGE=swerefactor/infra:1
FROM ${INFRA_IMAGE} AS infra

FROM debian:bookworm-slim@sha256:7b140f374b289a7c2befc338f42ebe6441b7ea838a042bbd5acbfca6ec875818

ENV DEBIAN_FRONTEND=noninteractive \
    LC_ALL=C.UTF-8 \
    LANG=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONHASHSEED=0

# --- The toolchain -----------------------------------------------------------
# cmake + ninja + make + gcc, and the binutils this suite reads objects with.
#
# Plus Autotools, for one reason: State A is the oracle.  Every number this stage
# compares against -- 119 translation units, 15 ISA groups, 2689 symbols, 80
# tests, 67 headers -- was recorded by building State A, and the identity run
# that keeps those numbers honest has to be reproducible inside this image.  The
# agent's own container is a separate image and does not have these packages, so
# nothing here widens what a submission may use.
RUN apt-get update && apt-get install -y --no-install-recommends \
      gcc g++ libc6-dev make \
      cmake ninja-build \
      autoconf automake libtool m4 \
      pkg-config \
      binutils file \
      python3 python3-pip python3-pytest \
      xz-utils bzip2 \
      coreutils findutils diffutils \
    && rm -rf /var/lib/apt/lists/* \
    # Both drivers have to be here, or a whole flavour reports as a build
    # failure and the report reads like a broken submission.
    # `libtoolize` and not `libtool`: on Debian the `libtool` package ships the
    # macros and libtoolize, and /usr/bin/libtool lives in `libtool-bin`.  An
    # Autotools project does not use the system one anyway -- libtoolize copies
    # ltmain.sh in and configure generates a `libtool` script in the build tree.
    && for required in cmake ninja make gcc autoreconf automake libtoolize m4; do \
         command -v "$required" >/dev/null 2>&1 || { \
           echo "FATAL: the stage-2 image needs $required" >&2; exit 1; }; \
       done \
    && cmake --version | head -1 \
    && ninja --version \
    && gcc --version | head -1 \
    && autoconf --version | head -1 \
    && python3 -c "import pytest; print('pytest', pytest.__version__)"

# --- The harness -------------------------------------------------------------
COPY --from=infra /opt/swerefactor /opt/swerefactor

# lib/ is on the path for `-p srbfixtures` and for the helpers the modules
# import by name; /opt/swerefactor is there for `-p swerefactor.pytest_module`.
ENV PYTHONPATH=/opt/swerefactor:/tests/behavioural/lib

# --- The suite ---------------------------------------------------------------
# Copied last so iterating on a module does not rebuild the toolchain layer.
COPY . /tests/behavioural

RUN chmod +x /tests/behavioural/lib/run-pytest.sh /tests/behavioural/modules/*/run.sh \
 # The frozen ground truth has to be present and readable, or every comparison
 # in the suite silently has nothing to compare against.
 && python3 -c "\
import json, pathlib, sys; \
d = pathlib.Path('/tests/behavioural/data'); \
files = sorted(p.name for p in d.glob('*.json')); \
[json.loads(p.read_text()) for p in d.glob('*.json')]; \
sys.exit('no ground truth in data/') if not files else None; \
print(f'ground truth: {len(files)} files, {files}')" \
 # The corpus archive is checked against the digest the suite will re-verify at
 # run time, so a truncated copy fails the build rather than the submission.
 && python3 -c "\
import sys; sys.path.insert(0, '/tests/behavioural/lib'); \
import corpus; \
print(f'corpus: {corpus.CORPUS_TGZ}'); \
sys.exit('corpus digest mismatch') if corpus.sha256_file(corpus.CORPUS_TGZ) \
    != corpus.CORPUS_SHA else None" \
 # The manifest is the thing most likely to be edited and least likely to be
 # tested, so it is checked at build time: a malformed weight, a duplicate id or
 # a module directory that does not exist fails the build, not the run.
 && PYTHONPATH=/opt/swerefactor python3 -c "\
import sys; from swerefactor import config; \
s = config.Suite.load('/tests/behavioural/suite.toml'); \
missing = [m.id for m in s.modules if not (s.root / (m.dir or f'modules/{m.id}')).is_dir()]; \
sys.exit(f'suite.toml names missing module dir(s): {missing}') if missing else None; \
print(f'suite: {len(s.modules)} modules, weight {s.weight_total:g}')"

WORKDIR /tests/behavioural
