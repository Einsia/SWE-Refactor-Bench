# =============================================================================
# SWERefactorBench -- pf03-quickjs-byteorder-port
# Stage 3 image: the verification probe.
#
#     docker build -t swerefactor/pf03-verification:1 \
#         tasks/pf03-quickjs-byteorder-port/tests/verification
#
# Six models, an hour each, trying to write a test the original passes and the
# submission fails.  So this is the only image that has to build BOTH trees for
# ALL THREE targets -- six builds -- and it therefore carries the same three
# toolchains and two emulators as stage 2.
#
# That is not a loophole.  Stage 1 has already judged whether the port is real by
# reading; stage 2 has already measured 458 recorded cases against answers frozen
# before any submission existed.  What this image adds is the ability to ask the
# original a question at grading time, which is the entire point of the stage: a
# candidate's claim that "the original does X" is worth nothing unless the
# original is right there to demonstrate it.
#
# THE ONE THING THIS IMAGE HAS THAT NO OTHER STAGE DOES
#
# /opt/reference: upstream QuickJS, built for x86-64 here, at image build time,
# read-only.  It is the ground truth for what an expression is worth, and
# srbqjs.reference() is the only way a candidate reaches it.
#
# Why it is baked rather than built at run time: computing reference answers while
# grading would mean running a submission's `make` in the same container while the
# answers were being decided.  A Makefile is a program.  The same reasoning froze
# stage 2's answer sheets, and it applies with more force here, where the
# reference is what decides whether a break is upheld.
#
# Why it is x86-64 only, and why srbqjs.reference() takes no target argument: on
# s390x the original is WRONG.  That is the defect the task exists to remove.  A
# candidate asserting "the submission must print on s390x what the original prints
# on s390x" would pass on the original, fail on every correct submission, and
# reproduce every time -- every mechanical condition for an upheld break, while
# asserting the bug.  There is deliberately no supported way to ask for it: the
# probe's first deny clause forbids the claim, the prompt says so in its own body,
# check-probe.py fails this build if either stops being true, and the image
# contains no cross build of the original for a candidate to reach around them.
#
# The cross compilers here are also what a candidate uses for its own helpers:
# srbqjs.CProgram links a candidate's C against a target's libquickjs.a, which is
# the only route to JS_WriteObject, JS_ReadObject and JS_WRITE_OBJ_BSWAP -- none
# of them reachable from a command line, and none of them touched by stage 2.
# They cannot be used to supply C the submission is missing: all six builds happen
# before any candidate runs, and nothing in srbqjs rebuilds one.
#
# At runtime the harness provides:
#
#     /workspace/repo    the submission                            (read-only)
#     /opt/original      State A, unpacked from original.tar.gz    (read-only)
#     /logs/verifier     where the stage result JSON is written
#
# run-candidate.sh copies each tree before building, so what stage 2 measured is
# not what stage 3 mutates, and the two trees are staged under salted per-run
# tokens rather than under their roles.
#
# Network: reaches the model API for the six rounds and the adjudicator, and
# nothing else.  A candidate's own runs get SRB_NETWORK=denied and QuickJS has no
# package manager to reach for.
# =============================================================================

ARG INFRA_IMAGE=swerefactor/infra:1
ARG REPO_SHA256=8fce443dc18e01f25f64246f067bee346b7a5ed340c1372d653d4f7a4fa8d03e

FROM ${INFRA_IMAGE} AS infra

