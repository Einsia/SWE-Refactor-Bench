# =============================================================================
# SWERefactorBench -- lang06-jsonnet-cpp-to-csharp
# Stage 3 image: the verification probe.
#
#     docker build -t swerefactor/lang06-verification:1 \
#         tasks/lang06-jsonnet-cpp-to-csharp/tests/verification
#
# Six models, an hour each, trying to write a test the C++ passes and the C#
# fails.  So this is the only image that has to build BOTH trees: the original with
# g++ and make, the submission with `dotnet publish`.  It therefore carries both
# toolchains.
#
# That is not a loophole.  Stage 2 grades in an image with no C++ compiler and no
# jsonnet binary anywhere on the filesystem -- it asserts as much -- and has already
# measured whether the submission needs one; stage 1 has already judged whether the
# C++ left.  What this image adds is the ability to ask the C++ a question at
# grading time, which is the entire point of the stage: a candidate's claim that
# "the original does X" is worth nothing unless the original is right there to
# demonstrate it.
#
# The C++ toolchain is here for one purpose and reaches one tree.  Both trees are
# built once, before any candidate runs, and nothing here rebuilds them; a candidate
# gets two installed programs and no compiler it can usefully point at the tree
# under test.
#
# At runtime the harness provides:
#
#     /workspace/repo    the submission                            (read-only)
#     /opt/original      State A, unpacked from original.tar.gz    (read-only)
#     /logs/verifier     where the stage result JSON is written
#
# The stage copies both trees before building anything, so what stage 2 measured is
# not what stage 3 mutates.  Neither tree is baked into this image: State A arrives
# on the mount, which is why there is no original.tar.gz in this build context.
#
# Network: reaches the model API for the six rounds and the adjudicator, and
# nothing else.  NuGet resolves against the offline feed seeded below -- the same
# package set, from the same pinned file, as the agent's own environment image -- so
# a submission cannot acquire a dependency here that it could not acquire where it
# was written, and the C++ build needs nothing it does not carry.
# =============================================================================

ARG INFRA_IMAGE=swerefactor/infra:1
FROM ${INFRA_IMAGE} AS infra

# The same digest as environment/Dockerfile and tests/behavioural/Dockerfile.  A
# submission that builds where it was written builds here, against the same SDK.
FROM mcr.microsoft.com/dotnet/sdk@sha256:3c0edbfe1549dd93fb789dc96299a40df865ad7bffefcaf38e8c05940686d641

