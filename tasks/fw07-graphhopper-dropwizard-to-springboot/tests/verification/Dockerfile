# =============================================================================
# SWERefactorBench — fw07-graphhopper-dropwizard-to-springboot
# Stage 3 image: the verification probe.
#
#     docker build -t swerefactor/fw07-verification:1 \
#         tasks/fw07-graphhopper-dropwizard-to-springboot/tests/verification
#
# What this image is for: give six models a budget each to find one request the
# original answers correctly and the submission does not.  Stage 2 asks whether the
# submission reproduces a fixed 88-case recording; this stage asks whether anything
# OUTSIDE that recording diverges, which means it has to build BOTH trees and serve
# them live, side by side, for the whole stage.
#
# That is the requirement that shapes everything below.  Stage 1 reads code and
# needs no toolchain at all.  Stage 2 builds two trees once and drives one fixed
# list against them.  This stage builds two trees and then keeps four servers alive
# across every candidate of every round, because a stage that re-imported a graph
# per candidate would spend its entire budget importing graphs.
#
# WHY THIS IMAGE KEEPS THE RETIRED STACK, WHICH IS THE OPPOSITE OF THE AGENT'S.
# The agent's environment has no dropwizard-core jar; that omission is how the
# retirement is enforced.  This image keeps the complete closure, Dropwizard and
# Jersey and HK2 included, for the same reason stage 2 does: the reference IS State
# A, its whole web layer is written against that stack, and it is compiled here at
# grade time.  A stage that cannot build its own oracle cannot show its
# expectations are reachable.
#
# Four things have to be true when this image ships, and each is asserted below:
#
#   1. probe.toml and the driver agree with each other and with the scoring
#      policy.  Cheap, so it runs first.  See check-probe.py.
#   2. The three modules copied from stage 2 are byte-identical to stage 2's.  The
#      claim that a stage-3 finding is a real divergence rests on both stages
#      asking the question with the same client and building with the same command.
#   3. State A builds offline in this image and its jar is discoverable.
#   4. A candidate handed to run-candidate.sh reaches four live servers and gets
#      correct answers from them — run through the same driver the harness runs,
#      not through private build-time commands.
#
# 3 and 4 are the ones that matter, and the reason is the same for both: if the
# reference cannot be built or cannot be reached at grade time, every candidate
# "fails on the original", the adjudicator upholds none of them, and all six rounds
# record a survival that was never earned.  That failure is silent and it pays the
# submission all 60 points.  Discovering it here costs an image build.
#
# Base image pinned to the same digest stage 2 and environment/Dockerfile use:
# maven:3.9.11-eclipse-temurin-17 -> Debian bookworm, Maven 3.9.11, Temurin JDK
# 17.0.17.  It has to be the same build: this stage compares two jars, and "the two
# behave the same" is a measurement about the repositories only if the compiler and
# the runtime are held fixed — across the two trees, and across the two stages that
# grade them.
#
# Network is available while this builds and gone when it runs.
# =============================================================================

ARG INFRA_IMAGE=swerefactor/infra:1
FROM ${INFRA_IMAGE} AS infra

FROM maven:3.9.11-eclipse-temurin-17@sha256:e4a7ace3dc0d645ed97f8d9ad0b0d3f0b14fa8d150138f27f116d7105a639b82

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    LC_ALL=C.UTF-8 \
    LANG=C.UTF-8 \
    TZ=UTC \
    SOURCE_DATE_EPOCH=1705276800 \
    MAVEN_OPTS="-Dmaven.repo.local=/root/.m2/repository"

