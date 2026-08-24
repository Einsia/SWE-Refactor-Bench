# SWERefactorBench - lang06-jsonnet-cpp-to-csharp - agent environment
#
# State A is the pinned jsonnet 0.20.0 C++ implementation.  The image is built
# from a digest-pinned .NET 8 SDK base whose apt sources are re-pointed at the
# exact Debian snapshot that base image documents, so the toolchain closure is
# reproducible years from now.
#
# The workspace the agent receives carries no *upstream* repository-management
# data: State A is extracted from a tarball with no .git, and also no .gitignore,
# .gitattributes, .gitmodules or .github/.  Upstream history is an answer key --
# the commit that ports a file is the port -- and a .gitignore is a small map of
# what the build being replaced produces.
#
# What the workspace does carry is a single history-free commit made at the end of
# this file, so the agent has a local diff surface for its own work.  It contains
# State A and nothing else, and grading never trusts it: the verifier holds its own
# copy of State A and treats .git as agent-writable data.
#
# The C++ toolchain is installed on purpose.  A migration that cannot build and
# run the thing it is replacing is being done blind: the agent needs the
# reference to diff against while porting, and byte-exact output compatibility
# is not something anyone derives from reading alone.
FROM mcr.microsoft.com/dotnet/sdk@sha256:3c0edbfe1549dd93fb789dc96299a40df865ad7bffefcaf38e8c05940686d641 AS toolchain

