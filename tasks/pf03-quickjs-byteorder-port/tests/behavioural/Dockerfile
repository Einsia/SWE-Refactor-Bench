# =============================================================================
# SWERefactorBench -- pf03-quickjs-byteorder-port
# Stage 2 image: the behavioural suite.
#
#     docker build -t swerefactor/pf03-behavioural:1 \
#         tasks/pf03-quickjs-byteorder-port/tests/behavioural
#
# Built from the digest-pinned base the agent environment uses, with the same
# three toolchains and the same two emulators.  A submission that builds for
# s390x in the environment builds here for the same reasons, and a divergence
# between the two sides of a comparison is never the compiler's.
#
# Four properties of this file are load-bearing.
#
#  1. It carries its own copy of original.tar.gz and never reads the submitted
#     tree to learn what State A was.  The baseline is how the suite tells a
#     correct answer from a plausible one, so it cannot come from the party being
#     graded.  The digest below is the one environment/original.sha256 records;
#     if the two copies ever diverge, one of the two image builds fails on its
#     checksum instead of grading against the wrong baseline.
#
#  2. freeze.py runs here, at build time.  It builds State A for all three
#     targets, runs the eight programs, and records every expected value into an
#     immutable blob under /opt/assets.  By the time a submission exists the
#     answers are already fixed and digested; grading only compares against them.
#
#     This is the difference worth stating plainly.  Computing expectations at
#     grading time would mean running `make` from the submitted tree, as root, in
#     the same container, while the answers were being decided.  A Makefile is a
#     program: it can write outside its own directory, put a compiler earlier on
#     PATH, or edit a sheet collected a minute earlier.  Freezing removes that
#     whole class rather than defending against it, and it halves the stage --
#     three builds instead of six -- so a timeout stops being the thing most
#     likely to fail an honest submission.
#
#  3. Nothing here trusts a build directory the agent left behind.  The `build`
#     module snapshots the submitted tree and builds it in scratch roots of its
#     own, with CFLAGS, CROSS_PREFIX and every SRB_ variable stripped from the
#     environment.
#
#  4. There is no second JavaScript engine, asserted below.  The subject is this
#     runtime; an engine already present would let a submission answer with
#     someone else's interpreter.
#
# At runtime the harness provides:
#
#     /workspace/repo    the submission                        (read-write copy)
#     /opt/original      State A, unpacked from original.tar.gz    (read-only)
#     /logs/verifier     where the stage result JSON is written
#
# The suite reads its baseline from the frozen assets, not from /opt/original --
# that mount is there for the modules that compare trees.  Network: none.
# =============================================================================

# Declared before the first FROM so both the baseline and the freeze stage can
# re-declare it and inherit one default.  An ARG does not cross a FROM, so the
# bare `ARG REPO_SHA256` lines below are what carry it into each stage.
ARG INFRA_IMAGE=swerefactor/infra:1
ARG REPO_SHA256=8fce443dc18e01f25f64246f067bee346b7a5ed340c1372d653d4f7a4fa8d03e

FROM ${INFRA_IMAGE} AS infra

# Same digest as environment/Dockerfile: debian:bookworm-slim = Debian 12,
# gcc 12.2.0, binutils 2.40, qemu-user 7.2.
FROM debian:bookworm-slim@sha256:7b140f374b289a7c2befc338f42ebe6441b7ea838a042bbd5acbfca6ec875818 AS toolchain

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=UTC

