# =============================================================================
# SWERefactorBench -- lang02-zlib-c-to-java
# Stage 2 image: the behavioural suite.
#
#     docker build -t swerefactor/lang02-behavioural:1 \
#       tasks/lang02-zlib-c-to-java/tests/behavioural
#
# Built from the same digest-pinned base and the same Debian snapshot as the
# agent environment, so a submission that builds there builds here for the same
# reasons.
#
# Three properties of this file are load-bearing:
#
#  1. It carries its own copy of original.tar.gz and never reads the submitted
#     tree to learn what State A was.  The baseline is how the suite tells an
#     unchanged file from a rewritten one, so it cannot come from the party being
#     graded.  The digest below is the same one environment/original.sha256
#     records; if the two copies ever diverge, one of the two builds fails on its
#     checksum instead of grading against the wrong baseline.
#
#  2. freeze.py runs here, at build time.  It builds the pinned upstream C
#     release from source, generates the corpus, and records the expected output
#     of every case into an immutable blob.  By the time a submission exists the
#     answers are already fixed and digested; grading only compares against them.
#     Computing expectations at grading time would put the submission in the
#     container while the answers were being decided.
#
#  3. Nothing here trusts a build directory the agent left behind.  The `build`
#     module configures both BUILD_SHARED_LIBS values from the submitted source,
#     under a shimmed C compiler, into scratch prefixes of its own.
#
# The C toolchain in this image belongs to the *freeze*, not to the grading.  The
# oracle for a C-to-Java port is what C zlib does, so the reference is compiled
# once, here, from State A -- and then deleted.  gcc survives into the final layer
# only because CMake's compiler-ABI probe has to be able to succeed for a
# submission whose CMakeLists still declares `project(zlib C)`.
#
# At runtime the harness provides:
#
#     /workspace/repo    the submission                       (read-write copy)
#     /opt/original      State A, unpacked from original.tar.gz    (read-only)
#     /logs/verifier     where the stage result JSON is written
#
# The suite reads its baseline from the frozen assets, not from /opt/original --
# that mount is there for the stages that compare trees.  Network: none.
# =============================================================================

ARG INFRA_IMAGE=swerefactor/infra:1
FROM ${INFRA_IMAGE} AS infra

FROM debian@sha256:26f2a7cab45014541c65f9d140ccfa6aaefbb49686c6759bea9c6f7f5bb3d72f AS toolchain

