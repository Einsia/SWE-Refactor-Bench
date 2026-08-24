# =============================================================================
# SWERefactorBench -- lang02-zlib-c-to-java
# Stage 3 image: the verification probe.
#
#     docker build -t swerefactor/lang02-verification:1 \
#       tasks/lang02-zlib-c-to-java/tests/verification
#
# Six models, an hour each, trying to write a test that C zlib passes and the
# Java port fails.  So this is the only image that has to build BOTH trees: the
# original from C with cc and CMake, the submission from Java with javac and the
# same published CMake argv.  It therefore carries both toolchains.
#
# That is not a loophole.  Stage 2 runs in an image where repository C fails to
# compile at all -- the shimmed compiler rejects any compile action on a repository
# source and records it -- and has already measured whether the submission needs
# one; stage 1 has already judged whether the C left.  What this image adds is the
# ability to ask C zlib a question at grading time, which is the entire point of
# the stage: a candidate's claim that "the original does X" is worth nothing unless
# the original is right there to demonstrate it.
#
# Nor is it asymmetric.  Both trees are configured, built and installed by the same
# run-candidate.sh with the same argv, and that script picks a probe by looking at
# what the install produced rather than by reading which tree it was handed.
# Shimming the C compiler for one tree and not the other would itself be a leak:
# the two builds would fail in different ways for reasons that have nothing to do
# with the port.
#
# --- The probe -----------------------------------------------------------------
#
# A same-language task can ship one probe binary and point it at either install.
# This one cannot: the original exports a C ABI and the submission ships a jar, so
# there are two probe programs -- probe.c and Probe.java -- writing the same record
# format, and run-candidate.sh compiles whichever one the install can answer.  Both
# are byte-identical to stage 2's copies and that is checked below.
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
# nothing else.  There is no dependency fetch here -- CMake, javac and cc all work
# offline against what is already in the image -- so a submission cannot acquire
# something here that it could not acquire in the agent's own environment.
# =============================================================================

ARG INFRA_IMAGE=swerefactor/infra:1
FROM ${INFRA_IMAGE} AS infra

FROM debian@sha256:26f2a7cab45014541c65f9d140ccfa6aaefbb49686c6759bea9c6f7f5bb3d72f