ARG DEBIAN_FRONTEND=noninteractive
# The snapshot the base image documents in its own sources.list.d/debian.sources.
ARG DEBIAN_SNAPSHOT=20260713T000000Z

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Pin the package closure to that snapshot, then install exactly the versions
# this task was calibrated against.  Version-pinned so a rebuild either produces
# the same closure or fails loudly instead of drifting.
RUN set -eux; \
    dotnet --version | grep -Fx '8.0.423'; \
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
      build-essential=12.9 \
      g++=4:12.2.0-3 \
      make=4.3-4.1 \
      python3=3.11.2-1+b1; \
    rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*.deb /var/cache/apt/*.bin; \
    g++ --version | head -1; \
    make --version | head -1; \
    python3 --version

# ---------------------------------------------------------------------------
# Offline NuGet feed.
#
# The implementation must build against the base class library alone -- that is
# in the contract and the verifier publishes with no feed of its own beyond this
# one.  What the feed exists for is the submission's *tests*: the structural
# score rewards a port that ships tests, and with an empty cache and no network
# `dotnet add package xunit` fails, which would push every submission towards a
# hand-rolled harness for the wrong reason.
#
# Seeded here, at build time, while there is still a network.  Grading runs with
# the same NUGET_PACKAGES and no network at all.
# ---------------------------------------------------------------------------
COPY --chown=0:0 nuget-packages.txt /run/seed/nuget-packages.txt

# NUGET_PACKAGES is exported *inside* this step, not just in the ENV block at
# the bottom of the file: an ENV declared later does not apply to an earlier RUN,
# and without it the SDK seeds $HOME/.nuget/packages instead.  That would leave
# /opt/nuget-offline empty while the agent -- who runs as uid 2000 with
# NUGET_PACKAGES=/opt/nuget-offline -- got a cache miss on every package and no
# network to recover with.  The two `test -d` lines below are what caught it.
RUN set -eux; \
    export NUGET_PACKAGES=/opt/nuget-offline; \
    export DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1; \
    install -d -m 0755 /opt/nuget-offline /tmp/seed; \
    cd /tmp/seed; \
    dotnet new classlib -o . --no-restore > /dev/null; \
    while read -r pkg ver; do \
      case "$pkg" in ''|\#*) continue ;; esac; \
      echo "seeding $pkg $ver"; \
      dotnet add package "$pkg" --version "$ver" > /dev/null; \
    done < /run/seed/nuget-packages.txt; \
    dotnet restore > /dev/null; \
    test -d /opt/nuget-offline/xunit; \
    test -d /opt/nuget-offline/nunit; \
    test -d /opt/nuget-offline/fluentassertions; \
    test ! -d /root/.nuget/packages/xunit; \
    cd /; rm -rf /tmp/seed /run/seed; \
    chmod -R a+rX /opt/nuget-offline; \
    echo "feed seeded: $(find /opt/nuget-offline -maxdepth 1 -mindepth 1 -type d | wc -l) packages"

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
COPY --chown=0:0 source-contract.json /run/state-a/source-contract.json
COPY --chown=0:0 verify_environment.py /run/state-a/verify_environment.py

# Both digests are pinned in more than one build context, because a context cannot
# reach outside itself: original.tar.gz is asserted here and again by stages 2 and 3,
# and source-contract.json is asserted here and again by stages 1 and 2.  Each
# assertion covers only its own copy, so what keeps the copies one file is a host
# validator that compares all of them and confirms every asserted digest is the real
# sha256 of the file beside it.
#
# This comment used to say the copies were held honest by "`swerefactor validate`'s
# byte-comparison", which validate does not do, while two of the three copies of
# source-contract.json carried no digest assertion at all.  A stated mechanism that
# does not exist is worse than a missing check: it stops anyone looking for one.
RUN set -eux; \
    echo '07acc7b9ac3785a710cae1f7defe0068cca11450c2b18a5ffb1eac9729529e9e  /run/state-a/original.tar.gz' \
      | sha256sum -c -; \
    echo '2c6a07be63642e6408dc814bf895798183c8b654f6b6c937c3e3f93b5c5f6f19  /run/state-a/source-contract.json' \
      | sha256sum -c -; \
    test "$(stat -c '%s' /run/state-a/original.tar.gz)" = "22277485"; \
    install -d -m 0755 /workspace/repo; \
    tar --extract --file=/run/state-a/original.tar.gz --directory=/workspace/repo \
        --strip-components=1 --no-same-owner --no-same-permissions; \
    test -f /workspace/repo/Makefile; \
    test -f /workspace/repo/core/vm.cpp; \
    test -f /workspace/repo/stdlib/std.jsonnet; \
    test "$(find /workspace/repo -type f | wc -l)" = "4296"; \
    test -z "$(find /workspace/repo \
      \( -type l -o \( ! -type d ! -type f \) \) -print -quit)"; \
    test -z "$(find /workspace/repo \
      \( -name '.git*' -o -name .hg -o -name .svn -o -name .bzr \
         -o -name _darcs -o -name CVS -o -name '*.pyc' \) -print -quit)"; \
    install -d -m 0755 /opt/swerefactor; \
    install -m 0444 /run/state-a/source-contract.json \
      /opt/swerefactor/source-contract.json

# Audit the tree the agent will actually receive: pristine, unbuilt, exactly as
# extracted.  This runs before the build on purpose -- a build leaves object files
# and two binaries behind, and an audit of the delivered environment that ran
# afterwards would be auditing something no agent ever sees.
RUN set -eux; \
    /usr/bin/python3 -I -B /run/state-a/verify_environment.py \
      --repo /workspace/repo \
      --contract /opt/swerefactor/source-contract.json \
      --expect-files 4296

# Prove the delivered environment can build and exercise State A before any
# solver touches it, then discard every build artifact so the workspace the agent
# receives is exactly the source snapshot again.  Nothing here reaches the
# network.  The closing file count is what proves the cleanup was complete.
RUN set -eux; \
    cd /workspace/repo; \
    make -j"$(nproc)" jsonnet jsonnetfmt > /tmp/baseline-build.log 2>&1 \
      || { tail -40 /tmp/baseline-build.log; exit 1; }; \
    ./jsonnet --version | grep -F 'v0.20.0'; \
    ./jsonnetfmt --version | grep -F 'v0.20.0'; \
    echo '{a: 1+1}' > /tmp/probe.jsonnet; \
    ./jsonnet /tmp/probe.jsonnet | grep -F '"a": 2'; \
    ./jsonnetfmt /tmp/probe.jsonnet | grep -F 'a: 1 + 1'; \
    make clean > /dev/null 2>&1 || true; \
    rm -f jsonnet jsonnetfmt libjsonnet.so libjsonnet++.so; \
    find . -name '*.o' -delete; \
    rm -rf /tmp/baseline-build.log /tmp/probe.jsonnet /run/state-a; \
    test ! -e /run/state-a; \
    test ! -e /workspace/repo/jsonnet; \
    test ! -e /workspace/repo/jsonnetfmt; \
    test -z "$(find /workspace/repo -name '*.o' -print -quit)"; \
    test "$(find /workspace/repo -type f | wc -l)" = "4296"

# Prove the .NET side works offline before handing the image over: an agent that
# discovers a broken SDK at hour three has lost the time to a defect of mine.
RUN set -eux; \
    install -d -m 0755 /tmp/dotnet-probe; \
    cd /tmp/dotnet-probe; \
    dotnet new console -o . --no-restore > /dev/null; \
    dotnet build -c Release --nologo > /tmp/dotnet-probe.log 2>&1 \
      || { tail -30 /tmp/dotnet-probe.log; exit 1; }; \
    dotnet publish -c Release -o /tmp/dotnet-probe/pub --nologo \
      -p:PublishSingleFile=false -p:SelfContained=false > /dev/null; \
    test -f /tmp/dotnet-probe/pub/dotnet-probe.dll; \
    cd /; rm -rf /tmp/dotnet-probe /tmp/dotnet-probe.log

# A single history-free baseline commit gives the solver a local diff surface.
# The verifier never trusts this repository: it holds its own copy of State A and
# treats .git as untrusted agent-writable data.
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
      git commit -q -m 'State A: jsonnet 0.20.0 (C++ implementation)'; \
    test "$(git rev-list --count HEAD)" = "1"; \
    test -z "$(git status --porcelain)"; \
    git log --format=%H -1 > /opt/swerefactor/baseline-commit.txt; \
    chmod 0444 /opt/swerefactor/baseline-commit.txt; \
    chown -R agent:agent /workspace

# The closed feed, in the place `dotnet` reads by default, so the agent develops
# under the rule it is graded under.
#
# "The implementation must compile against the .NET base class library alone.  No
# external packages" is in instruction.md, but NUGET_PACKAGES names a *cache*, not a
# source: without this file the SDK still has nuget.org configured, and the rule
# would be enforced only by the absence of a network.  An agent that added a
# PackageReference would then see a DNS failure -- a symptom that reads like a
# broken container -- rather than `NU1100: Unable to resolve`, which reads like the
# rule it is.  Worse, it would not find out at all if a network ever appeared, and
# would lose the whole behavioural stage at grading time for something no message
# had told it.
#
# <clear/> with nothing added back does not empty the feed: a package already in
# the global packages folder resolves from there without consulting any source, so
# the pre-seeded test frameworks the instruction invites still restore and still
# run.  The same file is at /opt/nuget.config, which is where the verifier's own
# restores point with --configfile; this copy is the agent's, and editing it
# changes nothing about grading.
RUN set -eux; \
    install -d -o agent -g agent -m 0755 /home/agent/.nuget/NuGet; \
    printf '%s\n' \
      '<?xml version="1.0" encoding="utf-8"?>' \
      '<configuration>' \
      '  <packageSources>' \
      '    <clear />' \
      '  </packageSources>' \
      '</configuration>' \
      | tee /opt/nuget.config > /home/agent/.nuget/NuGet/NuGet.Config; \
    chown agent:agent /home/agent/.nuget/NuGet/NuGet.Config; \
    chmod 0644 /home/agent/.nuget/NuGet/NuGet.Config; \
    chmod 0444 /opt/nuget.config; \
    grep -q '<clear />' /home/agent/.nuget/NuGet/NuGet.Config; \
    grep -q '<clear />' /opt/nuget.config

ENV DOTNET_CLI_TELEMETRY_OPTOUT=1 \
    DOTNET_NOLOGO=1 \
    DOTNET_SKIP_FIRST_TIME_EXPERIENCE=1 \
    NUGET_PACKAGES=/opt/nuget-offline \
    DOTNET_CLI_HOME=/home/agent \
    TZ=UTC \
    LC_ALL=C.UTF-8 \
    LANG=C.UTF-8

WORKDIR /workspace/repo
USER agent