ARG DEBIAN_FRONTEND=noninteractive
ARG DEBIAN_SNAPSHOT=20251020T000000Z

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# The same closure as the environment image, plus what the freeze needs.  Every
# version is pinned against the snapshot above, so this resolves to one set of
# bytes for as long as snapshot.debian.org keeps that timestamp.
#
# sources.list.d/debian.sources is *replaced*, not supplemented: the base image
# ships that file, and writing sources.list alongside it would leave the live
# archive in the resolution set and make these pins advisory.
#
# Four of these are worth naming a reason for:
#
#   make      - CMake is used without -G, so the generator is Unix Makefiles and
#               `cmake --build` shells out to make.  The base image has no make.
#   gcc       - the reference C build, probe.c and consumer.c.  Freeze-time only.
#   python3   - the suite itself.  Not in the base image either.
#   binutils  - the jar and class-file forensics the structure and provenance
#               modules do.
#
# gzip is already in the base image and is not reinstalled, but it is asserted
# below: executor.py stages gzip-produced streams for the decompress cases and
# asks `gzip -t` to validate what minigzip wrote, so its absence would surface as
# failing behavioural cases rather than as a broken image.
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
      binutils=2.40-2 \
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
      python3=3.11.2-1+b1; \
    rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*.deb /var/cache/apt/*.bin

# What was installed is now asserted, separately from having asked for it.  A
# pinned apt line can still be satisfied by a different upstream if the snapshot
# is repointed, and the JDK is the one tool where that would be invisible: a JDK
# 21 that accepts `--release 17` produces class files this task's contract calls
# correct, but its `java.base` signatures are a different API surface and its
# `jar` writes a different Created-By.  So the version is read back from the tool.
#
# JAVA_HOME is asserted as a path, not just as a working javac.  build.py resolves
# javac and jar through JAVA_HOME rather than PATH -- deliberately, so that a
# submission's CMakeLists has to find the compiler itself -- and the contract
# names this directory, so a JDK that installed somewhere else would break the
# graded build in a way that reads as the submission's fault.
RUN set -eux; \
    test -d /usr/lib/jvm/java-17-openjdk-amd64; \
    /usr/lib/jvm/java-17-openjdk-amd64/bin/javac --version | grep -Fx 'javac 17.0.16'; \
    /usr/lib/jvm/java-17-openjdk-amd64/bin/java  --version | head -1 \
      | grep -Fx 'openjdk 17.0.16 2025-07-15'; \
    test -x /usr/lib/jvm/java-17-openjdk-amd64/bin/jar; \
    cmake --version | head -1 | grep -Fx 'cmake version 3.25.1'; \
    make --version | head -1 | grep -F 'GNU Make 4.3'; \
    gcc -dumpfullversion | grep -Fx '12.2.0'; \
    pkg-config --version; \
    readelf --version | head -1; \
    /usr/bin/python3 --version | grep -F 'Python 3.11.2'; \
    gzip --version | head -1 | grep -F 'gzip 1.12'

# ---------------------------------------------------------------------------
# Baseline: the suite's own authenticated copy of State A.
#
# tests/behavioural/ is its own build context and cannot reach ../../environment/,
# so the archive is duplicated in data/ on purpose.  The digests below are the
# same ones the environment image checks; if the two copies ever diverge, one of
# the two builds fails on its checksum rather than grading against the wrong
# baseline.
#
# The contract is checked here as well as the tarball, and for the same reason:
# the environment installs it as the submission's specification while the suite
# grades against it, so the two copies being one file is what makes the task
# well-posed.  freeze.py then re-asserts the contract's pinned constants against
# the reference it just built, which closes the remaining gap -- that the file
# both sides agree on is also *true*.
# ---------------------------------------------------------------------------
FROM toolchain AS baseline

COPY --chown=0:0 data/original.tar.gz /run/baseline/original.tar.gz
COPY --chown=0:0 data/source-contract.json /run/baseline/source-contract.json

RUN set -eux; \
    echo '18809c7fa08d686c3317f4e9858a1c6c0d3ad9f0fe3d7109f985365361dafbc6  /run/baseline/original.tar.gz' \
      | sha256sum -c -; \
    echo '4ecbc79f7d1e579beacf2c3252953a3085aa105ccb8e101f82bd78a39387003b  /run/baseline/source-contract.json' \
      | sha256sum -c -; \
    test "$(stat -c '%s' /run/baseline/original.tar.gz)" = "1177532"; \
    test "$(stat -c '%s' /run/baseline/source-contract.json)" = "64258"; \
    install -d -m 0755 /run/unpack; \
    tar --extract --file=/run/baseline/original.tar.gz --directory=/run/unpack \
        --no-same-owner --no-same-permissions; \
    test -f /run/unpack/repo/CMakeLists.txt; \
    test -f /run/unpack/repo/zlib.map; \
    test -f /run/unpack/repo/zconf.h; \
    test "$(find /run/unpack/repo -type f | wc -l)" = "58"

# ---------------------------------------------------------------------------
# Freeze: build the reference, generate the corpus, record the expectations.
#
# freeze.py stages its own fixed inputs (the baseline tree, both halves of the
# probe, both halves of the consumer, the surface enumerator, the compiler shim,
# the source contract) into /opt/assets, so the whole grading input set is one
# directory that can be sealed in one step.  It lives beside /opt/swerefactor
# rather than inside it: the harness there is shared by every task and arrives
# from the donor image, and one directory should not be half vendored code and
# half this task's answers.
#
# The reference does not survive this stage.  Nothing links against it: State B is
# a jar, and the only programs ever built against the reference are probe.c and
# consumer.c, which run inside freeze.py and never again.  So the workspace is
# /tmp and the last thing this layer does is delete it -- what ships is the
# expectation blob.
# ---------------------------------------------------------------------------
FROM baseline AS frozen

COPY --chown=0:0 . /tests/behavioural

# The four files this stage shares with stage 3, checked against the digest list
# that ships in environment/ and in both stages' data/.
#
# tests/behavioural/ and tests/verification/ are separate build contexts, so neither
# can COPY from the other and the copies are duplicated by hand.  `swerefactor
# validate` compares same-named files at a stage's top level and in its data/
# directory, but not files under a stage's lib/ -- and all four of these are under
# lib/.  So this is the check that closes that gap, and it runs in both images.
#
# It runs before the freeze, deliberately: the frozen answers come from this
# probe.c, so a probe that had drifted from stage 3's would bake the divergence
# into the expectation blob and every later check would agree with it.
RUN set -eux; \
    cd /tests/behavioural/lib; \
    grep -v '^#' /tests/behavioural/data/shared-lib.sha256 | sha256sum -c -

# py_compile before freeze.py runs.  A syntax error in a module that only the
# grading path imports would otherwise survive the image build and surface as a
# crashed run against a real submission.
RUN set -eux; \
    rm -f /tests/behavioural/data/original.tar.gz /tests/behavioural/Dockerfile \
          /tests/behavioural/.dockerignore; \
    find /tests/behavioural/lib -name '*.py' -print0 \
      | xargs -0 /usr/bin/python3 -m py_compile; \
    /usr/bin/python3 -I /tests/behavioural/lib/freeze.py \
      --baseline /run/unpack/repo \
      --assets /opt/assets \
      --tests /tests/behavioural/lib \
      --data /tests/behavioural/data \
      --workspace /tmp/freeze \
      --image lang02-zlib-c-to-java-behavioural; \
    rm -rf /tmp/freeze /run/unpack /run/baseline

# The frozen assets are the answers; nothing that runs later has any business
# writing to them.  Grading runs as root because it has to install a submission
# into a prefix, so read-only bits are a guard rail rather than a boundary -- but
# they do mean an accidental write fails loudly.
#
# The case floors and the release surface are re-asserted against the manifest
# that actually shipped.  catalog.py enforces the floors when it builds, but this
# is the copy being graded with, and a promise of "at least N cases" is worth
# checking on the artifact.
#
# `test ! -e /opt/assets/reference` is the one that matters most: the reference C
# build is the oracle, and if it survived into the image a submission could be
# graded against a library it can read rather than against frozen bytes.
RUN set -eux; \
    /usr/bin/python3 -I /tests/behavioural/lib/manifest_check.py \
      --manifest /opt/assets/verifier-manifest.json; \
    chmod -R a-w /opt/assets; \
    test "$(stat -c '%a' /opt/assets/catalog.json)" = "444"; \
    test -s /opt/assets/oracle/expectations.bin; \
    test -f /opt/assets/baseline/zconf.h; \
    test -f /opt/assets/probe/probe.c; \
    test -f /opt/assets/probe/Probe.java; \
    test -f /opt/assets/consumer.c; \
    test -f /opt/assets/Consumer.java; \
    test -f /opt/assets/surface/Surface.java; \
    test -f /opt/assets/shim/ccshim.py; \
    test -f /opt/assets/source-contract.json; \
    test ! -e /opt/assets/reference

# Two checks that are about *this* task rather than about the freeze.
#
# The first is the one that would otherwise be discovered by a submission.  The
# contract pins class-file major version 61 and the graded build is expected to
# reach it with `--release 17`; a JDK whose default target drifted would fail
# every correct submission on a mandatory gate.  So a class file is compiled here
# and read back with the suite's own parser -- which exercises classfile.py at
# image-build time as a side effect, on a class file the suite did not write.
#
# The second is the compiler shim's two branches, both of which a provenance gate
# reads as evidence.  A shim that had stopped refusing would let a C build pass
# `compiler-shim-clean` silently, and a shim that had stopped forwarding would
# break CMake's compiler-ABI probe and fail every submission whose CMakeLists
# still says `project(zlib C)`.  Neither failure announces itself, so both
# directions are exercised against a real invocation.
RUN set -eux; \
    install -d /tmp/jdkcheck; \
    printf '%s\n' \
      'package p;' \
      'public class Pin { public static void main(String[] a) {} }' \
      > /tmp/jdkcheck/Pin.java; \
    printf '%s\n' \
      'import sys' \
      'from pathlib import Path' \
      'sys.path.insert(0, "/tests/behavioural/lib")' \
      'import classfile' \
      'c = classfile.load_class(Path(sys.argv[1]))' \
      'print("javac --release 17 ->", c.major_version, c.minor_version,' \
      '      c.binary_name, c.source_file)' \
      'if (c.major_version, c.minor_version) != (61, 0):' \
      '    raise SystemExit("this JDK does not target class version 61.0")' \
      'if c.source_file != "Pin.java":' \
      '    raise SystemExit("this JDK emitted no SourceFile attribute")' \
      > /tmp/jdkcheck/major.py; \
    /usr/lib/jvm/java-17-openjdk-amd64/bin/javac --release 17 \
      -d /tmp/jdkcheck/classes /tmp/jdkcheck/Pin.java; \
    /usr/bin/python3 -I /tmp/jdkcheck/major.py /tmp/jdkcheck/classes/p/Pin.class; \
    install -d /tmp/shimcheck/bin /tmp/shimcheck/repo \
              /tmp/shimcheck/build/CMakeFiles/CMakeTmp; \
    cp /opt/assets/shim/ccshim.py /tmp/shimcheck/bin/ccshim.py; \
    chmod 0755 /tmp/shimcheck/bin/ccshim.py; \
    ln -s ccshim.py /tmp/shimcheck/bin/cc; \
    printf '%s\n' '#include <zlib.h>' \
      'int main(void){return (int)adler32(0,0,0);}' \
      > /tmp/shimcheck/repo/deflate.c; \
    printf '%s\n' 'int main(void){return 0;}' \
      > /tmp/shimcheck/build/CMakeFiles/CMakeTmp/testCCompiler.c; \
    export CCSHIM_PATH=/usr/local/bin:/usr/bin:/bin \
           CCSHIM_REPO=/tmp/shimcheck/repo \
           CCSHIM_LOG=/tmp/shimcheck/ledger.jsonl; \
    rc=0; \
    /tmp/shimcheck/bin/cc -c /tmp/shimcheck/repo/deflate.c \
      -o /tmp/shimcheck/deflate.o >/dev/null 2>&1 || rc=$?; \
    test "$rc" = "97"; \
    test ! -e /tmp/shimcheck/deflate.o; \
    /tmp/shimcheck/bin/cc /tmp/shimcheck/build/CMakeFiles/CMakeTmp/testCCompiler.c \
      -o /tmp/shimcheck/testCCompiler; \
    test -x /tmp/shimcheck/testCCompiler; \
    grep -F '"event": "reject"' /tmp/shimcheck/ledger.jsonl > /dev/null; \
    grep -F '"verdict": "probe"' /tmp/shimcheck/ledger.jsonl > /dev/null; \
    rm -rf /tmp/jdkcheck /tmp/shimcheck

# --- The harness -------------------------------------------------------------
# The module runner, the result contract and the scorer, shared by every task.
COPY --from=infra /opt/swerefactor /opt/swerefactor

# --- The manifest, checked at build time -------------------------------------
# suite.toml is the file most likely to be edited and least likely to be tested,
# and the family map in driver.py is the thing that decides whether a case is
# graded at all.  Both are checked here: a duplicate id, a weight that does not
# parse, a module directory that does not exist, or a case family no module
# claims fails the build rather than the run.
#
# driver.py --self-check additionally proves every measured gate has a handler on
# IntegrityAuditor and every struct case has one on StructureEvaluator.  A
# renamed check would otherwise reach the report as a per-case "no handler
# implemented" -- a fail charged to the submission for a gap on this side.
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

# Bytecode last, and for a reason: py_compile ran during the freeze stage and
# every python3 invocation since could have written more.  An assertion placed
# before the last step that can violate it passes vacuously, so the removal and
# the check are the final layer.  Stale .pyc beside an edited .py is a verifier
# that grades with code nobody can read.
RUN set -eux; \
    find /tests/behavioural /opt/swerefactor -name __pycache__ -type d -prune \
      -exec rm -rf {} +; \
    found="$(find /tests/behavioural /opt/swerefactor \
               \( -name '__pycache__' -o -name '*.pyc' \) -print)"; \
    test -z "$found" || { echo "bytecode shipped: $found" >&2; exit 1; }

ENV JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64 \
    CMAKE_BUILD_PARALLEL_LEVEL=4 \
    TZ=UTC \
    LC_ALL=C.UTF-8 \
    LANG=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/opt/swerefactor \
    SWEREFACTOR_ASSETS=/opt/assets \
    SWEREFACTOR_REPO=/workspace/repo \
    SWEREFACTOR_LOG_DIR=/logs/verifier

RUN mkdir -p /opt/original /workspace /logs/verifier

WORKDIR /tests/behavioural
