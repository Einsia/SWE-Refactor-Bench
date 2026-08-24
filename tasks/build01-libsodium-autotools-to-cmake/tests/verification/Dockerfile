# =============================================================================
# SWERefactorBench -- build01-libsodium-autotools-to-cmake
# Stage 3 image: the verification probe.
#
#     docker build -t swerefactor/build01-verification:1 \
#         tasks/build01-.../tests/verification
#
# Six models, an hour each, trying to write a test the Autotools build passes
# and the CMake one fails.  So this is the only one of build01's three images that
# can configure EITHER build system, and it therefore ships both: autoconf,
# automake, libtool and m4 next to cmake and ninja.
#
# That reads like a loophole and is the opposite of one.  Stage 1 has already
# judged whether Autotools left the submission; stage 2 runs in an image where
# autoconf is not installable, asserts its absence at build time, and puts
# exit-127 stubs for the whole toolchain first on PATH.  Both of those verdicts
# are in before this image starts.  What this image adds is the ability to ask
# State A a question at grading time -- because a candidate's claim that "the
# Autotools build ships this and the CMake build does not" is worth nothing
# unless the Autotools build is right here to demonstrate it.  A stage that
# could not build the original could only take an adversary's word for what the
# original does.
#
# The C compiler is also what a candidate uses for its own consumers.  srbsodium's
# CProgram compiles a candidate's C against an install using flags from that
# install's own libsodium.pc, which is how the ABI, the symbol visibility, the
# static archive and the run-time CPU dispatch get attacked -- none of them is
# reachable from a command line.  binutils is here for the same reason: the
# per-file ISA partition is only visible by extracting the shipped archive and
# disassembling its members.
#
# Same pinned base as the agent image and as stage 2, because what a candidate
# measures is compiler-dependent.  The macros configure.ac probes, the flags that
# reach each translation unit, the symbols visibility leaves in the table and the
# instructions in each object are all answers gcc gives.  A different gcc would
# make State A's own answers different, and every candidate that compared the two
# trees would be comparing two compilers instead.
#
# At runtime the harness provides:
#
#     /workspace/repo    the submission                            (read-only)
#     /opt/original      State A, unpacked from original.tar.gz    (read-only)
#     /logs/verifier     where the stage result JSON is written
#
# The stage copies both trees before anything is built, so what stage 2 measured
# is not what stage 3 mutates, and both copies are writable: this stage builds in
# the source tree's neighbourhood on purpose, because where a build writes is one
# of the things a candidate is allowed to ask about.
#
# Network: reaches the model API for the six rounds and the adjudicator, and
# nothing else.  That matters more here than in the other tasks -- autogen.sh
# fetches config.guess and config.sub over curl when it can, so State A's own
# bootstrap would otherwise vary with what savannah.gnu.org served that morning.
# DO_NOT_UPDATE_CONFIG_SCRIPTS pins it shut regardless, and curl is not installed.
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

