# =============================================================================
# SWERefactorBench -- build02-gson-maven-to-gradle
# Stage 3 image: the verification probe.
#
#     docker build -t swerefactor/build02-verification:1 \
#         tasks/build02-gson-maven-to-gradle/tests/verification
#
# The only image in this task that can build BOTH trees, and the only one that has
# to be able to. Stage 1 reviews source and has no JDK and no build system at all.
# Stage 2 has Gradle and asserts fifteen other engine names are absent, because its
# central claim is that the migrated repository does not need them. Here the claim
# under test is different: "the Maven build ships this and yours does not" is a
# statement about two artifacts, and it is worth nothing until both have been built
# and compared. So this image has Maven 3.9.9 and Gradle 8.10.2, both real.
#
# THE INVERSE ASSERTION, AND WHY IT IS THE MOST IMPORTANT LINE IN THIS FILE
#
# Stage 2's build fails if Maven is PRESENT. This one fails if Maven is ABSENT, and
# the reason is arithmetic rather than symmetry. This stage pays ten points per
# round that finds nothing. If Maven were missing here, every candidate's build of
# the original would raise, the runner would record "did not pass on the original",
# the adjudicator would read that as a broken test rather than a defect found, and
# six rounds would report six survivals -- sixty points, to a submission that
# might have earned none of them, with nothing in any log that looks like a fault.
# A silent sixty is the worst failure mode this benchmark has, and every assertion
# at the bottom of this file exists to make some version of it a red build instead.
#
# The mirror image is just as bad and is guarded the other way: nothing here is
# stubbed, shadowed or removed from the submission's path. A submission that cannot
# build in this image would make every candidate pass on the original and fail on
# the submission -- six breaks, and a zero. Whether the submission still needs
# Maven is stage 2's question, measured there; charging for it again here would
# charge twice for one defect.
#
# The base image is the same digest every other image in this task starts from.
# That is the version contract: 570 checks in stage 2 compare class bytes against
# ground truth from a State A build, and javac is reproducible for a fixed compiler
# version and not across versions. Here it matters for the same reason in the other
# direction -- a candidate comparing two builds must be comparing two trees, not two
# compilers.
# =============================================================================

ARG INFRA_IMAGE=swerefactor/infra:1
FROM ${INFRA_IMAGE} AS infra

FROM maven:3.9.9-eclipse-temurin-17@sha256:f58d59b6273e785ac0a4477f6e9b5ba1d7731c75b906c0f7b34076f1851318cc

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    LC_ALL=C.UTF-8 \
    LANG=C.UTF-8 \
    TZ=UTC \
    MAVEN_OPTS="-Dmaven.repo.local=/root/.m2/repository" \
    GRADLE_HOME=/opt/gradle/gradle-8.10.2 \
    PATH=/opt/gradle/gradle-8.10.2/bin:$PATH