# ---------------------------------------------------------------------------
# OS tooling.  Installed while the image build still has network.
#
# `procps` is not optional: the driver keeps servers alive across candidates and
# checks liveness by signalling a process group, and it reaps by group too.  A reap
# that cannot enumerate processes leaves a JVM holding a port that the replacement
# then fails to bind — which reads as "the submission would not boot".
# ---------------------------------------------------------------------------
RUN apt-get -o Acquire::Retries=8 update \
    && apt-get -o Acquire::Retries=8 install -y --no-install-recommends \
        ca-certificates \
        curl jq \
        file binutils \
        git \
        procps \
        python3 python3-pip \
        unzip zip xz-utils \
    && rm -rf /var/lib/apt/lists/* \
    && java -version && mvn -v | head -1 && python3 --version

# ---------------------------------------------------------------------------
# The harness.  From the infra image, so every stage agrees on what a check is.
# ---------------------------------------------------------------------------
COPY --from=infra /opt/swerefactor /opt/swerefactor

# ---------------------------------------------------------------------------
# pytest, hash-pinned, at stage 2's versions.  See requirements-candidate.txt: a
# candidate that passes this stage's pytest and would have failed stage 2's is
# reporting a difference between two test runners, and the report would name the
# submission for it.
# ---------------------------------------------------------------------------
COPY requirements-candidate.txt /opt/requirements-candidate.txt
RUN pip3 install --break-system-packages --require-hashes --no-deps \
        -r /opt/requirements-candidate.txt \
    && python3 -m pytest --version

# ---------------------------------------------------------------------------
# data/ first, and alone, because everything below it is expensive.  Editing a
# prompt or a scope line must not rebuild a 20-minute Maven closure.
# ---------------------------------------------------------------------------
COPY data /tests/verification/data

# ---------------------------------------------------------------------------
# Offline closure, part 1 of 2: everything State A resolves.
#
# Warmed by BUILDING State A, for the reason the other two images give: a build is
# exact by construction — it warms the plugins, the plugins' own dependencies and
# the surefire provider — and a hand-written coordinate list always misses one.
#
# original.tar.gz is in this stage's data/ for exactly this layer.  At run time the
# original arrives as a mounted tree and this copy is not read; it is here because
# the warm has to happen while the image still has network, and it has to happen
# against the same snapshot the run will build.
#
# The mtime re-stamp is load-bearing and its failure is silent.  Every entry in the
# snapshot carries mtime 0 so the archive bytes depend only on content;
# maven-resources-plugin then copies unfiltered resources incrementally, asking
# whether source.lastModified() > dest.lastModified(), and a destination that does
# not exist yet answers 0.  With the source also at 0 the comparison is false, so
# the plugin logs "Copying 76 resources" and writes none of them.  Core's 49
# translation files vanish and the symptom is a German route answered in English.
# harness/maven.py applies the same stamp to the tree it builds at grade time.
#
# Three passes, each warming what the previous does not:
#   install -DskipTests   the compile closure, every module's jar, the plugins, and
#                         — through frontend-maven-plugin's `npm pack` — the map UI
#                         tarball into /root/.npm/_cacache
#   test-compile          every module's TEST-scope jars.  This stage runs no tests,
#                         but it does not get to skip them either: the graded
#                         command is `-DskipTests package`, and `-DskipTests` skips
#                         test EXECUTION while test-compile still runs.  A module
#                         whose test scope was never warmed therefore fails at
#                         resolution during a `package` that was never going to run
#                         a test, and the failure reads as "the submission does not
#                         build" — exit 71, every candidate of every round invalid.
#   test on the web tier  the surefire provider, which `-DskipTests` does NOT reach.
#                         Warmed anyway, for one module rather than ten: a
#                         submission is free to bind surefire differently or to a
#                         different phase, and this is cheap insurance against a
#                         resolution failure in a plugin no submission was asked to
#                         touch.  It is also a proof that State A's web layer is
#                         green in this image before anything is measured against it.
# ---------------------------------------------------------------------------
RUN cd /tests/verification/data && sha256sum -c original.sha256 \
    && mkdir -p /tmp/warm \
    && tar -xzf original.tar.gz -C /tmp/warm --strip-components=1 \
    && find /tmp/warm -exec touch -d '2024-01-01T00:00:00Z' {} + \
    && cd /tmp/warm \
    && mvn -B -DskipTests install \
    && mvn -B -DskipTests test-compile \
    && mvn -B -pl web-bundle,web test \
    && cd / && rm -rf /tmp/warm

# The repository under test must not be resolvable as a binary.  The warm above
# installed the reactor's own modules into the local repository; left there, a
# submission that deleted a Maven module would resolve State A's jar for it and
# appear to have kept the behaviour — and in THIS stage it would then boot and
# answer every request correctly, so no adversary could find anything and all six
# rounds would pay out.
#
# Exactly the eleven reactor artifacts, named rather than removed by group prefix:
# `com.graphhopper.external:jackson-datatype-jts` is an upstream fork published
# under the project's own group and `graphhopper-web-api` depends on it, so deleting
# the group wholesale takes it too and breaks the reference build with a message
# about something no submission touched.
RUN cd /root/.m2/repository/com/graphhopper \
    && rm -rf graphhopper-parent graphhopper-core graphhopper-web graphhopper-web-api \
              graphhopper-web-bundle graphhopper-nav graphhopper-reader-gtfs \
              graphhopper-map-matching graphhopper-tools graphhopper-example \
              directions-api-client-hc \
    && test -d external \
    && test -z "$(find . -name 'graphhopper-*.jar' -print -quit)" \
    && test -z "$(find . -name 'directions-api-*.jar' -print -quit)"

# ---------------------------------------------------------------------------
# Offline closure, part 2 of 2: everything the target stack needs.
#
# State A's build cannot warm this half — the target framework appears nowhere in
# it — and this stage has to compile submissions written against it.
# data/offline-deps is the same pom the agent environment and stage 2 warm from,
# byte for byte, which is what makes "resolvable here" and "resolvable there" the
# same claim; `swerefactor validate` enforces the identity.  A stage resolving from a
# different closure than the agent had would fail honest submissions for reasons
# they cannot see — and in this stage that failure is an exit 71, which invalidates
# every candidate of every round.
# ---------------------------------------------------------------------------
RUN mkdir -p /tmp/offline-deps/src/main/java/local/swerefactor \
    && cp /tests/verification/data/offline-deps/pom.xml /tmp/offline-deps/pom.xml \
    && cp /tests/verification/data/offline-deps/Warm.java \
          /tmp/offline-deps/src/main/java/local/swerefactor/Warm.java \
    && cd /tmp/offline-deps \
    && mvn -B -DskipTests dependency:go-offline \
    && mvn -B -DskipTests compile \
    && test -f target/classes/local/swerefactor/Warm.class \
    && cd / && rm -rf /tmp/offline-deps

# ---------------------------------------------------------------------------
# Maven's and npm's side of the offline plumbing.  The last fetching layer is
# above; from here nothing reaches the network.
#
# The settings file is installed TWICE, at the two paths two callers look for:
# /root/.m2/settings.xml so a bare `mvn` in this image is offline without a flag,
# and /opt/srb/settings-offline.xml which is what harness/maven.py passes with
# `--settings`.  That flag is not a convenience: Maven records per artifact which
# repository id it came from and refuses, offline, to use one whose provenance
# names a repository it is not configured with now.  Both copies are the same file,
# so the two callers cannot drift apart.
#
# npmrc is here because State A does not keep its map UI in the tree — it runs
# `npm pack @graphhopper/graphhopper-maps-bundle` through frontend-maven-plugin and
# untars the result into web-bundle's classes, from where /maps/ is served.  The
# warm above paid for that pack with network; this file makes every later pack
# cache-only.  Without it the reference build dies at EAI_AGAIN on a step no
# submission wrote, and a round assigned to static assets attacks a server that
# has none.
# ---------------------------------------------------------------------------
RUN mkdir -p /opt/srb
COPY data/settings-offline.xml /root/.m2/settings.xml
COPY data/settings-offline.xml /opt/srb/settings-offline.xml
COPY data/npmrc.offline /root/.npmrc

# Maven refuses, offline, to use an artifact whose recorded provenance names a
# repository it is not configured with.  The mirror keeps the id `central` so the
# warm's own records and its range metadata still match — web-bundle resolves
# several org.webjars.npm dependencies through version ranges, and those read
# `maven-metadata-central.xml` — but a few artifacts came through State A's
# `github` repository declaration and would look un-downloaded.  Deleting these
# records turns the closure into what it now is: locally installed, always
# available, never re-fetched.
RUN find /root/.m2/repository -type f \
         \( -name '_remote.repositories' -o -name '*.lastUpdated' \) -delete \
    && test -z "$(find /root/.m2/repository -name '_remote.repositories' -print -quit)"

# ---------------------------------------------------------------------------
# The stage's own code.  Last of the COPYs, so editing the driver or a prompt
# rebuilds seconds rather than the closure above.
# ---------------------------------------------------------------------------
COPY . /tests/verification
RUN chmod +x /tests/verification/run-candidate.sh

# `lib/` holds both the copied harness modules and the candidate plugin, which
# pytest loads by name (`-p srbcandidate`), so the directory has to be importable.
# On PYTHONPATH rather than pip-installed: it is four files, and a package here
# would need a version number nothing would ever read.
ENV PYTHONPATH=/tests/verification/lib:/opt/swerefactor

# --- Assertion 1: the probe and the driver are coherent ----------------------
# First, because it is the cheap one.
RUN python3 /tests/verification/check-probe.py

# --- Assertion 2: the copied modules are the ones that were reviewed ---------
# The whole claim that a stage-3 finding is a real divergence rests on both stages
# asking the question with the same client and building with the same command.  If
# these drift, a candidate can pass here and fail there, or the reverse, and the
# report names the submission for a difference between two harnesses.
#
# What this layer can and cannot check, stated because it was read the other way
# once.  A digest in this file is a constant: the build context is
# tests/verification, so tests/behavioural is not present and CANNOT be compared
# here.  These three values are therefore a pin on THIS stage's copies -- they
# catch an edit to the file beside this Dockerfile, and nothing else.  Stage 2 is
# free to move out from under them, and did: it adopted swerefactor.contract's
# submission_env in maven.py and server.py while these copies kept
# `dict(os.environ)`, and the pin passed throughout because it had been re-derived
# from the stage-3 files rather than the stage-2 ones.  Both were 12 lines apart
# for however long, in an image whose own harness/__init__.py says all three are
# byte-identical to tests/behavioural/lib/harness/.
#
# The two-directory comparison lives where both directories exist:
# infra/tests/test_layout.py, which walks every task and diffs the two harness
# dirs.  That test is what actually holds the invariant; this layer holds the
# weaker one it is able to hold, on the theory that an image should still refuse to
# ship if someone edits its own copy without re-pinning.
#
# harness/__init__.py is deliberately NOT on this list — it is this stage's own
# file, naming what was copied and what was not.  See its docstring.
RUN set -eu; \
    fail=0; \
    check() { \
      got=$(sha256sum "/tests/verification/lib/harness/$1" | cut -d' ' -f1); \
      if [ "$got" != "$2" ]; then \
        echo "FATAL: lib/harness/$1 is not stage 2's copy." >&2; \
        echo "       got  $got" >&2; \
        echo "       want $2" >&2; \
        fail=1; \
      fi; \
    }; \
    check wire.py   7e753cf4f025fe7c518fae06eef451b0246c032d58b223956d1692b35ff642c8; \
    check maven.py  08cdb5f9dd3c211a99dfca74ffa6f2099771cdf1928ce9ca0931b0bfba0bc26c; \
    check server.py e5c33db6643d87a255eed878b3c9327633d1721b5c939ce48f1896bcfc304752; \
    if [ "$fail" -ne 0 ]; then \
      echo "       Both stages must ask the question the same way, or a finding" >&2; \
      echo "       here is a difference between two harnesses rather than" >&2; \
      echo "       between two repositories." >&2; \
      exit 1; \
    fi; \
    echo "wire, maven and server match stage 2 byte for byte"

# The candidate plugin and the driver must import.  A syntax error in either
# surfaces at grade time as every candidate failing on both trees, which the
# adjudicator reads as nothing found and the scorer pays as six survivals.
#
# The three SRB_* below are not scaffolding for this check: they are what
# CandidateRunner.execute puts in the environment of every single invocation, and
# run-candidate.sh refuses to start without them.  stage3.py reads them at module
# level, so importing it with an empty environment raises KeyError on SRB_TARGET
# before a line of it is checked -- which is how this step failed on its first
# build.  Setting them here is not overriding the graded configuration, it is
# reproducing it; a check run under an environment the grade-time path never sees
# is a check on a mode nothing else uses.  The values are only Path()'d and joined
# during import: nothing is built, booted or written.
RUN SRB_TARGET=/opt/original \
    SRB_TARGET_TOKEN=selfcheck \
    SRB_WORK=/tmp/srb-selfcheck \
    python3 -c "\
import sys; sys.path.insert(0, '/tests/verification/lib');\
import srbcandidate, stage3;\
from harness import wire, maven, server;\
print('plugin: %d profiles (%s); driver: %d profiles; ladder: %d rungs'\
      % (len(srbcandidate.PROFILES), ', '.join(sorted(srbcandidate.PROFILES)),\
         len(stage3.PROFILES), len(server.LADDER)))" \
    && python3 -m py_compile /tests/verification/lib/stage3.py \
                             /tests/verification/lib/srbcandidate.py \
                             /tests/verification/data/selfcheck_candidate.py \
    && bash -n /tests/verification/run-candidate.sh

# --- Assertions 3 and 4: the grade-time path works end to end ----------------
#
# One layer, because the two claims are one execution: a candidate is handed to
# run-candidate.sh exactly as the harness hands one over — same environment
# variables, same role argument, same script — and it has to come back zero.
#
# Deliberately NOT built and booted by private commands written for this layer.
# A proof that some sequence of Maven and java invocations works is not the claim
# anyone needs; the claim needed is that THIS driver, reached THIS way, builds the
# tree, boots both profiles and serves a pytest file.  So the layer sets
# SRB_TARGET, SRB_TARGET_TOKEN, SRB_CANDIDATE and SRB_WORK the way
# CandidateRunner.execute does, passes the role as the last argument the way it
# does, and reads the exit status the way it does.
#
# The tree under test is State A, because at image-build time there is no
# submission.  So what the self-check asserts is also a statement of what the
# reference does: both profiles up, /info answering JSON, a car route across
# Andorra returning a path, /route-pt registered under the transit configuration
# and 404 under the other, and the admin connector answering on its own port.  See
# data/selfcheck_candidate.py for why each one is worth a build failure.
#
# What this layer cannot prove is the submission side, which does not exist yet.
# It proves the half that is common to both and the half whose failure is silent.
#
# The extraction below deliberately does NOT re-stamp mtimes, unlike the warm
# above and unlike every other place this tarball is unpacked.  Not an oversight:
# maven.normalise_mtimes now does it inside build_tree, so the tree must arrive
# here in the state the harness will really hand over -- epoch 0, straight out of
# an archive whose timestamps are fixed so its bytes are reproducible.  Stamping it
# here would prove the tarball can be built after someone fixes it up, which is not
# a claim about grade time.  Leaving it proves the driver fixes it up itself.
# This layer is what caught the bug: State A built, shaded a 45 MiB jar with none
# of its 49 translation resources in it, and every rung of the launch ladder threw
# `No input stream found in class path!?` before binding a port -- exit 74, on the
# reference, which at grade time is six rounds of survivals nothing was measured
# for.  A green run of this layer now reports the re-stamp count in its log.
RUN set -u; \
    export MAVEN_OPTS="-Dmaven.repo.local=/root/.m2/repository -Xmx2g -Djava.awt.headless=true"; \
    mkdir -p /tmp/proof/tree /tmp/proof/work/run; \
    tar -xzf /tests/verification/data/original.tar.gz \
        -C /tmp/proof/tree --strip-components=1; \
    SRB_TARGET=/tmp/proof/tree \
    SRB_TARGET_TOKEN=selfcheck0000000 \
    SRB_CANDIDATE=/tests/verification/data/selfcheck_candidate.py \
    SRB_CANDIDATE_NAME=selfcheck \
    SRB_WORK=/tmp/proof/work/run \
    SRB_ADV_DATA=/tests/verification/data \
    bash /tests/verification/run-candidate.sh original > /tmp/proof/out.log 2>&1; \
    rc=$?; \
    tail -40 /tmp/proof/out.log; \
    if [ "$rc" -ne 0 ]; then \
        echo "FATAL: the stage-3 path does not work on the REFERENCE (exit $rc)." >&2; \
        case "$rc" in \
          71) echo "       71: State A does not build offline in this image. A" >&2; \
              echo "       layer above missed something in the closure." >&2 ;; \
          74) echo "       74: State A built but never bound a port. Look for the" >&2; \
              echo "       launch ladder's attempts in the log above." >&2 ;; \
          70) echo "       70: the driver itself broke — a bug in stage3.py, not" >&2; \
              echo "       in the closure or the tree." >&2 ;; \
          *)  echo "       the self-check candidate's assertions failed, so the" >&2; \
              echo "       reference is not answering what this stage believes it" >&2; \
              echo "       answers. Every claim in prompt.txt about State A rests" >&2; \
              echo "       on these." >&2 ;; \
        esac; \
        echo "       At grade time this means every candidate fails on the" >&2; \
        echo "       original, no round can uphold anything, and all six pay the" >&2; \
        echo "       submission for survivals it did not earn." >&2; \
        sed -n '1,200p' /tmp/proof/out.log >&2; \
        exit 1; \
    fi; \
    SRB_TARGET=/tmp/proof/tree SRB_TARGET_TOKEN=selfcheck0000000 \
    SRB_WORK=/tmp/proof/work/run \
    python3 /tests/verification/lib/stage3.py stop || true; \
    echo "proof: run-candidate.sh built State A offline, booted both graph"; \
    echo "       profiles, and a candidate reached all four connectors"; \
    rm -rf /tmp/proof

# The negative caches and the fault ledger must be the only things the driver
# leaves behind, and nothing from the proof may ship.  A stale $STATE in the image
# would be read at grade time as "this tree is already built" for a token that will
# never exist, which is harmless, and as a `.buildfail` for one that might, which is
# not.
RUN test ! -e /tmp/proof \
    && rm -rf /tmp/swerefactor-verification \
    && find /tests/verification -name __pycache__ -prune -exec rm -rf {} + || true

# ---------------------------------------------------------------------------
# Run-time contract.
#
# The two trees are staged by CandidateRunner under $SRB_WORK/trees and reached
# through $SRB_TARGET; the mounted original at /opt/original and the submission at
# /workspace/repo are what the harness copies FROM.  This stage reads neither
# directly — everything it builds, it builds from the copy it was pointed at.
#
# MAVEN_OPTS is set for this container's own shell only.  Every graded Maven
# invocation composes its own environment in harness/maven.py, which drops
# JAVA_TOOL_OPTIONS and MAVEN_ARGS: a submitted tree that set either would
# otherwise inject flags into a command this harness composed deliberately.
# ---------------------------------------------------------------------------
RUN mkdir -p /workspace/repo /opt/original /logs/verifier

ENV SRB_INFRA=/opt/swerefactor \
    SRB_RESULTS=/logs/verifier \
    SRB_ADV_DATA=/tests/verification/data \
    SRB_M2_REPO=/root/.m2/repository \
    SRB_OFFLINE_SETTINGS=/opt/srb/settings-offline.xml

WORKDIR /workspace

CMD ["/bin/bash"]