ARG DEBIAN_FRONTEND=noninteractive
ARG DEBIAN_SNAPSHOT=20251020T000000Z

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# The same base digest, the same snapshot and the same pins as the environment and
# behavioural images, so the C the original builds from here is the C it builds from
# anywhere else in this benchmark, and the Java the submission builds from here is
# the Java the agent built.
#
# Beyond the behavioural stage's closure: python3-venv for the candidates' pytest,
# and the reading tools the adversaries use on the source.  binutils is absent --
# the class-file and jar forensics belong to stage 2, and this stage compares
# behaviour.
#
# gzip is already in the base image and is not reinstalled, but it is asserted
# below: executor.py stages gzip-produced streams and validates gz output with
# `gzip -t`, so its absence would surface as candidates failing on both trees.
RUN set -eux; \
    rm -f /etc/apt/sources.list /etc/apt/sources.list.d/*; \
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
      gcc=4:12.2.0-3 \
      libarchive13=3.6.2-1+deb12u3 \
      libc6-dev=2.36-9+deb12u13 \
      libjsoncpp25=1.9.5-4 \
      librhash0=1.4.3-3 \
      libuv1=1.44.2-1+deb12u1 \
      make=4.3-4.1 \
      openjdk-17-jdk-headless=17.0.16+8-1~deb12u1 \
      pkg-config=1.8.1-1 \
      python3=3.11.2-1+b1 \
      python3-venv=3.11.2-1+b1 \
      diffutils \
      file \
      jq \
      less \
      ripgrep \
      tree; \
    rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*.deb /var/cache/apt/*.bin

# What was installed is now asserted, separately from having asked for it, and for
# the same reason the behavioural image does it: a pinned apt line can still be
# satisfied by a different upstream if the snapshot is repointed, and the JDK is
# the one tool where that would be invisible.  A JDK 21 that accepts `--release 17`
# would compile Probe.java and run it, and the comparison would silently be against
# a different java.base than the one the agent targeted.
#
# JAVA_HOME is asserted as a path because run-candidate.sh resolves javac and java
# through it, not through PATH -- deliberately, so that a submission's CMakeLists
# has to find the compiler itself.
#
# /usr/bin/cc is asserted as the path run-candidate.sh compiles probe.c with.  It
# is gcc via the alternatives symlink; naming the path here is what makes a broken
# alternatives link a failed build rather than a stage where every C-side probe
# fails to compile and reads as "the original does not answer".
RUN set -eux; \
    test -d /usr/lib/jvm/java-17-openjdk-amd64; \
    /usr/lib/jvm/java-17-openjdk-amd64/bin/javac --version | grep -Fx 'javac 17.0.16'; \
    /usr/lib/jvm/java-17-openjdk-amd64/bin/java  --version | head -1 \
      | grep -Fx 'openjdk 17.0.16 2025-07-15'; \
    cmake --version | head -1 | grep -Fx 'cmake version 3.25.1'; \
    make --version | head -1 | grep -F 'GNU Make 4.3'; \
    test -x /usr/bin/cc; \
    /usr/bin/cc -dumpfullversion | grep -Fx '12.2.0'; \
    /usr/bin/python3 --version | grep -F 'Python 3.11.2'; \
    gzip --version | head -1 | grep -F 'gzip 1.12'

# --- The candidate's test stack ----------------------------------------------
# pytest and pytest-timeout, at one set of versions for both trees.  A candidate
# that ran under different pytest versions on the two trees would not be comparing
# the two implementations.  Nothing else: a candidate imports srbzlib and the
# standard library, and everything it needs beyond that it expresses as probe ops.
COPY requirements-candidate.txt /opt/pins/requirements-candidate.txt

RUN set -eux; \
    /usr/bin/python3 -m venv /opt/venv; \
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

# The four files this stage shares with stage 2, checked against the digest list
# that ships in environment/ and in both stages' data/.
#
# Neither stage can COPY from the other, so the copies are duplicated by hand and
# `swerefactor validate` does not compare files under a stage's lib/.  This is the
# check that closes it.  It matters more here than anywhere else in the task: stage
# 3 concludes that a candidate passing on the original and failing on the
# submission found a real difference, and that conclusion is void if the two halves
# of the comparison were built from probes that had drifted apart.
RUN set -eux; \
    cd /tests/verification/lib; \
    grep -v '^#' /tests/verification/data/shared-lib.sha256 | sha256sum -c -

RUN set -eux; \
    chmod +x /tests/verification/run-candidate.sh; \
    bash -n /tests/verification/run-candidate.sh; \
    /opt/venv/bin/python -m py_compile \
        /tests/verification/lib/srbzlib.py \
        /tests/verification/lib/executor.py \
        /tests/verification/lib/vlib.py; \
    # A scope-less probe is unwinnable and a mismatched adversary count creates
    # points nobody can earn.  Checked here as well as by `swerefactor validate`,
    # because this is the image that would otherwise discover it six rounds in.
    #
    # check-probe.py also cross-checks the op grammar in prompt.txt against both
    # probe halves.  That table is the only statement of the grammar an adversary
    # ever sees -- its tools reach the two repository trees and not /tests -- so an
    # op named there that the probe does not have spends an hour of budget
    # collecting `badop`, and a verb the dispatch chain does not recognise runs the
    # default branch instead and looks like agreement.
    /usr/bin/python3 /tests/verification/check-probe.py

# Neither probe half can be compiled in this image, and it is worth saying where
# each one's compilability is actually established.
#
# probe.c needs zlib.h, which is deliberately absent here: no zlib1g-dev is
# installed, so nothing in this image can compile or link against a system zlib
# instead of against the tree under test.  It is compiled at *stage 2's* build
# time -- freeze.py builds it against the reference and runs it to record the
# frozen answers -- on bytes the digest check above proves are these bytes.  That
# is a stronger check than a syntax pass would be, and it is already paid for.
#
# Probe.java needs the org.zlib module, which exists only once a tree has been
# built and installed, so it cannot be compiled at any image's build time.  Its
# first compile is against the submission's own jar, in run-candidate.sh, which
# then runs the launcher once with --list-keys before any candidate sees it: a
# question whose answer is the same on both trees, so the smoke test proves the
# launcher starts and the library loads without leaking which tree it is.
#
# So the failure this image cannot rule out is "Probe.java does not compile", and
# run-candidate.sh reports it as its own exit status (73) rather than as a
# candidate result.

# Bytecode last: py_compile ran above and every python3 invocation since could have
# written more.  An assertion placed before the last step that can violate it
# passes vacuously.  Stale .pyc beside an edited .py is a verifier grading with code
# nobody can read -- and here it would be a candidate importing a stale srbzlib.
RUN set -eux; \
    find /tests/verification /opt/swerefactor -name __pycache__ -type d -prune \
      -exec rm -rf {} +; \
    found="$(find /tests/verification /opt/swerefactor \
               \( -name '__pycache__' -o -name '*.pyc' \) -print)"; \
    test -z "$found" || { echo "bytecode shipped: $found" >&2; exit 1; }

ENV JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64 \
    CMAKE_BUILD_PARALLEL_LEVEL=4 \
    TZ=UTC \
    LC_ALL=C.UTF-8 \
    LANG=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=0 \
    SWEREFACTOR_LOG_DIR=/logs/verifier

RUN mkdir -p /opt/original /workspace /logs/verifier

WORKDIR /tests/verification
