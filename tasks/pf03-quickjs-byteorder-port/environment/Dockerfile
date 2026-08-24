# Agent environment for pf03-quickjs-byteorder-port -- State A.
#
# The image ships three toolchains and two emulators, because this port cannot
# be done without running the result.  Nothing here fails to compile: State A
# builds cleanly for s390x today and produces wrong answers.  A byte-order bug
# in an interpreter is a wrong value on a machine the agent has no hardware for,
# so the agent needs to cross-compile and then execute what it built.  That is
# the same equipment the task was authored against, and it is the minimum for
# the work to be engineering rather than guesswork.
#
# The grading images are separate and are not derived from this one.  Nothing
# here tells the agent what they check, and nothing here is a rehearsal of them.
# This file installs a toolchain, unpacks a repository, and asserts that the
# machinery it just installed works -- at image-build time, leaving nothing on
# disk for the agent to read.
#
# Base pinned by digest rather than tag: the task's premise is a measured split
# between three ABIs under one gcc, and a floating tag would silently change the
# compiler and the libm underneath every recorded expectation.  All grading
# images pin the same digest, so a divergence between two sides is never the
# toolchain's.  debian:bookworm-slim = Debian 12, gcc 12.2.0, binutils 2.40,
# qemu-user 7.2.
FROM debian:bookworm-slim@sha256:7b140f374b289a7c2befc338f42ebe6441b7ea838a042bbd5acbfca6ec875818

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=UTC

# Three compilers, two of them cross:
#
#   gcc                        x86-64  little-endian, LP64   -- the host
#   s390x-linux-gnu-gcc        s390x   BIG-endian,    LP64   -- the subject
#   arm-linux-gnueabihf-gcc    armhf   little-endian, ILP32  -- the control
#
# armhf earns its place: it changes word size without changing byte order, so a
# change that is really about pointer width cannot hide inside a change that
# claims to be about endianness.
#
# The -cross libc packages matter.  Without them each cross compiler falls back
# to /usr/include and compiles against the host's headers, which fails at the
# first #include <stdlib.h> rather than producing a subtly wrong binary.
#
# qemu-user-static executes the cross-built binaries.  Both target sysroots are
# on disk (/usr/s390x-linux-gnu, /usr/arm-linux-gnueabihf) because the dynamic
# loader needs them:  qemu-s390x-static -L /usr/s390x-linux-gnu ./qjs
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
      git \
      less \
      procps \
      vim-tiny \
      ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# gcc 12 across all three, asserted rather than hoped for: a different major
# version changes libm results and code generation, and every expectation this
# task was authored against was measured under 12.
RUN set -eu; \
    for cc in gcc s390x-linux-gnu-gcc arm-linux-gnueabihf-gcc; do \
      v="$("$cc" -dumpversion | cut -d. -f1)"; \
      [ "$v" = "12" ] || { echo "$cc major version $v, expected 12" >&2; exit 1; }; \
    done; \
    qemu-s390x-static --version >/dev/null; \
    qemu-arm-static --version >/dev/null

# No second JavaScript engine.  The task is to port *this* runtime; an engine
# already present would let a submission shell out to it or vendor it instead of
# fixing the one in the tree.
RUN set -eu; \
    for tool in node nodejs deno bun d8 js jsc duktape mujs; do \
      if command -v "$tool" >/dev/null 2>&1; then \
        echo "unexpected JS engine in agent image: $tool" >&2; exit 1; \
      fi; \
    done

# ---------------------------------------------------------------------------
# Repository snapshot.  Verified by digest, then unpacked to /workspace/repo.
#
# Members are all under a repo/ prefix, so --strip-components=1 puts quickjs.c
# at the tree root, where upstream's Makefile expects it.
# ---------------------------------------------------------------------------
ARG REPO_SHA256=8fce443dc18e01f25f64246f067bee346b7a5ed340c1372d653d4f7a4fa8d03e
COPY original.tar.gz /tmp/original.tar.gz
RUN set -eu; \
    actual="$(sha256sum /tmp/original.tar.gz | cut -d' ' -f1)"; \
    [ "$actual" = "$REPO_SHA256" ] || { \
      echo "original.tar.gz sha256 $actual != expected $REPO_SHA256" >&2; exit 1; }; \
    mkdir -p /workspace/repo; \
    tar -xzf /tmp/original.tar.gz -C /workspace/repo --strip-components=1; \
    rm -f /tmp/original.tar.gz; \
    test -f /workspace/repo/quickjs.c; \
    test -f /workspace/repo/cutils.h; \
    test -f /workspace/repo/Makefile; \
    test -f /workspace/repo/VERSION

