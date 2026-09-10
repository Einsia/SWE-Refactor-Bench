# =============================================================================
# SWERefactorBench -- lang06-jsonnet-cpp-to-csharp
# Stage 2 image: the behavioural suite.
#
#     docker build -t swerefactor/lang06-behavioural:1 \
#         tasks/lang06-jsonnet-cpp-to-csharp/tests/behavioural
#
# What this stage measures: the original is a pair of command-line programs, a
# port of it is a pair of command-line programs, and the port is complete exactly
# insofar as the two pairs are indistinguishable from outside.  So the suite
# publishes the submission, then compares exit status, stdout, stderr and the
# files each invocation created, changed or removed against expectations recorded
# from the reference.  It reads no C# and asserts nothing about how any of it was
# written -- that is stage 1's job, and it does it by reading rather than running.
#
# Four properties of this file are load-bearing.
#
#  1. The reference is built HERE, at image-build time, and the answers are frozen
#     before any submission exists.  freeze.py runs the C++ jsonnet and jsonnetfmt
#     over every case and records what came back.  Deciding expectations at
#     grading time would mean the submission was in the container while the
#     answers were being settled.
#
#  2. The reference does not survive into the final image.  The C++ is built in a
#     stage that has g++; the image that grades starts again from the SDK base,
#     inherits /opt/assets and nothing else, and asserts that no compiler and no
#     jsonnet binary are present.  A submission's code executes in this container
#     -- it is the one stage where that is true -- so an oracle reachable by
#     exec() would be an oracle, however well hidden the path to it was.
#
#  3. The suite carries its own copy of the graded .jsonnet inputs.  The two
#     upstream families run files that live in the repository the submission
#     edited; the executor materializes them from /opt/assets/upstream, never from
#     the submitted tree, and every one is hash-checked against the freeze at
#     startup.  Otherwise editing an input until the diff went away would score.
#
#  4. Nothing trusts a build directory the agent left behind.  The `build` module
#     copies the submission, deletes bin/ obj/ publish/, and publishes both CLIs
#     itself with single-file, AOT, trimming, ReadyToRun and self-contained forced
#     off -- because the three CLR metadata checks read the ImplMap, ModuleRef and
#     MemberRef tables of what was published, and a single-file bundle has no
#     tables to read.
#
# At runtime the harness provides:
#
#     /workspace/repo    the submission                       (read-write copy)
#     /opt/original      State A                                  (read-only)
#     /logs/verifier     where the stage result JSON is written
#
# Network: none.  NuGet resolves against the offline feed seeded below, which is
# the same package set the agent's environment image seeded from the same file.
# The verifier must not be stricter about restore than the container the work was
# done in.
# =============================================================================

ARG INFRA_IMAGE=swerefactor/infra:1
FROM ${INFRA_IMAGE} AS infra

# The same digest as environment/Dockerfile.  A submission that builds where it
# was written builds here, for the same reasons and against the same SDK.
FROM mcr.microsoft.com/dotnet/sdk@sha256:3c0edbfe1549dd93fb789dc96299a40df865ad7bffefcaf38e8c05940686d641 AS toolchain

