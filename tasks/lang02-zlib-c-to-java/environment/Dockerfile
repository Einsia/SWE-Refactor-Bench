# SWERefactorBench - lang02-zlib-c-to-java - agent environment
#
# State A is the pinned zlib 1.3.1 C implementation.  The image is built from the
# same digest-pinned Debian base and the same Debian snapshot as the verifier, so
# a submission that builds here builds there for the same reasons, and the
# toolchain closure is reproducible years from now.
#
# The workspace the agent receives carries no repository-management data of any
# kind.  zlib's upstream history contains no Java port, but the general rule is
# the point: an agent that can read a project's history can read a migration out
# of a later commit instead of performing one, and a benchmark that leaks its
# answer through .git measures search rather than engineering.
#
# One thing this image deliberately does not do is enforce the central rule of
# the task.  The JDK ships a bit-compatible deflate in java.util.zip, and it
# cannot be removed -- it is part of java.base, which the module contract
# requires.  So "do not delegate to java.util.zip" is a graded gate, checked in
# the constant pool and by class-load observation at grading time, not a property
# of this environment.  Nothing here should be read as permission.
FROM debian@sha256:26f2a7cab45014541c65f9d140ccfa6aaefbb49686c6759bea9c6f7f5bb3d72f AS toolchain

ARG DEBIAN_FRONTEND=noninteractive
ARG DEBIAN_SNAPSHOT=20251020T000000Z

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# The closure is the verifier's, plus git, and minus nothing.  Four of these are
# worth a stated reason:
#
#   openjdk-17-jdk-headless - the migration target: javac, java, jar.
#   gcc, libc6-dev, binutils - State A is C, and an agent has to be able to build
#                              and run the thing it is replacing.  A real compiler
#                              here is not a cheat vector: grading happens in the
#                              verifier image, where a shim refuses to compile any
#                              C or C++ that belongs to the repository.
#   make                     - CMake is used without -G, so the generator is Unix
#                              Makefiles.  The base image has no make.
#   git                      - for the baseline commit below, and so the agent has
#                              a working local diff surface.
#
# ninja-build is pointedly absent, in both images.  If it were here and not in the
# verifier, an agent could write a CMakeLists that only configures under Ninja and
# discover that at submission time; keeping the two generators identical means the
# build the agent debugs is the build that gets graded.
RUN set -eux; \
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
      git=1:2.39.5-0+deb12u2 \
      git-man=1:2.39.5-0+deb12u2 \
      libarchive13=3.6.2-1+deb12u3 \
      libc6-dev=2.36-9+deb12u13 \
      libcurl3-gnutls=7.88.1-10+deb12u14 \
      libjsoncpp25=1.9.5-4 \
      librhash0=1.4.3-3 \
      libuv1=1.44.2-1+deb12u1 \
      make=4.3-4.1 \
      openjdk-17-jdk-headless=17.0.16+8-1~deb12u1 \
      pkg-config=1.8.1-1 \
      python3=3.11.2-1+b1; \
    rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*.deb /var/cache/apt/*.bin

# What was installed is now asserted, separately from having asked for it, and
# against the same strings the verifier asserts.  The JDK is the one tool where a
# drift would be invisible: a JDK 21 that accepts `--release 17` produces class
# files this contract calls correct, while compiling against a different
# java.base signature set and writing a different Created-By into the jar.  A
# submission developed against such a JDK would fail in the verifier for reasons
# that look like its own fault.
#
# JAVA_HOME is asserted as a path because the contract names this directory and
# the verifier resolves javac and jar through it.
RUN set -eux; \
    test -d /usr/lib/jvm/java-17-openjdk-amd64; \
    /usr/lib/jvm/java-17-openjdk-amd64/bin/javac --version | grep -Fx 'javac 17.0.16'; \
    /usr/lib/jvm/java-17-openjdk-amd64/bin/java  --version | head -1 \
      | grep -Fx 'openjdk 17.0.16 2025-07-15'; \
    test -x /usr/lib/jvm/java-17-openjdk-amd64/bin/jar; \
    javac --version | grep -Fx 'javac 17.0.16'; \
    cmake --version | head -1 | grep -Fx 'cmake version 3.25.1'; \
    make --version | head -1 | grep -F 'GNU Make 4.3'; \
    gcc -dumpfullversion | grep -Fx '12.2.0'; \
    git --version | grep -Fx 'git version 2.39.5'; \
    pkg-config --version; \
    /usr/bin/python3 --version | grep -F 'Python 3.11.2'; \
    gzip --version | head -1 | grep -F 'gzip 1.12'; \
    test -z "$(command -v ninja || true)"

RUN set -eux; \
    groupadd --gid 2000 agent; \
    useradd --uid 2000 --gid 2000 --create-home --shell /bin/bash agent; \
    install -d -m 0755 -o agent -g agent /workspace

# ---------------------------------------------------------------------------
# State A materialization.  The archive is authenticated before it is opened and
# the extracted tree is audited afterwards, so neither a substituted archive nor
# a stray VCS directory can reach the workspace.
# ---------------------------------------------------------------------------
FROM toolchain AS state-a

COPY --chown=0:0 original.tar.gz /run/state-a/original.tar.gz
COPY --chown=0:0 original.sha256 /run/state-a/original.sha256
COPY --chown=0:0 source-contract.json /run/state-a/source-contract.json
COPY --chown=0:0 verify_environment.py /run/state-a/verify_environment.py

# The digest is checked twice against two independently written copies of it:
# once against original.sha256, which travels beside the archive and is the file
# the behavioural stage also verifies, and once against the literal below.  A
# single copy is a digest that can be updated in step with a substituted
# archive; two copies in two files mean an unnoticed substitution has to be
# performed twice.
RUN set -eux; \
    cd /run/state-a; \
    sha256sum -c original.sha256; \
    echo '18809c7fa08d686c3317f4e9858a1c6c0d3ad9f0fe3d7109f985365361dafbc6  /run/state-a/original.tar.gz' \
      | sha256sum -c -; \
    echo '4ecbc79f7d1e579beacf2c3252953a3085aa105ccb8e101f82bd78a39387003b  /run/state-a/source-contract.json' \
      | sha256sum -c -; \
    test "$(stat -c '%s' /run/state-a/original.tar.gz)" = "1177532"; \
    test "$(stat -c '%s' /run/state-a/source-contract.json)" = "64258"; \
    install -d -m 0755 /workspace; \
    tar --extract --file=/run/state-a/original.tar.gz --directory=/workspace \
        --no-same-owner --no-same-permissions; \
    test -f /workspace/repo/CMakeLists.txt; \
    test "$(find /workspace/repo -type f | wc -l)" = "58"; \
    test -z "$(find /workspace/repo \
      \( -type l -o \( ! -type d ! -type f \) \) -print -quit)"; \
    test -z "$(find /workspace/repo \
      \( -name .git -o -name .github -o -name .gitlab -o -name .gitignore \
         -o -name .gitattributes -o -name .gitmodules -o -name .hg \
         -o -name .svn -o -name .bzr -o -name _darcs -o -name CVS \
         -o -name .agit -o -name '*.pyc' \) -print -quit)"; \
    install -d -m 0755 /opt/swerefactor; \
    install -m 0444 /run/state-a/source-contract.json \
      /opt/swerefactor/source-contract.json

# Prove the delivered environment can build and exercise State A, and can produce
# the artifact State B is required to be, before any solver touches it.  Then
# discard every build artifact, so the workspace the agent receives is exactly the
# source snapshot.  Nothing below reaches the network.
#
# The C build is done in a copy rather than in /workspace/repo because upstream's
# CMakeLists.txt renames a source-tree zconf.h to zconf.h.included during an
# out-of-source configure.  Configuring against the delivered tree would mutate
# it, and the agent would receive a workspace that is not the snapshot.
RUN set -eux; \
    cp -a /workspace/repo /tmp/state-a-copy; \
    cmake -S /tmp/state-a-copy -B /tmp/baseline \
      -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=ON \
      -DZLIB_BUILD_EXAMPLES=ON \
      -DCMAKE_INSTALL_PREFIX=/tmp/baseline-prefix > /tmp/baseline-configure.log; \
    cmake --build /tmp/baseline --parallel "$(nproc)" > /tmp/baseline-build.log; \
    cmake --install /tmp/baseline > /tmp/baseline-install.log; \
    test -f /tmp/baseline-prefix/lib/libz.so.1.3.1; \
    test -f /tmp/baseline-prefix/lib/libz.a; \
    readelf -d /tmp/baseline-prefix/lib/libz.so.1.3.1 \
      | grep -F 'Library soname: [libz.so.1]'; \
    test "$(readelf -V --wide /tmp/baseline-prefix/lib/libz.so.1.3.1 \
      | grep -coE 'Name: ZLIB_[0-9.]+')" = "14"; \
    LD_LIBRARY_PATH=/tmp/baseline/. /tmp/baseline/example > /tmp/example.log; \
    grep -F 'zlib version 1.3.1' /tmp/example.log; \
    printf 'round trip check\n' > /tmp/rt.txt; \
    LD_LIBRARY_PATH=/tmp/baseline/. /tmp/baseline/minigzip < /tmp/rt.txt > /tmp/rt.gz; \
    LD_LIBRARY_PATH=/tmp/baseline/. /tmp/baseline/minigzip -d < /tmp/rt.gz \
      | grep -Fx 'round trip check'; \
    gzip -t /tmp/rt.gz

# The Java half of the same proof: this image's JDK can compile a module and run
# it from a jar on the module path.  That is the capability the migration needs
# and the thing whose absence would be this image's fault rather than a solver's.
#
# It deliberately stops there.  An earlier version of this block walked the
# grading gates instead -- class-file 61.0, the SourceFile attribute, the
# versioned filename with its symlink, MANIFEST.MF's position among the archive
# entries, --describe-module, both launch modes -- on the reasoning that a
# requirement no toolchain here can satisfy is unfair.  The reasoning is sound and
# the placement was wrong.  The environment image is what the solver works inside
# and can read; a block that rehearses each gate in order is a checklist of what
# the grader measures, sitting in the one image the grader does not control.  The
# fairness question it was answering belongs to whoever authors the task, and it
# is answered where the requirements are stated: every compile-time requirement in
# instruction.md is machine-checked against source-contract.json, and no
# requirement is enforced that the instruction does not state.
#
# So the gates are proved reachable outside this image, and what stays here is the
# toolchain check: javac exists, targets 17, and the result runs.
RUN set -eux; \
    install -d /tmp/jdkprobe/src/org.example/org/example; \
    printf '%s\n' 'module org.example { exports org.example; }' \
      > /tmp/jdkprobe/src/org.example/module-info.java; \
    printf '%s\n' 'package org.example;' \
      'public final class Probe {' \
      '  public static void main(String[] a) { System.out.println("probe ok"); }' \
      '}' > /tmp/jdkprobe/src/org.example/org/example/Probe.java; \
    /usr/lib/jvm/java-17-openjdk-amd64/bin/javac --release 17 \
      -d /tmp/jdkprobe/classes \
      /tmp/jdkprobe/src/org.example/module-info.java \
      /tmp/jdkprobe/src/org.example/org/example/Probe.java; \
    /usr/lib/jvm/java-17-openjdk-amd64/bin/jar --create \
      --file /tmp/jdkprobe/probe.jar -C /tmp/jdkprobe/classes .; \
    java -p /tmp/jdkprobe/probe.jar -m org.example/org.example.Probe \
      | grep -Fx 'probe ok'

# The self-check runs last, against the workspace as delivered, and then every
# trace of the proofs above is removed and the workspace re-asserted.
RUN set -eux; \
    /usr/bin/python3 -I /run/state-a/verify_environment.py \
      --repo /workspace/repo \
      --contract /opt/swerefactor/source-contract.json \
      --expect-files 58; \
    rm -rf /tmp/baseline /tmp/baseline-prefix /tmp/state-a-copy /tmp/jdkprobe \
      /tmp/rt.txt /tmp/rt.gz /tmp/example.log /tmp/baseline-configure.log \
      /tmp/baseline-build.log /tmp/baseline-install.log \
      /run/state-a; \
    test ! -e /run/state-a; \
    test "$(find /workspace/repo -type f | wc -l)" = "58"; \
    test -f /workspace/repo/zconf.h; \
    test ! -e /workspace/repo/zconf.h.included

# A single history-free baseline commit gives the solver a local diff surface.
# The verifier never trusts this repository: it holds its own copy of the baseline
# manifest and treats .git as untrusted agent-writable data.
RUN set -eux; \
    cd /workspace/repo; \
    git -c init.defaultBranch=main init -q .; \
    git config user.name 'SWERefactorBench'; \
    git config user.email 'bench@swerefactor.invalid'; \
    git config commit.gpgsign false; \
    git config gc.auto 0; \
    git add -A; \
    GIT_AUTHOR_DATE='2000-01-01T00:00:00 +0000' \
    GIT_COMMITTER_DATE='2000-01-01T00:00:00 +0000' \
    GIT_AUTHOR_NAME='SWERefactorBench' \
    GIT_COMMITTER_NAME='SWERefactorBench' \
    GIT_AUTHOR_EMAIL='bench@swerefactor.invalid' \
    GIT_COMMITTER_EMAIL='bench@swerefactor.invalid' \
      git commit -q -m 'State A: zlib 1.3.1 (C implementation)'; \
    test "$(git rev-list --count HEAD)" = "1"; \
    test -z "$(git status --porcelain)"; \
    git log --format=%H -1 > /opt/swerefactor/baseline-commit.txt; \
    chmod 0444 /opt/swerefactor/baseline-commit.txt; \
    chown -R agent:agent /workspace

ENV JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64 \
    CMAKE_BUILD_PARALLEL_LEVEL=4 \
    TZ=UTC \
    LC_ALL=C.UTF-8 \
    LANG=C.UTF-8

WORKDIR /workspace/repo
USER agent