# Properties of the archive that was just verified by digest, so none of these
# can fail for a submission's reasons -- they fail if the *task* was packaged
# wrong, at image-build time.
RUN set -eu; \
    cd /workspace/repo; \
    for vcs in .git .gitignore .gitmodules .gitattributes .github .hg .svn .bzr CVS; do \
      if [ -e "$vcs" ]; then echo "VCS leak: $vcs present" >&2; exit 1; fi; \
    done; \
    leaks="$(find . \( -name '.git*' -o -name '.hg*' -o -name '.svn' -o -name 'CVS' \) -print)"; \
    if [ -n "$leaks" ]; then echo "nested VCS leak: $leaks" >&2; exit 1; fi; \
    built="$(find . \( -name '*.o' -o -name '*.a' -o -name '*.so' \) -print)"; \
    if [ -n "$built" ]; then echo "prebuilt artifact in State A: $built" >&2; exit 1; fi; \
    for gen in repl.c qjscalc.c host-qjsc qjs qjsc; do \
      if [ -e "$gen" ]; then echo "generated artifact in State A: $gen" >&2; exit 1; fi; \
    done; \
    [ "$(cat VERSION)" = "2020-11-08" ] || { echo "unexpected VERSION" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Environment validation.
#
# A check on the *environment*, not on a submission, and not a rehearsal of any
# gate.  It asserts three premises the task depends on:
#
#   1. State A builds natively and its qjs runs.  If that were false the task
#      would be "fix the build", which it is not.
#   2. Each cross compiler emits binaries for the architecture it claims, with
#      the byte order it claims, and qemu-user can execute them.  A wrong
#      sysroot or a missing emulator would make the port unverifiable by the
#      agent, and the failure would look like the agent's fault.
#   3. /workspace/repo is untouched afterwards.
#
# It runs on a scratch copy and the scratch is removed in the same layer, so
# every trial starts from the same clean State A with no build products and
# nothing about this check on disk.  A failure fails the image build, which is
# the point of putting it here instead of in a script the agent could read.
# ---------------------------------------------------------------------------
RUN set -eux; \
    cp -a /workspace/repo /tmp/state-a; \
    cd /tmp/state-a; \
    make CONFIG_LTO= -j"$(nproc)" qjs qjsc >/tmp/native.log 2>&1 \
      || { tail -40 /tmp/native.log; echo "State A does not build natively" >&2; exit 1; }; \
    ./qjs -e 'if ((1 + 1) !== 2) throw Error("arithmetic"); print("state A native ok")'; \
    printf 'int main(void){return 42;}\n' > /tmp/probe.c; \
    s390x-linux-gnu-gcc -o /tmp/probe.s390x /tmp/probe.c; \
    arm-linux-gnueabihf-gcc -o /tmp/probe.armhf /tmp/probe.c; \
    file /tmp/probe.s390x | grep -q 'MSB'; \
    file /tmp/probe.s390x | grep -q 'S/390'; \
    file /tmp/probe.armhf  | grep -q 'LSB'; \
    file /tmp/probe.armhf  | grep -q 'ARM'; \
    qemu-s390x-static -L /usr/s390x-linux-gnu /tmp/probe.s390x || [ $? -eq 42 ]; \
    qemu-arm-static -L /usr/arm-linux-gnueabihf /tmp/probe.armhf || [ $? -eq 42 ]; \
    echo "cross toolchains and emulators ok"; \
    cd /; \
    rm -rf /tmp/state-a /tmp/probe.c /tmp/probe.s390x /tmp/probe.armhf /tmp/native.log

# ---------------------------------------------------------------------------
# Baseline git repository holding only State A, so the agent can diff its own
# work.  Created after the leak checks: exactly one commit, no remote.  It can
# answer "what have I changed" and nothing about how upstream later addressed
# this debt.
# ---------------------------------------------------------------------------
COPY gitignore.baseline /workspace/repo/.gitignore
RUN set -eu; \
    cd /workspace/repo; \
    git init -q -b main; \
    git config user.email "task@swerefactorbench.invalid"; \
    git config user.name  "SWERefactorBench"; \
    git config commit.gpgsign false; \
    git add -A; \
    git commit -q -m "State A: QuickJS 2020-11-08 (upstream, unmodified)"; \
    git tag state-a; \
    test "$(git rev-list --count HEAD)" = "1"; \
    test -z "$(git remote)"; \
    test -z "$(git status --porcelain)"; \
    chmod -R a+rX /workspace/repo

WORKDIR /workspace/repo
CMD ["/bin/bash"]
