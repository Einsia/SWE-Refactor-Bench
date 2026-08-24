# =============================================================================
# SWERefactorBench -- build02-gson-maven-to-gradle
# Stage 2 image: the behavioural modules.
#
#     docker build -t swerefactor/build02-behavioural:1 \
#         tasks/build02-gson-maven-to-gradle/tests/behavioural
#
# A build machine that can run the two build systems this task is a migration
# between, and nothing else. It holds a JDK, Gradle, Maven, a warmed offline
# artifact repository, pytest, and the ten modules -- and it holds no Ant, no make,
# no cmake, no bazel, and no gson 2.10.1 artifact of any kind.
#
# Why Maven is here
# -----------------
# Because this stage's expectations came from a Maven build. Every one of the 2,678
# checks below reads an artifact -- 282 entry names, 570 .class comparisons, 21
# manifest headers, 1,328 test cases -- and all of it was frozen from State A, the
# unmigrated tree. A stage that could not build its own oracle could not show that
# what it demands is satisfiable, and would charge a correct submission for any
# defect in the ground truth. So the suite drives whichever build system the
# delivered tree declares (lib/dialect.py), and State A scores what State A is
# worth: full marks, because nothing about the repository's behaviour is supposed
# to change.
#
# That is not a hole. Whether the migration happened is stage 1's question, asked
# over the files by a reviewer, through required gates -- `maven_retired` and
# `gradle_is_the_build` -- and a required-gate failure zeroes the submission before
# this image is ever run. A submission that keeps Maven therefore scores zero for
# the reason it should, named in the words that describe it, rather than by
# failing 2,678 artifact checks that would each have read as something else.
#
# The other absences remain the instrument they were: a build that reaches for an
# engine this repository declares nowhere fails here for a reason no amount of
# careful phrasing in a build script can talk its way around. `build` also puts a
# recording stub for each of them on PATH -- including `mvn` when the delivered
# tree is Gradle -- so a submission that vendored one gets caught calling it rather
# than merely failing to find it.
#
# The JDK is the exact Temurin build the agent image ships (Temurin 17.0.15+6), and
# it must be: javac output is reproducible for a fixed compiler version and not
# across versions. A base image bump here would fail hundreds of checks on every
# submission, correct ones included, and the failure would read as the submission's
# fault. This is the same digest-pinned image stage 3 uses, which is also where the
# Maven comes from -- one JDK, and one Maven, across all three stages.
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
# OS tooling.
#
# `binutils` and `file` are here because a module that cannot identify a file it
# was handed reports a confusing failure; `git` because prepare_workspace reads
# the baseline commit to tell delivered files from build output. Notably absent:
# every build engine except the two this task is a migration between. The check
# at the bottom of this file enforces that.
# ---------------------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        file binutils \
        git \
        procps \
        python3 python3-pip \
        unzip zip xz-utils \
    && rm -rf /var/lib/apt/lists/* \
    && java -version && mvn -v | head -1 && python3 --version

# ---------------------------------------------------------------------------
# The harness. `swerefactor.pytest_module` is what turns each collected test into
# one check keyed by nodeid, and `swerefactor behavioural` is what runs suite.toml.
# ---------------------------------------------------------------------------
COPY --from=infra /opt/swerefactor /opt/swerefactor
ENV PYTHONPATH=/opt/swerefactor

# ---------------------------------------------------------------------------
# pytest, hash-pinned, at the same version stage 1 pins. Collection behaviour
# decides what a check id is, so the two stages agreeing on a runner is what makes
# a scan module and a behavioural module the same shape.
# ---------------------------------------------------------------------------
COPY requirements-target.txt /opt/requirements-target.txt
RUN pip3 install --break-system-packages --require-hashes --no-deps \
        -r /opt/requirements-target.txt \
    && python3 -m pytest --version

# ---------------------------------------------------------------------------
# Gradle 8.10.2 -- same version, same SHA-256, same layout as the agent image, and
# seeded into /opt/gradle/dist so a submission's own ./gradlew can resolve the
# distribution offline instead of trying to download it.
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
# The offline closure: the same pinned deps.txt the agent image uses, byte for
# byte, laid out in Maven's local repository format.
#
# fetch_deps.py rather than the agent image's fetch_deps.sh, because that script
# populates the repository by running `mvn dependency:get` and there is no Maven
# here to run. Every coordinate in the manifest is fully pinned
# (group:artifact:version:packaging[:classifier]), so writing the files out
# directly is equivalent to what Maven would have done -- and it does the one
# thing a literal fetch would get wrong: it also fetches each artifact's POM,
# which Maven implies and Gradle requires. Gradle refuses to resolve a module
# whose POM is absent, and a plugin marker is resolved from its POM alone.
#
# protoc is in this closure as com.google.protobuf:protoc:3.17.3:exe:linux-x86_64,
# so the proto project's codegen resolves an artifact rather than needing a
# compiler installed here.
# ---------------------------------------------------------------------------
COPY data/offline/deps.txt /opt/srb/deps.txt
COPY data/offline/fetch_deps.py /opt/srb/fetch_deps.py
RUN python3 /opt/srb/fetch_deps.py --manifest /opt/srb/deps.txt \
        --repo /root/.m2/repository \
    && rm -f /opt/srb/fetch_deps.py

# The Gradle half of the offline plumbing, identical to the file at the same path
# in the agent image: repositories resolve against the local Maven repository, and
# the plugin ids this build needs map to the vendored plugin modules because the
# Plugin Portal is unreachable. It is environment, not solution -- it declares no
# plugin, no task and no convention, only where artifacts come from -- and
# instruction.md documents that it is there.
COPY data/offline/srb-offline.gradle /root/.gradle/init.d/srb-offline.gradle

# The Maven half, the same copy the agent image and stage 3 ship: the local
# repository is the vendored closure and Central is mirrored to an unusable URL, so
# an accidental remote resolution fails loudly instead of hanging until the
# timeout. Only a Maven-dialect run reads it, and only through --settings.
COPY data/offline/settings-offline.xml /root/.m2/settings.xml

# ---------------------------------------------------------------------------
# The suite.
# ---------------------------------------------------------------------------
COPY . /tests/behavioural

# The ground truth has to be here, and it has to be the ten frozen files. Every
# parametrize list in the suite derives from them -- 282 entry names, 271 class
# files, 1,328 test cases, 21 manifest headers -- so a data/ that did not make it
# into the image collects almost nothing, and `grade_behavioural` reads a module
# with no checks as rate 0.0. That is indistinguishable from a submission that
# failed, which is why it is asserted here where it can only be an image bug.
RUN python3 -c "\
import json, pathlib, sys; \
data = pathlib.Path('/tests/behavioural/data'); \
want = {'build_contract', 'classes', 'jars', 'manifest', 'module_info', 'pom', \
        'sources', 'tests', 'version_probe'}; \
have = {p.stem for p in data.glob('*.json')}; \
missing = sorted(want - have); \
sys.exit(f'data/ is missing {missing}') if missing else None; \
[json.loads(p.read_text()) for p in sorted(data.glob('*.json'))]; \
print(f'ground truth ok: {len(have)} files, all parse')"

# ---------------------------------------------------------------------------
# The invariants this image exists to enforce.
#
# Two engines are required, and they are the two endpoints of the migration: the
# suite drives whichever one the delivered tree declares, so a missing one does not
# fail visibly -- it makes every build raise for one of the two trees, which reads
# as the tree's fault rather than the image's.
#
# Everything else is an engine neither endpoint declares, and removing them is what
# makes a build that reaches for one fail here for a reason no amount of careful
# phrasing in a build script can talk its way around. The wrappers are on the
# forbidden list too: `mvnw` and `gradlew` would try to fetch a distribution over a
# network that does not exist, and a build that needs one has made this stage's own
# invocation not the build.
#
# The stub layer inside the run is the other half, and it is sharper than absence:
# `build` puts a recording stub for every engine the delivered tree does not declare
# on PATH before it builds -- `mvn` included, when that tree is Gradle -- so a
# submission that vendored one gets caught calling it, with its argv and cwd.
# ---------------------------------------------------------------------------
RUN set -eu; \
    for required in mvn gradle java javac jar; do \
        if ! command -v "$required" >/dev/null 2>&1; then \
            echo "FATAL: the stage-2 image has no $required." >&2; \
            echo "" >&2; \
            echo "This stage builds whichever build system the delivered tree" >&2; \
            echo "declares, because its expectations were frozen from State A" >&2; \
            echo "and a stage that cannot build its own oracle cannot show" >&2; \
            echo "they are satisfiable. A missing engine here would fail all" >&2; \
            echo "2,678 artifact checks for one of the two trees." >&2; \
            exit 1; \
        fi; \
    done; \
    for forbidden in mvnd mvnw gradlew ant antRun ivy \
                     make gmake cmake ninja bazel buck sbt scons lein; do \
        if command -v "$forbidden" >/dev/null 2>&1; then \
            echo "FATAL: the stage-2 image ships $forbidden, which neither" >&2; \
            echo "endpoint of this migration declares. A submission that" >&2; \
            echo "needs it would build here, and the claim that the two" >&2; \
            echo "trees are the only things being compared would be" >&2; \
            echo "unenforced." >&2; \
            exit 1; \
        fi; \
    done; \
    for dir in /usr/share/ant /opt/ant; do \
        if [ -e "$dir" ]; then \
            echo "FATAL: the stage-2 image has an installation at $dir" >&2; \
            exit 1; \
        fi; \
    done; \
    stale=$(find /root/.m2/repository/com/google/code/gson \
                 -mindepth 2 -maxdepth 2 -type d -name 2.10.1 2>/dev/null || true); \
    if [ -n "$stale" ]; then \
        echo "FATAL: artifacts under migration are resolvable offline here, so a" >&2; \
        echo "build could resolve one instead of producing it:" >&2; \
        echo "$stale" >&2; \
        exit 1; \
    fi; \
    mvn -v | head -1; \
    gradle --version | grep -i '^Gradle'; \
    echo "stage 2: both endpoints, nothing else, and no prebuilt gson"

# Both engines have to work offline in this image, or every submission on that side
# would score zero for a reason that has nothing to do with the submission. The
# Gradle probe uses the two things most likely to be broken by the plumbing rather
# than by a build script: a plugin id that only resolves through the init script's
# mapping, and a dependency that only resolves if the closure carries its POM as
# well as its jar. The Maven probe pins every lifecycle plugin to the version the
# closure holds, because Maven 3.9.9's own defaults are not in it -- a closure is
# only a closure with respect to a stated set of versions.
#
# Deliberately not gson: gson is not in this image, and a smoke test that needed it
# would be a smoke test that shipped the thing under measurement.
RUN set -eu; \
    mkdir -p /tmp/probe && cd /tmp/probe; \
    printf "rootProject.name = 'p'\n" > settings.gradle; \
    printf "plugins { id 'java-library'; id 'biz.aQute.bnd.builder' }\n\
java { toolchain { languageVersion = JavaLanguageVersion.of(17) } }\n\
configurations { probe }\n\
dependencies { probe 'junit:junit:4.13.2' }\n\
tasks.register('r') { def f = configurations.probe; doLast { println \"ok \${f.files.size()}\" } }\n" \
      > build.gradle; \
    gradle --offline --no-daemon --console=plain r | tee /tmp/probe.log; \
    grep -q '^ok 2' /tmp/probe.log; \
    cd /; rm -rf /tmp/probe /tmp/probe.log /root/.gradle/caches /root/.gradle/daemon

RUN set -eu; \
    mkdir -p /tmp/mprobe/src/main/java/p && cd /tmp/mprobe; \
    printf 'package p;\npublic class C { public static void main(String[] a) {} }\n' \
      > src/main/java/p/C.java; \
    printf '<project xmlns="http://maven.apache.org/POM/4.0.0">\n\
  <modelVersion>4.0.0</modelVersion>\n\
  <groupId>srb</groupId><artifactId>probe</artifactId><version>1</version>\n\
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
</project>\n' > pom.xml; \
    mvn -B -o --settings /root/.m2/settings.xml clean package -DskipTests \
        > /tmp/mprobe.log 2>&1 || { tail -40 /tmp/mprobe.log >&2; \
        echo "FATAL: the Maven side cannot build offline in this image." >&2; \
        echo "State A would then produce no artefact at all, and the 2,678" >&2; \
        echo "checks frozen from it could not be shown satisfiable." >&2; \
        exit 1; }; \
    test -f /tmp/mprobe/target/probe-1.jar; \
    echo "maven: builds offline in this image"; \
    cd /; rm -rf /tmp/mprobe /tmp/mprobe.log

# ---------------------------------------------------------------------------
# Does the suite collect, and does it still derive from the ground truth?
#
# Collection executes every import and every parametrize list, which is where this
# suite breaks. The floor is 2,400 against an actual 2,678: high enough that a
# module collapsing to zero fails the build, low enough that adding or removing a
# handful of checks does not.
#
# The second phase is the one worth having. It reduces a copy of data/jars.json by
# seven entries and asserts the collected count falls by exactly seven -- so a
# module that stopped consulting the ground truth and hard-coded a plausible number
# of checks fails here rather than reporting confidently on names nobody froze.
# ---------------------------------------------------------------------------
RUN bash /tests/behavioural/lib/collect-check.sh 2400

# The compiled modules would otherwise ship as a layer that differs per build.
RUN find /tests/behavioural -name __pycache__ -prune -exec rm -rf {} + || true

# ---------------------------------------------------------------------------
# Run-time contract.
#
# The submission is mounted at /workspace/repo and State A at /opt/original. Both
# exist in the image so a misconfigured run fails against an empty directory rather
# than a confusing mkdir error mid-suite.
#
# GRADLE_OPTS and MAVEN_OPTS are set here for the container's own shell, not for the
# builds: the modules run every invocation through builder.clean_env(), which strips
# GRADLE_OPTS, MAVEN_OPTS, JAVA_TOOL_OPTIONS and CLASSPATH before starting. A build
# that only succeeds because an option leaked in from the environment is not the
# build being graded.
# ---------------------------------------------------------------------------
RUN mkdir -p /workspace/repo /opt/original /logs/verifier /tmp/srb-work

ENV SRB_REPO=/workspace/repo \
    SRB_ORIGINAL=/opt/original \
    SRB_SUITE_DIR=/tests/behavioural \
    SRB_STUB_DIR=/tmp/srb-stubs \
    SRB_RESULTS=/logs/verifier \
    SRB_MAVEN_SETTINGS=/root/.m2/settings.xml \
    GRADLE_USER_HOME=/root/.gradle \
    GRADLE_OPTS="-Dorg.gradle.daemon=false -Dorg.gradle.console=plain"

WORKDIR /tests/behavioural