# ---------------------------------------------------------------------------
# OS tooling. `file` and `binutils` because a candidate that cannot identify a
# file it was handed writes a confusing failure into a transcript somebody has to
# read later.
# ---------------------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        file binutils \
        git \
        procps \
        python3 python3-pip python3-venv \
        unzip zip xz-utils \
    && rm -rf /var/lib/apt/lists/* \
    && java -version && mvn -v | head -1 && python3 --version

# ---------------------------------------------------------------------------
# Gradle 8.10.2 -- same version, same SHA-256, same layout as the agent image and
# stage 2. A candidate comparing the two builds must be comparing the two trees,
# so the Gradle that builds the submission here has to be the Gradle that built it
# there.
# ---------------------------------------------------------------------------
ARG GRADLE_VERSION=8.10.2
ARG GRADLE_SHA256=31c55713e40233a8303827ceb42ca48a47267a0ad4bab9177123121e71524c26
RUN curl -fsSL -o /tmp/gradle.zip \
      "https://services.gradle.org/distributions/gradle-${GRADLE_VERSION}-bin.zip" \
    && echo "${GRADLE_SHA256}  /tmp/gradle.zip" | sha256sum -c - \
    && mkdir -p /opt/gradle /opt/gradle/dist \
    && unzip -q /tmp/gradle.zip -d /opt/gradle \
    && mv /tmp/gradle.zip "/opt/gradle/dist/gradle-${GRADLE_VERSION}-bin.zip" \
    && gradle --version

# ---------------------------------------------------------------------------
# The harness, and the interpreter a candidate runs in.
#
# A venv at /opt/venv rather than the system interpreter, and run-candidate.sh
# invokes it by absolute path. The reason is that a candidate is code an adversary
# wrote: it may pip-install nothing, but it can and will import things, and keeping
# its interpreter separate from the one running the harness means a candidate that
# breaks its own environment breaks only its own.
# ---------------------------------------------------------------------------
COPY --from=infra /opt/swerefactor /opt/swerefactor
ENV PYTHONPATH=/opt/swerefactor

COPY requirements-candidate.txt /opt/requirements-candidate.txt
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --require-hashes --no-deps \
        -r /opt/requirements-candidate.txt \
    && /opt/venv/bin/python -m pytest --version \
    && /opt/venv/bin/python -c "import pytest_timeout; print('timeout plugin ok')"

# ---------------------------------------------------------------------------
# The offline closure, in Maven's local repository layout, from the same pinned
# deps.txt the agent image and stage 2 use -- byte for byte, which is asserted
# below rather than assumed.
#
# One manifest serves both dialects because fetch_deps.py writes each artifact's
# POM as well as its jar. Maven implies the POM; Gradle refuses to resolve a module
# without one and resolves a plugin marker from its POM alone. So the same directory
# tree satisfies a Maven reactor and a Gradle multi-project, which is what makes it
# possible to build both trees in one image at all.
#
# maven-deploy-plugin:3.1.2 is in the closure, which is what lets the `publish`
# configuration run a real `mvn deploy` into a file:// repository with no network.
# protoc arrives as com.google.protobuf:protoc:3.17.3:exe:linux-x86_64, so the proto
# project's codegen resolves an artifact rather than needing a compiler installed.
# ---------------------------------------------------------------------------
COPY data/offline/deps.txt /opt/srb/deps.txt
COPY data/offline/fetch_deps.py /opt/srb/fetch_deps.py
RUN python3 /opt/srb/fetch_deps.py --manifest /opt/srb/deps.txt \
        --repo /root/.m2/repository \
    && rm -f /opt/srb/fetch_deps.py

# Both halves of the offline plumbing, at the same paths as the agent image. Maven
# reads settings.xml for the local repository and offline mode; Gradle reads the
# init script for the repository mapping and the five vendored plugin ids. Neither
# declares a task, a plugin application or a convention -- they say where artifacts
# come from and nothing else, which is why the same files can be present for both
# trees without either being helped.
COPY data/offline/settings-offline.xml /root/.m2/settings.xml
COPY data/offline/srb-offline.gradle /root/.gradle/init.d/srb-offline.gradle

# ---------------------------------------------------------------------------
# The probe.
# ---------------------------------------------------------------------------
COPY . /tests/verification
RUN chmod +x /tests/verification/run-candidate.sh

# ---------------------------------------------------------------------------
# The shared inputs are the shared inputs.
#
# deps.txt, settings-offline.xml and srb-offline.gradle are copies of files the
# agent image also ships. If a copy drifted, this stage would build one of the trees
# against a different dependency closure than the one it was written for, and every
# candidate would see a difference that belongs to the images rather than to the
# repositories. Compared by digest against the values measured when the copies were
# made.
# ---------------------------------------------------------------------------
RUN set -eu; \
    check() { \
        actual=$(sha256sum "$1" | cut -c1-16); \
        if [ "$actual" != "$2" ]; then \
            echo "FATAL: $1 has digest $actual, expected $2." >&2; \
            echo "This file is a copy of one the agent image ships. A drifted" >&2; \
            echo "copy means the two trees are built against different inputs," >&2; \
            echo "and every candidate would see a difference that belongs to" >&2; \
            echo "the images rather than to the repositories." >&2; \
            exit 1; \
        fi; \
    }; \
    check /opt/srb/deps.txt                          8c21fb43b03e006d; \
    check /root/.m2/settings.xml                     dd1e2f901e381beb; \
    check /root/.gradle/init.d/srb-offline.gradle    9f1bb633e5ee6759; \
    echo "shared inputs: three files, digests match the agent image"

# ---------------------------------------------------------------------------
# BOTH build systems are here, and this is the assertion that guards the silent
# sixty. See the header.
# ---------------------------------------------------------------------------
RUN set -eu; \
    for required in mvn gradle java javac jar; do \
        if ! command -v "$required" >/dev/null 2>&1; then \
            echo "FATAL: this image has no $required." >&2; \
            echo "" >&2; \
            echo "Stage 3 builds BOTH trees. A missing toolchain here does not" >&2; \
            echo "fail visibly: it makes the build raise for every candidate," >&2; \
            echo "which the runner records as 'did not pass on the original'," >&2; \
            echo "which the adjudicator reads as six rounds finding nothing," >&2; \
            echo "which pays the submission all sixty points. Nothing in any" >&2; \
            echo "log would look like a fault." >&2; \
            exit 1; \
        fi; \
    done; \
    mvn -v | head -1; \
    gradle --version | grep -i '^Gradle'; \
    echo "stage 3: both build systems present, as intended"

# No prebuilt gson, on either side. A closure that could resolve
# com.google.code.gson:gson:2.10.1 would let either build satisfy a dependency with
# the artifact under migration instead of producing it, and the two trees would then
# be compared on a jar neither of them built.
#
# Found by walking the group for a 2.10.1 directory rather than by naming the four
# artifact ids. The group and the version cannot be got wrong that way, and gson
# 2.8.x -- a real dependency of one of the plugins -- stays resolvable.
RUN set -eu; \
    stale=$(find /root/.m2/repository/com/google/code/gson \
                 -mindepth 2 -maxdepth 2 -type d -name 2.10.1 2>/dev/null || true); \
    if [ -n "$stale" ]; then \
        echo "FATAL: the artifacts under migration are resolvable offline:" >&2; \
        echo "$stale" >&2; \
        exit 1; \
    fi; \
    echo "no prebuilt gson 2.10.1 in the closure"

# ---------------------------------------------------------------------------
# The probe's own consistency: six adversaries, four configurations, a prompt
# that names all four and documents every helper that exists, a scope that both
# allows and denies, and a library that exports nothing naming either tree.
#
# Each of those is a way this stage can pay points it should not, and none of them
# fails visibly at run time. check-probe.py has the arithmetic in its docstring.
# ---------------------------------------------------------------------------
RUN python3 /tests/verification/check-probe.py

# The candidate's interpreter must be able to import the library the prompt
# promises, using exactly the path run-candidate.sh sets. A prompt that documents
# `import srbgson` against an interpreter that cannot import it costs every one of
# the six rounds its first candidate.
RUN PYTHONPATH=/tests/verification/lib /opt/venv/bin/python -c "\
import srbgson, jarinspect, classfile; \
print('candidate interpreter: srbgson %d configurations, jarinspect, classfile' \
      % len(srbgson.CONFIGURATIONS))"

# Nothing under test may be importable from either interpreter. Gson is a Java
# library so this is cheap insurance rather than a live risk, but the shape of the
# mistake is worth ruling out: a candidate that could reach a preinstalled copy of
# what it is measuring would compare that copy against itself and find nothing.
RUN set -eu; \
    for py in /usr/bin/python3 /opt/venv/bin/python; do \
        if "$py" -c "import gson" 2>/dev/null; then \
            echo "FATAL: $py can import a module named gson" >&2; exit 1; \
        fi; \
    done; \
    echo "nothing under test is importable"

# ---------------------------------------------------------------------------
# The smoke build: both dialects, offline, end to end.
#
# This is the last line of defence against the silent sixty, and the only one that
# tests the thing itself rather than a proxy for it. Everything above could pass in
# an image where the Maven side cannot actually resolve its plugins offline -- and
# that image would pay sixty points to every submission. So a throwaway two-module
# project is built here with each engine in turn, using the same offline plumbing a
# real build will use, and the image fails if either one cannot.
#
# It is deliberately not gson: gson is not in this image, and a smoke test that
# needed it would be a smoke test that shipped the thing under measurement.
#
# Every lifecycle plugin is pinned to the version the closure actually holds.
# The first attempt at this file left them unpinned and Maven 3.9.9 reached for its
# own defaults -- compiler-plugin 3.13.0, which the closure does not have -- so the
# smoke test failed against an image that was in fact fine. The reference build pins
# its plugins in the parent POM for the same reason, and a closure is only a closure
# with respect to a stated set of versions.
# ---------------------------------------------------------------------------
RUN set -eu; \
    mkdir -p /tmp/smoke/m/src/main/java/p /tmp/smoke/g/src/main/java/p; \
    printf 'package p;\npublic class C { public static void main(String[] a) {} }\n' \
        | tee /tmp/smoke/m/src/main/java/p/C.java \
        > /tmp/smoke/g/src/main/java/p/C.java; \
    printf '<project xmlns="http://maven.apache.org/POM/4.0.0">\n\
  <modelVersion>4.0.0</modelVersion>\n\
  <groupId>srb</groupId><artifactId>smoke</artifactId><version>1</version>\n\
  <properties>\n\
    <maven.compiler.release>8</maven.compiler.release>\n\
    <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>\n\
  </properties>\n\
  <dependencies><dependency>\n\
    <groupId>junit</groupId><artifactId>junit</artifactId>\n\
    <version>4.13.2</version><scope>test</scope>\n\
  </dependency></dependencies>\n\
  <build><pluginManagement><plugins>\n\
    <plugin><artifactId>maven-clean-plugin</artifactId><version>3.2.0</version></plugin>\n\
    <plugin><artifactId>maven-resources-plugin</artifactId><version>3.3.1</version></plugin>\n\
    <plugin><artifactId>maven-compiler-plugin</artifactId><version>3.10.1</version></plugin>\n\
    <plugin><artifactId>maven-surefire-plugin</artifactId><version>3.2.5</version></plugin>\n\
    <plugin><artifactId>maven-jar-plugin</artifactId><version>3.3.0</version></plugin>\n\
    <plugin><artifactId>maven-install-plugin</artifactId><version>3.1.2</version></plugin>\n\
    <plugin><artifactId>maven-deploy-plugin</artifactId><version>3.1.2</version></plugin>\n\
  </plugins></pluginManagement></build>\n\
</project>\n' > /tmp/smoke/m/pom.xml; \
    cd /tmp/smoke/m; \
    mvn -B -o --settings /root/.m2/settings.xml clean package -DskipTests \
        > /tmp/smoke/m.log 2>&1 || { tail -40 /tmp/smoke/m.log >&2; \
        echo "FATAL: the Maven side cannot build offline in this image." >&2; \
        echo "Every candidate would then fail to build the original, which" >&2; \
        echo "reads as six survivals and pays sixty points." >&2; exit 1; }; \
    test -f /tmp/smoke/m/target/smoke-1.jar; \
    cd /tmp/smoke/g; \
    printf "rootProject.name = 'smoke'\n" > settings.gradle; \
    printf "plugins { id 'java-library'; id 'biz.aQute.bnd.builder' }\n\
java { toolchain { languageVersion = JavaLanguageVersion.of(17) } }\n\
tasks.withType(JavaCompile) { options.release = 8 }\n\
dependencies { testImplementation 'junit:junit:4.13.2' }\n" > build.gradle; \
    gradle --offline --no-daemon --console=plain clean assemble \
        > /tmp/smoke/g.log 2>&1 || { tail -40 /tmp/smoke/g.log >&2; \
        echo "FATAL: the Gradle side cannot build offline in this image." >&2; \
        echo "Every candidate would then fail to build the submission, which" >&2; \
        echo "reads as six breaks and pays zero." >&2; exit 1; }; \
    test -f /tmp/smoke/g/build/libs/smoke.jar; \
    echo "smoke: both dialects build offline, and both produced a jar"; \
    cd /; rm -rf /tmp/smoke /root/.gradle/caches /root/.gradle/daemon

# The compiled library would otherwise ship as a layer that differs per build.
RUN find /tests/verification -name __pycache__ -prune -exec rm -rf {} + || true

# ---------------------------------------------------------------------------
# Run-time contract.
#
# The harness stages both trees itself, under salted tokens, and passes each one as
# $SRB_TARGET with its role as the candidate command's last argument. Nothing here
# names either tree: /opt/original and /workspace/repo exist so a misconfigured run
# fails against an empty directory rather than a confusing error mid-round, and the
# stage never reads them by those names.
# ---------------------------------------------------------------------------
RUN mkdir -p /workspace/repo /opt/original /logs/verifier /tmp/srb-verification

ENV SRB_WORK=/tmp/srb-verification \
    SRB_MVN_SETTINGS=/root/.m2/settings.xml

WORKDIR /tests/verification
CMD ["python3", "-m", "swerefactor.cli", "verification", "--task-dir", "/", \
     "--repo", "/workspace/repo", "--original", "/opt/original"]