# The environment image's closure, minus the agent's conveniences (no vim, no
# less, no git) and plus what grading itself needs: `file` and binutils for the
# ELF machine checks the build module makes, and coreutils' stat for the
# provenance assertions below.  A package the environment has and this image
# lacks would make a build failure here mean something other than a build
# failure there, so the compiler and libc lists are identical.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      build-essential \
      binutils \
      make \
      pkg-config \
      file \
      xz-utils \
      bzip2 \
      python3 \
      gcc-s390x-linux-gnu \
      g++-s390x-linux-gnu \
      libc6-dev-s390x-cross \
      gcc-arm-linux-gnueabihf \
      g++-arm-linux-gnueabihf \
      libc6-dev-armhf-cross \
      qemu-user-static \
      ca-certificates \
 && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*.deb /var/cache/apt/*.bin

# The same three assertions the environment image makes, for the same reason: a
# different gcc major changes libm results and code generation, and every value
# frozen below is measured under 12.  Asserted in both images so the two cannot
# drift apart silently.
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
# Baseline: the suite's own authenticated copy of State A.
#
# Digest, byte size and member count are all checked.  The digest is the
# authority; the other two are there so a truncated or repacked archive that
# somehow matched would still be caught, and so a reader of this file can see
# what the archive is supposed to be without unpacking it.
# ---------------------------------------------------------------------------
FROM toolchain AS baseline

ARG REPO_SHA256

COPY --chown=0:0 data/original.tar.gz /run/baseline/original.tar.gz

RUN set -eu; \
    echo "${REPO_SHA256}  /run/baseline/original.tar.gz" | sha256sum -c -; \
    test "$(stat -c '%s' /run/baseline/original.tar.gz)" = "924884"; \
    test "$(tar -tzf /run/baseline/original.tar.gz | wc -l)" = "68"

# ---------------------------------------------------------------------------
# Freeze: build State A three ways, run the programs, seal the answers.
#
# /opt/assets sits beside /opt/swerefactor rather than inside it: the harness there
# is shared by every task and arrives from the donor image, and one directory
# should not be half vendored code and half this task's answers.
#
# The archive and this Dockerfile are removed from the image afterwards.  The
# archive because the runtime mount at /opt/original is the copy the modules are
# supposed to read, and two copies of State A inside one image is an invitation
# to compare against the wrong one; the Dockerfile because a stage image should
# not carry its own recipe into a grading run.
# ---------------------------------------------------------------------------
FROM baseline AS frozen

ARG REPO_SHA256

COPY --chown=0:0 . /tests/behavioural

RUN set -eux; \
    rm -f /tests/behavioural/data/original.tar.gz \
          /tests/behavioural/Dockerfile \
          /tests/behavioural/.dockerignore; \
    find /tests/behavioural/lib -name '*.py' -print0 \
      | xargs -0 /usr/bin/python3 -m py_compile; \
    /usr/bin/python3 -I /tests/behavioural/lib/freeze.py \
      --archive /run/baseline/original.tar.gz \
      --sha256 "${REPO_SHA256}" \
      --assets /opt/assets \
      --workspace /tmp/freeze; \
    rm -rf /tmp/freeze /run/baseline; \
    find /tests/behavioural -name __pycache__ -type d -prune -exec rm -rf {} +

# The frozen assets are the answers, and nothing that runs later has any business
# writing to them.  Grading runs as root because it has to cross-compile and
# execute what it built, so the mode bits are a guard rail rather than a boundary
# -- the manifest digest that driver.py re-checks on every read is the boundary.
# They do mean an accidental write fails loudly instead of silently changing a
# mark.
#
# The floors freeze.py enforces while writing are re-asserted here against the
# manifest that actually shipped: a promise of three answer sheets and several
# hundred admitted keys is worth checking on the artifact, not just on the
# process that produced it.
RUN set -eux; \
    chmod -R a-w /opt/assets; \
    test "$(stat -c '%a' /opt/assets/manifest.json)" = "444"; \
    /usr/bin/python3 -I -c "\
import json, sys; \
m = json.load(open('/opt/assets/manifest.json')); \
c = m['counts']; \
sys.exit('frozen schema is %r' % m['schema']) \
  if m['schema'] != 'swerefactor-pf03-frozen-v2' else None; \
sys.exit('frozen sheets: %d' % len(m['targets'])) if len(m['targets']) != 3 else None; \
sys.exit('admitted keys: %d' % c['admitted']) if c['admitted'] < 300 else None; \
sys.exit('missing keys: %d' % c['missing']) if c['missing'] else None; \
sys.exit('no native keys') if not c['native'] else None; \
print('frozen: %d admitted, %d native, %d excluded, %d files' \
      % (c['admitted'], c['native'], c['excluded'], len(m['files'])))"

# --- The harness -------------------------------------------------------------
# The module runner, the result contract and the scorer, shared by every task.
COPY --from=infra /opt/swerefactor /opt/swerefactor

# --- The manifest, checked at build time -------------------------------------
# suite.toml is the file most likely to be edited and least likely to be tested,
# and the slice table in driver.py is what decides whether a key is graded at
# all.  Both are checked here.  A duplicate module id, a weight that does not
# parse, a module directory that does not exist, a (program, target) pair that no
# module claims, or a native key whose declared layout is not self-consistent
# fails the image build rather than the run.
#
# --self-check needs the program sources, which are in the image, and does not
# need the frozen assets -- it is a statement about this suite's own tables.
RUN set -eux; \
    chmod +x /tests/behavioural/lib/run-module.sh; \
    for d in /tests/behavioural/modules/*/; do \
      test -e "$d/run.sh" || { echo "no runner in $d" >&2; exit 1; }; \
    done; \
    PYTHONPATH=/opt/swerefactor /usr/bin/python3 -c "\
import sys; from swerefactor import config; \
s = config.Suite.load('/tests/behavioural/suite.toml'); \
missing = [m.id for m in s.modules if not (s.root / (m.dir or f'modules/{m.id}')).is_dir()]; \
sys.exit(f'suite.toml names missing module dir(s): {missing}') if missing else None; \
print(f'suite: {len(s.modules)} modules, weight {s.weight_total:g}')"; \
    /usr/bin/python3 -I /tests/behavioural/lib/driver.py --self-check

ENV TZ=UTC \
    LC_ALL=C.UTF-8 \
    LANG=C.UTF-8 \
    PYTHONPATH=/opt/swerefactor \
    PYTHONDONTWRITEBYTECODE=1 \
    SWEREFACTOR_ASSETS=/opt/assets \
    SWEREFACTOR_REPO=/workspace/repo \
    SWEREFACTOR_LOG_DIR=/logs/verifier

RUN mkdir -p /opt/original /workspace /logs/verifier

WORKDIR /tests/behavioural