# The same digest environment/Dockerfile and tests/behavioural/Dockerfile pin:
# debian:bookworm-slim = Debian 12, gcc 12.2.0, binutils 2.40, qemu-user 7.2.
# One base across all three images, so a build that succeeds for the agent
# succeeds here, and a divergence between the two trees is never the compiler's.
FROM debian:bookworm-slim@sha256:7b140f374b289a7c2befc338f42ebe6441b7ea838a042bbd5acbfca6ec875818 AS toolchain

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=UTC

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Stage 2's closure, plus python3-venv for the candidate stack and ca-certificates
# for the model API.  Identical compiler and libc lists to stage 2 and to the
# environment image: a package one image has and another lacks would make a build
# failure here mean something other than a build failure there.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      build-essential \
      binutils \
      make \
      pkg-config \
      file \
      xz-utils \
      bzip2 \
      diffutils \
      python3 \
      python3-venv \
      gcc-s390x-linux-gnu \
      g++-s390x-linux-gnu \
      libc6-dev-s390x-cross \
      gcc-arm-linux-gnueabihf \
      g++-arm-linux-gnueabihf \
      libc6-dev-armhf-cross \
      qemu-user-static \
      ca-certificates \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*.deb /var/cache/apt/*.bin

# The same three assertions the other two images make.  A different gcc major
# changes libm results and code generation, and every value stage 2 froze was
# measured under 12; a candidate comparing this image's original against stage 2's
# frozen answers has to be looking at the same compiler.
#
# The JS engine assertion is stage 2's, with one deliberate exception: the
# reference build below installs a `qjs` under /opt/reference.  That is this
# task's own interpreter, built from the archive whose digest is checked, and it
# is not on PATH.  What the loop still rules out is a SECOND, unrelated engine
# that a candidate or a submission could answer with.
RUN set -eu; \
    for cc in gcc s390x-linux-gnu-gcc arm-linux-gnueabihf-gcc; do \
      v="$("$cc" -dumpversion | cut -d. -f1)"; \
      [ "$v" = "12" ] || { echo "$cc major version $v, expected 12" >&2; exit 1; }; \
    done; \
    qemu-s390x-static --version >/dev/null; \
    qemu-arm-static --version >/dev/null; \
    for tool in node nodejs deno bun d8 js jsc duktape mujs; do \
      if command -v "$tool" >/dev/null 2>&1; then \
        echo "unexpected JS engine in grading image: $tool" >&2; exit 1; \
      fi; \
    done

# ---------------------------------------------------------------------------
# The reference: upstream QuickJS, built for x86-64, sealed.
#
# Digest, byte size and member count are all checked, exactly as stage 2 checks
# them.  The digest is the authority; the other two are there so a truncated or
# repacked archive that somehow matched would still be caught.
#
# Built with the argv instruction.md publishes, minus the cross goals -- this is
# the native build and there is no host/target split to make.  CONFIG_LTO= for the
# same reason the published argv passes it: LTO makes a link an optimisation pass
# over the whole program, and a reference should be the plainest build of the
# source, not the cleverest.
# ---------------------------------------------------------------------------
FROM toolchain AS reference

ARG REPO_SHA256

COPY --chown=0:0 data/original.tar.gz /run/reference/original.tar.gz

RUN set -eux; \
    echo "${REPO_SHA256}  /run/reference/original.tar.gz" | sha256sum -c -; \
    test "$(stat -c '%s' /run/reference/original.tar.gz)" = "924884"; \
    test "$(tar -tzf /run/reference/original.tar.gz | wc -l)" = "68"

RUN set -eux; \
    mkdir -p /opt/reference; \
    tar -xzf /run/reference/original.tar.gz -C /opt/reference --strip-components=1; \
    cd /opt/reference; \
    make CONFIG_LTO= -j"$(nproc)" \
        qjs qjsc libquickjs.a tests/bjson.so \
        examples/point.so examples/fib.so examples/hello examples/test_fib; \
    rm -rf /run/reference

# What the reference must be able to do before it is sealed.  A reference that
# does not run is worse than no reference: every candidate's first assertion --
# "this passes on the original" -- would fail, and six rounds would report
# nothing found for a reason that has nothing to do with the submission.
#
# The blob path is checked explicitly, because that is the mechanism the whole
# task is about and `examples/hello` is where it ships.
RUN set -eux; \
    cd /opt/reference; \
    test "$(./qjs -e 'print(1n<<200n)')" = "1606938044258990275541962092341162602522202993782792835301376"; \
    test "$(./qjs -e 'var b=new ArrayBuffer(4),d=new DataView(b); d.setUint32(0,0x01020304,true); print(d.getUint32(0,false))')" = "67305985"; \
    test "$(./examples/hello)" = "Hello World"; \
    ./examples/test_fib >/dev/null; \
    (cd tests && ../qjs test_bjson.js >/dev/null); \
    ./qjsc -e -o /tmp/blob.c examples/hello.js; \
    gcc -O2 -D_GNU_SOURCE -DCONFIG_BIGNUM -I/opt/reference -o /tmp/blob \
        /tmp/blob.c /opt/reference/libquickjs.a -lm -ldl -lpthread; \
    test "$(/tmp/blob)" = "Hello World"; \
    rm -f /tmp/blob /tmp/blob.c

# Sealed.  The stage runs as root because it has to cross-compile and execute what
# it built, so the mode bits are a guard rail rather than a boundary -- but they
# mean an accidental write fails loudly instead of silently changing what every
# candidate in the stage believes the right answer is.
RUN chmod -R a-w /opt/reference

# ---------------------------------------------------------------------------
# The probe.
# ---------------------------------------------------------------------------
FROM reference AS probe

# --- The candidate's test stack ----------------------------------------------
# pytest and pytest-timeout, at one set of versions for both trees.  A candidate
# that ran under different pytest versions on the two trees would not be comparing
# the two builds.  Nothing else: a candidate imports srbqjs and the standard
# library, and everything beyond that it writes in JavaScript or C.
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
# The archive is dropped after the reference is built: /opt/reference is the copy
# a candidate is supposed to reach, and two copies of the original inside one
# image is an invitation to reach the wrong one.  The Dockerfile goes for the
# reason stage 2's does -- a stage image should not carry its own recipe into a
# grading run.
COPY . /tests/verification

RUN set -eux; \
    rm -rf /tests/verification/data; \
    rm -f /tests/verification/Dockerfile /tests/verification/.dockerignore; \
    chmod +x /tests/verification/run-candidate.sh; \
    bash -n /tests/verification/run-candidate.sh; \
    /opt/venv/bin/python -m py_compile /tests/verification/lib/srbqjs.py; \
    python3 /tests/verification/check-probe.py; \
    find /tests/verification -name __pycache__ -type d -prune -exec rm -rf {} +

# srbqjs must import in the interpreter a candidate actually runs in, and it must
# fail with a legible message rather than a NameError when the environment the
# runner sets is absent.  Both are checked here because the first candidate of the
# first round is an expensive place to discover a typo in a module every candidate
# imports.
RUN set -eux; \
    SRB_TREES=/tmp/none SRB_REFERENCE=/opt/reference \
      PYTHONPATH=/tests/verification/lib \
      /opt/venv/bin/python -c "\
import srbqjs; \
assert srbqjs.TARGETS == ('x86_64', 's390x', 'armhf'), srbqjs.TARGETS; \
assert srbqjs.BIG_ENDIAN['s390x'] and not srbqjs.BIG_ENDIAN['armhf']; \
assert srbqjs.WORD_BITS['armhf'] == 32; \
import inspect; \
sig = inspect.signature(srbqjs.reference); \
assert 'target' not in sig.parameters, \
    'reference() grew a target parameter; the original is wrong on s390x and its ' \
    'cross answers are not evidence of anything'; \
print('srbqjs ok:', len(srbqjs.__all__), 'names')"; \
    out=$(PYTHONPATH=/tests/verification/lib /opt/venv/bin/python -c \
      "import srbqjs" 2>&1 || true); \
    case "$out" in \
      *SRB_TREES*) echo "srbqjs reports its own missing environment" ;; \
      *) echo "srbqjs failed unhelpfully without SRB_TREES: $out" >&2; exit 1 ;; \
    esac

ENV TZ=UTC \
    LC_ALL=C.UTF-8 \
    LANG=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    SRB_REFERENCE=/opt/reference \
    SWEREFACTOR_REPO=/workspace/repo \
    SWEREFACTOR_LOG_DIR=/logs/verifier

RUN mkdir -p /opt/original /workspace /logs/verifier

WORKDIR /tests/verification