ARG DEBIAN_FRONTEND=noninteractive
# The snapshot the base image documents in its own sources.list.d/debian.sources.
ARG DEBIAN_SNAPSHOT=20260713T000000Z

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# g++ and make to build State A, python3 for the harness and the candidates, and
# the reading tools the adversaries use on the two source trees.  Same versions and
# same Debian snapshot as stage 2's reference stage, so the C++ these rounds compare
# against is the C++ the expectations were frozen from.
#
# sources.list.d/ is *replaced*, not supplemented -- writing sources.list while
# debian.sources still names the live archive would resolve the pins against
# whatever is current and quietly void the pinning.
RUN set -eux; \
    dotnet --version | grep -Fx '8.0.423'; \
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
      build-essential=12.9 \
      g++=4:12.2.0-3 \
      make=4.3-4.1 \
      python3=3.11.2-1+b1 \
      python3-venv=3.11.2-1+b1 \
      diffutils \
      file \
      jq \
      less \
      ripgrep \
      tree; \
    rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*.deb /var/cache/apt/*.bin; \
    g++ --version | head -1; \
    make --version | head -1; \
    python3 --version | grep -Fx 'Python 3.11.2'

# ---------------------------------------------------------------------------
# Offline NuGet feed.
#
# Seeded from the same nuget-packages.txt the environment image uses, and checked
# byte-identical to it by `swerefactor validate`.  The implementation must build
# against the base class library alone, so the feed exists for the submission's
# *test* projects: publish.py restores the whole tree, and a solution whose test
# project references xunit should not fail restore here when it restored where it
# was written.
#
# NUGET_PACKAGES is exported inside this step rather than left to the ENV block at
# the bottom of the file -- an ENV declared later does not apply to an earlier RUN,
# and without it the SDK seeds $HOME/.nuget/packages and leaves /opt/nuget-offline
# empty.  The `test -d` lines are what would catch that.
# ---------------------------------------------------------------------------
COPY --chown=0:0 data/nuget-packages.txt /run/seed/nuget-packages.txt

RUN set -eux; \
    echo 'c839939faa82f9f12b36be011349101a03ae9d7cdf6a0681475804b1b05fe616  /run/seed/nuget-packages.txt' \
      | sha256sum -c -; \
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
    test ! -d /root/.nuget/packages/xunit; \
    cd /; rm -rf /tmp/seed /run/seed; \
    chmod -R a+rX /opt/nuget-offline; \
    echo "feed seeded: $(find /opt/nuget-offline -maxdepth 1 -mindepth 1 -type d | wc -l) packages"

# The same closed feed stage 2 publishes against, written the same way and for the
# same reason.  This stage rebuilds the submission in order to compare it against
# the original, so it has to build it the way grading did: a divergence found under
# a different dependency set would be evidence about this image rather than about
# the port.  <clear/> leaves packages already in the global packages folder
# resolvable and refuses everything else -- see the longer note in stage 2's
# Dockerfile, and the rule itself in environment/.
RUN set -eux; \
    printf '%s\n' \
      '<?xml version="1.0" encoding="utf-8"?>' \
      '<configuration>' \
      '  <packageSources>' \
      '    <clear />' \
      '  </packageSources>' \
      '</configuration>' \
      > /opt/nuget.config; \
    chmod 0444 /opt/nuget.config; \
    grep -q '<clear />' /opt/nuget.config

# --- The candidate's test stack -----------------------------------------------
# pytest and pytest-timeout, at one set of versions for both trees.  A candidate
# that ran under different pytest versions on the two trees would not be comparing
# the two implementations.  Nothing else: a candidate imports srbjsonnet and the
# standard library, and srbjsonnet reaches the product through two installed
# programs.
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

# --- The probe ---------------------------------------------------------------
COPY . /tests/verification

RUN set -eux; \
    chmod +x /tests/verification/run-candidate.sh; \
    bash -n /tests/verification/run-candidate.sh; \
    /opt/venv/bin/python -m py_compile /tests/verification/lib/srbjsonnet.py; \
    python3 -m py_compile /tests/verification/lib/publish.py; \
    # A scope-less probe is unwinnable, a mismatched adversary count creates points
    # nobody can earn, and a deny list missing stage 2's two exclusions lets a round
    # be won on the surface the benchmark decided not to grade.  Checked here as
    # well as by `swerefactor validate`, because this is the image that would
    # otherwise discover it six rounds in.
    PYTHONPATH=/opt/swerefactor python3 /tests/verification/check-probe.py; \
    find /tests/verification -name __pycache__ -type d -prune -exec rm -rf {} +; \
    find /tests/verification -name '*.pyc' -delete

# The publish path a candidate's submission side depends on, exercised against a
# synthetic tree shaped like an idiomatic port rather than like the C++: project
# files named nothing like the programs they produce, a library competing for one
# of the two names, and a stale project file under obj/.  A discovery function that
# keyed on filenames would pass a check built from a tree that looks like the
# reference and fail every real submission.
#
# It runs here rather than at grading time for the ordinary reason: a broken
# publish path discovered during a round is six rounds wasted and a score that
# is wrong rather than absent.
# PYTHONDONTWRITEBYTECODE is exported here and not taken from the ENV block below,
# which is set after this RUN.  The cleanup above is the last one, and this is a
# python run after it: today it writes no .pyc only because a script run as __main__
# does not get compiled to bytecode and publish.py imports no sibling module.  That
# is a property of the import graph, not a guarantee, and the first local import
# added here would start shipping bytecode with no other symptom.
RUN set -eux; \
    export PYTHONDONTWRITEBYTECODE=1; \
    export NUGET_PACKAGES=/opt/nuget-offline; \
    export DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1 \
           DOTNET_SKIP_FIRST_TIME_EXPERIENCE=1; \
    export HOME=/tmp/dotnet-home; mkdir -p "$HOME"; \
    python3 /tests/verification/lib/publish.py --self-check; \
    rm -rf /tmp/dotnet-home

ENV DOTNET_CLI_TELEMETRY_OPTOUT=1 \
    DOTNET_NOLOGO=1 \
    DOTNET_SKIP_FIRST_TIME_EXPERIENCE=1 \
    DOTNET_CLI_UI_LANGUAGE=en \
    NUGET_PACKAGES=/opt/nuget-offline \
    PYTHONPATH=/opt/swerefactor \
    PYTHONDONTWRITEBYTECODE=1 \
    SWEREFACTOR_REPO=/workspace/repo \
    SWEREFACTOR_ORIGINAL=/opt/original \
    SWEREFACTOR_LOG_DIR=/logs/verifier \
    TZ=UTC \
    LC_ALL=C.UTF-8 \
    LANG=C.UTF-8

# Last instruction that can fail, so it covers every RUN above it rather than only
# the ones that existed when it was written.
RUN set -eux; \
    mkdir -p /opt/original /workspace /logs/verifier /tmp/srb-verification; \
    found="$(find /tests -name '__pycache__' -o -name '*.pyc' | head -20)"; \
    if [ -n "$found" ]; then \
      echo "build-time bytecode shipped under /tests:" >&2; \
      echo "$found" >&2; \
      exit 1; \
    fi; \
    echo "no build-time bytecode under /tests"

WORKDIR /tests/verification