# --- Both toolchains ---------------------------------------------------------
# The union of the agent image's build tools and the reading tools this stage
# needs.  Unlike stage 2 there is nothing forbidden here; the assertion below is
# the inverse one, that everything both build systems need is actually present,
# because a missing autoconf here would not fail the stage -- it would silently
# make every candidate "fail on the original" and hand the submission 60 points.
RUN apt-get update && apt-get install -y --no-install-recommends \
      gcc g++ libc6-dev make \
      autoconf automake libtool m4 \
      cmake ninja-build \
      pkg-config \
      binutils file \
      python3 python3-venv \
      xz-utils bzip2 gzip tar \
      coreutils findutils diffutils \
      jq less ripgrep tree \
    && rm -rf /var/lib/apt/lists/* \
    && for required in cc gcc make cmake ctest cpack ninja pkg-config \
                       autoconf automake autoreconf aclocal libtoolize m4 \
                       ar nm objdump readelf strings file python3; do \
         command -v "$required" >/dev/null 2>&1 || { \
           echo "FATAL: the stage-3 image is missing $required." >&2; \
           echo "This stage builds BOTH trees. A tool the ORIGINAL needs and" >&2; \
           echo "does not have makes every candidate fail on the original," >&2; \
           echo "which reads as 'nobody found anything' and pays 60 points." >&2; \
           exit 1; }; \
       done \
    && cmake --version | head -1 \
    && ninja --version \
    && gcc --version | head -1 \
    && autoconf --version | head -1 \
    && automake --version | head -1 \
    && libtoolize --version | head -1 \
    && python3 --version

# --- The candidate's test stack ----------------------------------------------
# pytest and pytest-timeout, hash-pinned, at one set of versions for both trees.
# A candidate running under different pytest versions on the two halves of its
# comparison would not be comparing the two build systems.
#
# Nothing else.  A candidate imports srbsodium and the standard library; anything
# beyond that it writes in C and compiles against the install under test, which
# is the point.  Debian's own python3-pytest is deliberately not used -- stage 2
# uses it because its modules are the benchmark's own code, but a candidate's
# stack should be the same three versions in every task.
COPY requirements-candidate.txt /opt/pins/requirements-candidate.txt

RUN set -eux; \
    python3 -m venv /opt/venv; \
    /opt/venv/bin/pip install --no-cache-dir --upgrade "pip==24.3.1"; \
    /opt/venv/bin/pip install --no-cache-dir \
        --require-hashes -r /opt/pins/requirements-candidate.txt; \
    /opt/venv/bin/python -c "import pytest, pytest_timeout; print('candidate stack ok')"

# --- The harness -------------------------------------------------------------
# Runs the six rounds and the adjudicator.  The venv above is what candidates
# run in; this is the system python.  The process orchestrating the stage does not
# share an interpreter with code an adversary wrote.
COPY --from=infra /opt/swerefactor /opt/swerefactor
ENV PYTHONPATH=/opt/swerefactor

# --- The probe ---------------------------------------------------------------
COPY . /tests/verification

RUN set -eux; \
    chmod +x /tests/verification/run-candidate.sh; \
    bash -n /tests/verification/run-candidate.sh; \
    /opt/venv/bin/python -m py_compile /tests/verification/lib/srbsodium.py; \
    # srbsodium is the whole capability surface of this stage: five named
    # configurations, and the one piece of code that knows how each build system
    # spells them.  A candidate that cannot import it can do nothing, and an
    # __all__ entry that does not exist is a name the prompt advertises and the
    # module does not have.
    SRB_TARGET=/nonexistent SRB_TARGET_TOKEN=check \
    PYTHONPATH=/tests/verification/lib /opt/venv/bin/python -c "\
import sys; import srbsodium as m; \
missing = [n for n in m.__all__ if not hasattr(m, n)]; \
sys.exit(f'srbsodium.__all__ names missing: {missing}') if missing else None; \
print('srbsodium ok: %d configurations, %d exports' \
      % (len(m.CONFIGURATIONS), len(m.__all__)))"; \
    # A scope-less probe is unwinnable and a mismatched adversary count creates
    # points nobody can earn.  Checked here as well as by `swerefactor validate`,
    # because this is the image that would otherwise discover it six rounds in.
    python3 /tests/verification/check-probe.py; \
    find /tests/verification -name __pycache__ -type d -prune -exec rm -rf {} +

# DO_NOT_UPDATE_CONFIG_SCRIPTS: autogen.sh curls config.guess and config.sub from
# savannah when it can.  The agent image sets this for the same reason -- State A's
# bootstrap has to be the same bootstrap every time it runs.
ENV DO_NOT_UPDATE_CONFIG_SCRIPTS=1 \
    CMAKE_BUILD_PARALLEL_LEVEL=4 \
    TZ=UTC

RUN mkdir -p /opt/original /workspace /logs/verifier

WORKDIR /tests/verification