ARG DEBIAN_FRONTEND=noninteractive
# The snapshot the base image documents in its own sources.list.d/debian.sources.
ARG DEBIAN_SNAPSHOT=20260713T000000Z

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# python3 is the suite's runtime: every module is `python3 -I -B lib/driver.py`.
# Pinned to the same version the environment image installs, and re-pointed at the
# same Debian snapshot, so this closure is reproducible for as long as that
# snapshot is.  sources.list.d/ is *replaced*, not supplemented -- writing
# sources.list while debian.sources still names the live archive would resolve the
# pins against whatever is current and quietly void the pinning.
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
      python3=3.11.2-1+b1; \
    rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*.deb /var/cache/apt/*.bin; \
    python3 --version | grep -Fx 'Python 3.11.2'

# ---------------------------------------------------------------------------
# Offline NuGet feed.
#
# Seeded from the same nuget-packages.txt the environment image uses, and checked
# byte-identical to it by `swerefactor validate`.  The implementation itself must
# build against the base class library alone, so the feed exists for the
# submission's *test* projects: `build` restores only the two CLI projects, but a
# CLI project that picked up a test-only reference should not fail restore here
# when it restored where it was written.
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
    test -d /opt/nuget-offline/fluentassertions; \
    test ! -d /root/.nuget/packages/xunit; \
    cd /; rm -rf /tmp/seed /run/seed; \
    chmod -R a+rX /opt/nuget-offline; \
    echo "feed seeded: $(find /opt/nuget-offline -maxdepth 1 -mindepth 1 -type d | wc -l) packages"

# What makes the feed a *closed set* rather than a cache in front of the network.
#
# NUGET_PACKAGES names the global packages folder, which is a cache, not a source:
# with it set and nothing else done, `dotnet restore` still has the SDK's default
# nuget.org source configured and will happily resolve a package the feed has never
# heard of.  Until this file existed the rule at instruction.md's "No external
# packages" was enforced by nothing but the absence of a network -- and the network
# is not uniformly absent across this ladder, since stages 1 and 3 talk to a model
# API.  A rule enforced by a timeout is not a rule.
#
# <clear/> with nothing added back does not empty the feed.  A package already in
# the global packages folder resolves from there without consulting any source, so
# the pre-seeded test frameworks still restore and still run; anything else fails as
# `NU1100: Unable to resolve`, which names the problem rather than describing a
# symptom.  lib/build.py passes this with --configfile, which replaces the whole
# nuget.config hierarchy, so a submission cannot add a source back.
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

# ---------------------------------------------------------------------------
# The reference, in a stage that has a C++ compiler.
#
# Its own authenticated copy of State A: the suite must not learn what the
# original was from the party being graded.  The digest and the byte count are the
# same two the environment image checks, so if the two copies of original.tar.gz
# ever diverge one of the two builds fails on its checksum rather than freezing
# expectations from the wrong tree.
# ---------------------------------------------------------------------------
FROM toolchain AS reference

RUN set -eux; \
    apt-get -o Acquire::Retries=8 update; \
    apt-get install -y --no-install-recommends \
      build-essential=12.9 \
      g++=4:12.2.0-3 \
      make=4.3-4.1; \
    rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*.deb /var/cache/apt/*.bin; \
    g++ --version | head -1; \
    make --version | head -1

COPY --chown=0:0 data/original.tar.gz /run/reference/original.tar.gz

RUN set -eux; \
    echo '07acc7b9ac3785a710cae1f7defe0068cca11450c2b18a5ffb1eac9729529e9e  /run/reference/original.tar.gz' \
      | sha256sum -c -; \
    test "$(stat -c '%s' /run/reference/original.tar.gz)" = "22277485"; \
    install -d -m 0755 /run/state-a; \
    tar --extract --file=/run/reference/original.tar.gz --directory=/run/state-a \
        --strip-components=1 --no-same-owner --no-same-permissions; \
    test -f /run/state-a/Makefile; \
    test -f /run/state-a/core/vm.cpp; \
    test -f /run/state-a/stdlib/std.jsonnet; \
    test "$(find /run/state-a -type f | wc -l)" = "4296"

# Build both programs and confirm they are the version the spec describes.  Every
# expectation in this image comes from these two binaries; a silently different
# reference would produce a self-consistent set of answers for the wrong program.
RUN set -eux; \
    cd /run/state-a; \
    make -j"$(nproc)" jsonnet jsonnetfmt > /tmp/reference-build.log 2>&1 \
      || { tail -40 /tmp/reference-build.log; exit 1; }; \
    ./jsonnet --version | grep -F 'v0.20.0'; \
    ./jsonnetfmt --version | grep -F 'v0.20.0'; \
    rm -f /tmp/reference-build.log

# ---------------------------------------------------------------------------
# The freeze.
#
# Everything the grading run reads is assembled into /opt/assets here, so it can be
# sealed in one step.  It sits beside /opt/swerefactor rather than inside it:
# /opt/swerefactor is the shared harness that arrives from the donor image, and one
# directory should not be half vendored code and half this task's answers.
#
#     expectations.json      what the reference did, per case
#     upstream/              the graded .jsonnet inputs, hash-checked at startup
#     tools/AsmInspect.dll   the CLR metadata reader
# ---------------------------------------------------------------------------
FROM reference AS frozen

COPY --chown=0:0 . /tests/behavioural

# The freeze audits before it records.  spec.check_or_die reads the reference's own
# C++ to check the tables that describe it, because a table can be internally
# consistent, length-asserted and still wrong about seven builtins -- only a check
# against the source finds that.  contract.audit compares the shipped
# source-contract.json against what the graders enforce, in both directions, so a
# requirement stated but unchecked (or checked but unstated) fails the build rather
# than surfacing to a submission at grading time.  py_compile first: a syntax error
# in any module should name the file, not arrive as a traceback from an import.
#
# The contract is authenticated before it is read, with the same digest
# environment/Dockerfile asserts over the copy the submitter is handed.  There are
# three copies of this file -- one per build context, because a context cannot
# reach outside itself -- and a submission is graded against requirements it was
# never shown if they drift.  Asserting the digest in each context that carries a
# copy is what makes them one file, and it has to be asserted in all three: a pin
# repeated in three places needs a cross-check of its own, and that is the host's
# job, because nothing inside one context can see the other two.
RUN set -eux; \
    find /tests/behavioural/lib -name '*.py' -print0 \
      | xargs -0 python3 -m py_compile; \
    echo '2c6a07be63642e6408dc814bf895798183c8b654f6b6c937c3e3f93b5c5f6f19  /tests/behavioural/data/source-contract.json' \
      | sha256sum -c -; \
    install -d -m 0755 /opt/assets; \
    python3 -I -B /tests/behavioural/lib/freeze.py \
      --tree /run/state-a \
      --out /opt/assets/expectations.json \
      --contract /tests/behavioural/data/source-contract.json

# The three conformance directories, copied from the tree the freeze just ran
# against -- not from the submission, and not from a second unpack that could
# differ.  These are the inputs to the two upstream families; driver.py hashes all
# of them against expectations.json before grading anything.
RUN set -eux; \
    install -d -m 0755 /opt/assets/upstream; \
    for d in test_suite test_cmd examples; do \
      test -d "/run/state-a/$d"; \
      cp -a "/run/state-a/$d" /opt/assets/upstream/; \
    done; \
    echo "upstream inputs: $(find /opt/assets/upstream -name '*.jsonnet' | wc -l) .jsonnet files"

# Grade the reference against the expectations just frozen from it, and require all
# 2,602.  This is the last stage that can: the grading image starts from
# `toolchain` and asserts no jsonnet binary exists anywhere in it.
#
# The eighteen behaviour families carry the whole 1.00 of this stage's weight, so
# "a port that preserves jsonnet's behaviour scores 40/40" is now a statement about
# the case list rather than about the two structural modules -- and it is only true
# if every case is answerable by the program the answers came from.  That does not
# follow from having frozen them.  freeze.py records whatever the reference printed;
# an output carrying a temp path, a clock reading or an address is stable when
# recorded once and different on replay, and the resulting case is unpassable by
# anything while being indistinguishable, in a report, from a port that got it
# wrong.  No submission's score could reveal it, because every submission loses the
# same weight.
#
# Run through driver.py's own `_grade_case` and `executor.run_all`, entered at
# build.json's `bins` exactly where a submission's published programs enter, and
# under `unprivileged=True` -- the uid drop is the only execution difference between
# freezing and grading, so replaying as root is the one arrangement that could not
# detect a case which needs it.
RUN set -eux; \
    python3 -I -B /tests/behavioural/lib/driver.py \
      --replay-reference /run/state-a \
      --assets /opt/assets \
      --log /tmp/replay.log; \
    rm -f /tmp/replay.log

# The metadata reader, built from source by the pinned SDK.  It reads the ImplMap,
# ModuleRef and MemberRef tables of a publish directory, which is how a grader
# decides whether what shipped is managed IL.  Published framework-dependent so the
# final image runs it with the same `dotnet` it uses for everything else.
#
# No module calls it now.  Its caller was `artifacts`, at weight 0.00 -- it read
# these tables and reported what it found, and being paid nothing, nothing it found
# could move a score.  The tool and the proof below stay: they are what a future
# caller with a weight would need, and deleting a working, verified inspector is a
# larger change than deleting a weight-0 module.  See lib/asmmeta.py.
RUN set -eux; \
    export NUGET_PACKAGES=/opt/nuget-offline; \
    cd /tests/behavioural/lib/asminspect; \
    dotnet publish -c Release -o /run/asminspect --nologo \
      -p:PublishSingleFile=false -p:SelfContained=false \
      -p:PublishTrimmed=false -p:PublishAot=false > /tmp/asminspect.log 2>&1 \
      || { tail -40 /tmp/asminspect.log; exit 1; }; \
    install -d -m 0755 /opt/assets/tools; \
    cp -a /run/asminspect/. /opt/assets/tools/; \
    test -f /opt/assets/tools/AsmInspect.dll; \
    rm -rf /run/asminspect /tmp/asminspect.log

# Prove the reader works *in this image* before sealing it.  A reader that reported
# an empty assembly list would make the three metadata checks it feeds pass
# vacuously -- every submission would clear them by default, and nothing downstream
# could tell that apart from a clean report.  (Nothing runs those checks today; this
# is the property that has to hold before anything can again.)  It is pointed at a
# real publish directory
# containing a real assembly, and the shape of what it emits is checked against the
# keys asmmeta.py reads.  The same script asserts the case floor on the blob that
# actually shipped rather than on the code that generated it.
RUN set -eux; \
    export NUGET_PACKAGES=/opt/nuget-offline; \
    install -d -m 0755 /tmp/probe; \
    cd /tmp/probe; \
    dotnet new console -o . --no-restore > /dev/null; \
    dotnet publish -c Release -o /tmp/probe/pub --nologo \
      -p:PublishSingleFile=false -p:SelfContained=false > /dev/null; \
    dotnet /opt/assets/tools/AsmInspect.dll /tmp/probe/pub > /tmp/probe-report.json; \
    python3 -I -B /tests/behavioural/lib/manifest_check.py \
      --asminspect-report /tmp/probe-report.json \
      --expect-assembly probe \
      --frozen /opt/assets/expectations.json; \
    cd /; rm -rf /tmp/probe /tmp/probe-report.json

# Prove the build rule accepts the layouts the contract permits.  lib/build.py runs
# before any case does and every case needs what it publishes, so a mistake in it
# does not cost a case -- it costs all 40 points of this stage.  (The `build` module
# itself is weight 0.00, and nothing here is `required`: State A is C++ and
# publishes nothing, so neither could be otherwise.  Neither decides anything now:
# the stage pays only a submission that passed every scored check, and a tree whose
# programs do not publish has every case in all eighteen families recorded as
# failed.)
#
# --self-check points discovery, restore, publish and launch at a synthetic tree in
# the awkward legal shape: projects under src/<Name>/, project file names sharing no
# substring with the assemblies they produce, a library that also produces
# `jsonnet`, stale build output naming a third one, and *no solution file at the
# root*.
#
# That last one is why this check exists.  `dotnet restore` with no target fails
# outright in a directory holding neither a project nor a solution, so a `restore`
# that does not name one fails an ordinary `src/*/` port with no `.sln` at the first
# step of its build -- and it fails there, in this image, where the failure is cheap
# rather than in a graded run where it is the whole stage.
RUN set -eux; \
    export NUGET_PACKAGES=/opt/nuget-offline; \
    export DOTNET_CLI_HOME=/tmp/dotnet-home; mkdir -p "$DOTNET_CLI_HOME"; \
    python3 -I -B /tests/behavioural/lib/build.py --self-check; \
    rm -rf /tmp/dotnet-home

# Reduce /tests/behavioural to what grading actually runs, *here*, before the next
# stage copies it forward.  This is not tidiness: `COPY --from` transfers the final
# filesystem of a stage, so anything deleted here never enters a layer of the
# grading image, whereas a deletion made after the copy would leave the bytes in
# that image's history.  original.tar.gz is 22 MB of the C++ this file spent a
# stage keeping out of the container where submitted code runs, and the inspector's
# obj/ and bin/ are this stage's build scratch.
RUN set -eux; \
    rm -f /tests/behavioural/data/original.tar.gz \
          /tests/behavioural/Dockerfile /tests/behavioural/.dockerignore; \
    rm -rf /tests/behavioural/lib/asminspect/obj /tests/behavioural/lib/asminspect/bin; \
    find /tests/behavioural -name __pycache__ -type d -prune -exec rm -rf {} +; \
    find /tests/behavioural -name '*.pyc' -delete

# ---------------------------------------------------------------------------
# The grading image: the SDK base again, with no compiler for the language being
# replaced and no copy of the code being replaced.
#
# This is the one stage in the benchmark where submitted code executes, so what is
# reachable from inside it is part of the measurement.  Starting from `toolchain`
# rather than from `frozen` is what makes the assertions below true statements
# about the image rather than hopeful deletions: State A's C++, the two reference
# binaries, g++ and make were never in this filesystem to begin with.
# ---------------------------------------------------------------------------
FROM toolchain AS grading

COPY --from=frozen /opt/assets /opt/assets
COPY --from=frozen /tests/behavioural /tests/behavioural
COPY --from=infra /opt/swerefactor /opt/swerefactor

# The answers are root-only from here on, and that is a boundary rather than a
# guard rail.
#
# The thing being stopped is a read, not a write.  Grading runs as root and both
# halves of the submission run inside this image -- MSBuild builds its source, and
# the published CLI is executed once per case -- so a mode that merely forbids
# writing still lets an MSBuild target read expectations.json during the build and
# emit whatever the case wanted.  Assembling the path from string fragments defeats
# any scan of the submitted source, because no path literal ever appears in it:
#
#     <_d>$([System.String]::Concat('/opt','/assets'))</_d>
#     <_f>$([System.String]::Concat('expect','ations.json'))</_f>
#
# So the mode is 400 on the file and 700 on the directory, and every
# submission-authored process runs as uid 65534 -- vlib.run(unprivileged=True) for
# restore and publish, executor.run(unprivileged=True) for each graded case.
#
# Both halves are asserted below, because either one alone is a boundary that does
# nothing: root-only permissions with a root-run build still leak, and a dropped
# build against a world-readable file still leaks.
#
# The denial is asserted twice because `su` and the grading path drop differently.
# `su` calls initgroups, so its child is in nogroup and nothing else; subprocess
# with user=/group= does *not* call setgroups on its own, so a child can keep
# root's supplementary groups and have `id` report `groups=65534(nogroup),0(root)`
# -- at mode 0440 root:root the su check passes and the real path reads the file.
# The python check below is the real mechanism in its most permissive form -- no
# extra_groups -- so it fails on any mode that grants the root group a read.  vlib
# and executor pass extra_groups=[] on top of that.
RUN set -eux; \
    chmod -R a-w /opt/assets; \
    chmod 0700 /opt/assets; \
    chmod 0400 /opt/assets/expectations.json; \
    test "$(stat -c '%a' /opt/assets/expectations.json)" = "400"; \
    test "$(stat -c '%a' /opt/assets)" = "700"; \
    test "$(stat -c '%U' /opt/assets/expectations.json)" = "root"; \
    if su -s /bin/sh -p nobody -c 'cat /opt/assets/expectations.json' \
         >/dev/null 2>&1; then \
      echo 'FATAL: uid 65534 can read the answer key' >&2; exit 1; \
    fi; \
    if su -s /bin/sh -p nobody -c 'test -e /opt/assets/expectations.json' \
         2>/dev/null; then \
      echo 'FATAL: uid 65534 can stat the answer key' >&2; exit 1; \
    fi; \
    python3 -c 'import subprocess, sys; \
r = subprocess.run(["/bin/sh", "-c", "cat /opt/assets/expectations.json"], \
                   user=65534, group=65534, capture_output=True); \
sys.exit(0 if r.returncode else "FATAL: setuid-65534 with root groups reads the key")'; \
    test -r /opt/assets/expectations.json; \
    echo 'answer key is root-only and unreadable to uid 65534'

# What must not be reachable from a submission's process.  Each line below is one
# way to get the right answer without porting anything: run the reference, or build
# it from source and run that.  Asserted rather than assumed, because "I started
# from a clean base" is exactly the kind of claim that stays true until someone
# adds a COPY.
RUN set -eux; \
    test ! -e /run/state-a; \
    test ! -e /tests/behavioural/data/original.tar.gz; \
    ! command -v jsonnet; \
    ! command -v jsonnetfmt; \
    ! command -v g++; \
    ! command -v c++; \
    ! command -v cc; \
    ! command -v make; \
    test -z "$(find / -xdev -name 'std.jsonnet' -not -path '/opt/assets/*' -print -quit)"; \
    test -z "$(find / -xdev -name 'vm.cpp' -print -quit)"; \
    echo 'no reference implementation, no C++ toolchain'

# suite.toml is the file most likely to be edited and least likely to be tested,
# and the family map in driver.py decides whether a case is graded at all.  Both are
# checked here.  --self-check compares the 20 declared modules and their weights
# against spec.FAMILY_WEIGHTS and the frozen family counts, in every direction: a
# family no module claims does not fail anything at grading time, it silently stops
# being graded, and the stage would report a smaller denominator with no error
# anywhere.  It also holds the two structural modules at weight 0.00 and rejects any
# module declaring `required`: with the structural pair at zero the eighteen families
# are the stage's whole denominator, which is what lets State A's own behaviour reach
# 40.00.  That
# denominator summing to 1.00 is asserted by spec._self_check, which the `frozen`
# stage above already ran through freeze.py -- FAMILY_MODULES is
# tuple(spec.FAMILY_WEIGHTS), so re-asserting it here would be the same arithmetic
# on the same numbers, unreachable by any edit spec's copy does not already reject.
#
# Two different mechanisms suppress bytecode in this RUN, because the two python
# invocations below do not respond to the same one.
#
# The `frozen` stage deletes __pycache__ as its last act, but this stage copies
# /tests/behavioural out of it and then imports those modules twice -- the suite
# loader and driver.py --self-check -- so without suppression here the image ships
# bytecode the earlier cleanup was written to prevent.
#
# The export covers the first invocation.  It does nothing for the second: -I is
# isolated mode, which implies -E, and -E discards every PYTHON* variable including
# PYTHONDONTWRITEBYTECODE -- `python3 -I` reports dont_write_bytecode False with the
# variable set and visible in os.environ.  So the isolated call needs -B, the
# command-line form, which -I does not override.  This is also why the ENV block
# below cannot be what keeps the image clean at grading time: run-module.sh execs
# `python3 -I -B` for the same reason, and the -B there is load-bearing.
RUN set -eux; \
    export PYTHONDONTWRITEBYTECODE=1; \
    chmod +x /tests/behavioural/lib/run-module.sh; \
    for d in /tests/behavioural/modules/*/; do \
      test -x "$d/run.sh" || { echo "no runner in $d" >&2; exit 1; }; \
    done; \
    PYTHONPATH=/opt/swerefactor python3 -c "\
import sys; from swerefactor import config; \
s = config.Suite.load('/tests/behavioural/suite.toml'); \
missing = [m.id for m in s.modules if not (s.root / (m.dir or f'modules/{m.id}')).is_dir()]; \
sys.exit(f'suite.toml names missing module dir(s): {missing}') if missing else None; \
print(f'suite: {len(s.modules)} modules, weight {s.weight_total:g}')"; \
    python3 -I -B /tests/behavioural/lib/driver.py --self-check \
      --assets /opt/assets --suite /tests/behavioural/suite.toml

ENV DOTNET_CLI_TELEMETRY_OPTOUT=1 \
    DOTNET_NOLOGO=1 \
    DOTNET_SKIP_FIRST_TIME_EXPERIENCE=1 \
    DOTNET_CLI_HOME=/tmp/dotnet-home \
    NUGET_PACKAGES=/opt/nuget-offline \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/swerefactor \
    SWEREFACTOR_ASSETS=/opt/assets \
    SWEREFACTOR_REPO=/workspace/repo \
    SWEREFACTOR_LOG_DIR=/logs/verifier \
    TZ=UTC \
    LC_ALL=C.UTF-8 \
    LANG=C.UTF-8

# The mount points exist in the image so a misconfigured run fails on an empty
# directory rather than a confusing mkdir error partway through a module.
#
# The bytecode assertion is deliberately the last instruction that can fail: every
# RUN above it has already happened, so it is the one placement that covers all of
# them.  .dockerignore keeps the host's .pyc out of the context and the frozen stage
# deletes what the freeze wrote, but neither can speak for a RUN added after it, and
# an image shipping a stale module is the failure both were written to prevent.
RUN set -eux; \
    mkdir -p /opt/original /workspace /logs/verifier /tmp/dotnet-home; \
    found="$(find /tests -name '__pycache__' -o -name '*.pyc' | head -20)"; \
    if [ -n "$found" ]; then \
      echo "build-time bytecode shipped under /tests:" >&2; \
      echo "$found" >&2; \
      exit 1; \
    fi; \
    echo "no build-time bytecode under /tests"

WORKDIR /tests/behavioural
