# =============================================================================
# SWERefactorBench — fw07-graphhopper-dropwizard-to-springboot
# Stage 2 image: the behavioural modules.
#
#     docker build -t swerefactor/fw07-behavioural:1 \
#         tasks/fw07-graphhopper-dropwizard-to-springboot/tests/behavioural
#
# A machine that can build GraphHopper twice, offline, and talk to both results.
#
# WHY THIS IMAGE KEEPS THE RETIRED STACK, WHICH IS THE OPPOSITE OF THE AGENT'S.
# The agent's environment has no dropwizard-core jar: that omission is how the
# retirement is enforced, and it is why an untouched tree does not compile while
# the work is being done.  This image keeps the complete closure, Dropwizard and
# Jersey and HK2 included, because this stage's reference IS State A — its whole
# web layer is written against that stack, and it is compiled here at grade time.
# A stage that cannot build its own oracle cannot show its expectations are
# reachable, so the closure here is the union of both sides' needs.
#
# The consequence is stated plainly in evaluation.toml and worth repeating where
# someone reading the Dockerfile will meet it: a submission that changed nothing
# builds and passes in this image.  That is correct for a stage that only asks
# about behaviour.  Stage 1 asks whether the rewrite happened, its gates are
# required, and a failure there is the whole score.
#
# Base image pinned to the same digest the agent environment uses:
# maven:3.9.11-eclipse-temurin-17 -> Debian bookworm, Maven 3.9.11, Temurin JDK
# 17.0.17.  It has to be the same build.  This stage is a differential between two
# jars compiled here, and "the two behave the same" is only a measurement about
# the repositories if the compiler and the runtime are held fixed.
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
# OS tooling.  Installed while the image build still has network; the grading
# container has none.
#
# `procps` is not optional here: the build module launches two JVMs per profile
# and reaps them by process group, and a reap that cannot enumerate processes
# leaves a server holding port 18989 for the next launch to fail on.  `python3` is
# the modules' language; `file` and `binutils` are so a module that cannot launch
# a jar can say what it was handed instead of guessing.
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
# The harness.  `swerefactor.pytest_module` is the plugin that turns each collected
# test into one check keyed by nodeid, and `swerefactor behavioural` is what reads
# suite.toml and runs the eleven modules in order.  Both come from the infra
# image so every stage in the benchmark agrees on what a check is.
# ---------------------------------------------------------------------------
COPY --from=infra /opt/swerefactor /opt/swerefactor
ENV PYTHONPATH=/opt/swerefactor

# ---------------------------------------------------------------------------
# pytest, hash-pinned.  Seven of the eleven modules are pytest files; the other
# four are plain Python and need nothing installed.
#
# `--break-system-packages` because Debian bookworm marks its Python
# externally-managed, and `--no-deps` because the requirements file is already a
# closure — pip resolving anything here would be pip reaching the network.
# ---------------------------------------------------------------------------
COPY requirements-runner.txt /opt/requirements-runner.txt
RUN pip3 install --break-system-packages --require-hashes --no-deps \
        -r /opt/requirements-runner.txt \
    && python3 -m pytest --version

# ---------------------------------------------------------------------------
# data/ first, and alone, because everything below it is expensive.
#
# The warm is ~20 minutes of Maven.  Copying the whole context here instead would
# put the modules' source in the same layer, so editing one line of a test would
# rebuild the closure.  data/ changes when the snapshot or the dependency
# declaration changes, which is rarely and deliberately.
# ---------------------------------------------------------------------------
COPY data /tests/behavioural/data

# ---------------------------------------------------------------------------
# Offline closure, part 1 of 2: everything State A resolves.
#
# Warmed by BUILDING State A rather than from a coordinate list, for the reason
# the environment image gives: a build is exact by construction — it warms the
# plugins, the plugins' own dependencies and the surefire provider, which are the
# parts a hand-written list always misses.
#
# The mtime re-stamp is load-bearing and the failure it prevents is silent.  Every
# entry in the snapshot carries mtime 0 so the archive bytes depend only on
# content; maven-resources-plugin then copies unfiltered resources incrementally,
# asking whether source.lastModified() > dest.lastModified(), and a destination
# that does not exist yet answers 0.  With the source also at 0 the comparison is
# false, so the plugin creates the directories, logs "Copying 76 resources", and
# writes none of them.  Core's 49 translation files and web-bundle's test
# config.yml vanish, and the symptom is a German route answered in English.  The
# build module applies the same stamp to the tree it unpacks at grade time.
#
# Three passes, each warming what the previous does not:
#   install -DskipTests   the compile closure, every module's jar, the plugins,
#                         and — through frontend-maven-plugin's `npm pack` — the
#                         map UI tarball into /root/.npm/_cacache
#   test-compile          every module's test-scope jars, whole tree, because
#                         `own_tests` runs `mvn test` over the whole tree and a
#                         module whose test scope was never warmed fails there at
#                         resolution
#   test on the web tier  the surefire provider.  Surefire is pinned to 2.22.2 in
#                         the root pom's pluginManagement and JUnit Jupiter is a
#                         root-level test dependency, so one provider serves all
#                         ten modules and warming it once covers the whole tree.
#                         It is also a proof that State A's web layer is green
#                         before anything is measured against it.
# ---------------------------------------------------------------------------
RUN cd /tests/behavioural/data && sha256sum -c original.sha256 \
    && mkdir -p /tmp/warm \
    && tar -xzf original.tar.gz -C /tmp/warm --strip-components=1 \
    && find /tmp/warm -exec touch -d '2024-01-01T00:00:00Z' {} + \
    && cd /tmp/warm \
    && mvn -B -DskipTests install \
    && mvn -B -DskipTests test-compile \
    && mvn -B -pl navigation,web-bundle,web test \
    && cd / && rm -rf /tmp/warm

# The repository under test must not be resolvable as a binary.  The warm above
# installed the reactor's own modules into the local repository; left there, a
# submission that deleted a Maven module would resolve State A's jar for it and
# appear to have kept the behaviour.
#
# Exactly the eleven reactor artifacts, named rather than removed by group prefix:
# `com.graphhopper.external:jackson-datatype-jts` is an upstream fork published
# under the project's own group and `graphhopper-web-api` depends on it, so
# deleting the group wholesale takes it too and breaks the reference build with a
# message about something no submission touched.
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
# State A's build cannot warm this half — Spring Boot appears nowhere in it — and
# this stage has to compile submissions written against it.  data/offline-deps is
# the same pom the agent environment warms from, byte for byte, which is what
# makes "resolvable here" and "resolvable there" the same claim; `swerefactor
# validate` enforces the identity.  A stage resolving from a different closure
# than the agent had fails honest submissions for reasons they cannot see.
# ---------------------------------------------------------------------------
RUN mkdir -p /tmp/offline-deps/src/main/java/local/swerefactor \
    && cp /tests/behavioural/data/offline-deps/pom.xml /tmp/offline-deps/pom.xml \
    && cp /tests/behavioural/data/offline-deps/Warm.java \
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
# The settings file is installed TWICE on purpose, at the two paths two different
# callers look for:
#   /root/.m2/settings.xml           the default, so the proof below and any bare
#                                    `mvn` in this image are offline without a flag
#   /opt/srb/settings-offline.xml    what suite.toml's SRB_OFFLINE_SETTINGS names
#                                    and what harness/maven.py passes with
#                                    `--settings`.  That flag is not a
#                                    convenience: Maven records per artifact which
#                                    repository id it came from and refuses,
#                                    offline, to use one whose provenance names a
#                                    repository it is not configured with now.
#                                    Both copies are the same file, so the two
#                                    callers cannot drift apart.
#
# npmrc is here because State A does not keep its map UI in the tree — it runs
# `npm pack @graphhopper/graphhopper-maps-bundle` through frontend-maven-plugin
# and untars the result into web-bundle's classes, from where /maps/ is served.
# The warm above already paid for that pack with network; this file makes every
# later pack cache-only.  Without it the reference build dies at EAI_AGAIN on a
# step no submission wrote, and the `assets` module's four cases fail on both
# sides for a reason that is the image's fault.
# ---------------------------------------------------------------------------
COPY data/settings-offline.xml /root/.m2/settings.xml
COPY data/settings-offline.xml /opt/srb/settings-offline.xml
COPY data/npmrc.offline /root/.npmrc

# Maven refuses, offline, to use an artifact whose recorded provenance names a
# repository it is not configured with.  The mirror keeps the id `central` so the
# warm's own records and its range metadata still match — State A's web-bundle
# resolves several org.webjars.npm dependencies through version ranges, and those
# read `maven-metadata-central.xml` — but a few artifacts came through State A's
# `github` repository declaration and would look un-downloaded.  Deleting these
# records turns the closure into what it now is: locally installed, always
# available, never re-fetched.
RUN find /root/.m2/repository -type f \
         \( -name '_remote.repositories' -o -name '*.lastUpdated' \) -delete \
    && test -z "$(find /root/.m2/repository -name '_remote.repositories' -print -quit)"

# ---------------------------------------------------------------------------
# The proof: the two commands this stage runs at grade time, run here, offline,
# from a clean unpack of the same tarball the build module unpacks.
#
# This is the check most worth having in this image.  Every piece it exercises was
# warmed by a layer above, and a warm that missed one produces a stage where the
# reference does not build — at which point `build` exits 70 and NO submission is
# judged at all.  Discovering that here costs an image build; discovering it at
# grade time costs a round for every submission in it.
#
# The commands are copied from the code, not written to resemble it:
#   harness/maven.py    mvn -B -ntp --settings <s> -o -DskipTests package
#   modules/own_tests   mvn -B -ntp --settings <s> -o test
# with the same MAVEN_OPTS the harness sets, because a heap that only worked at
# image-build time is not a proof about grade time.
#
# The test phase asserts that tests RAN, not that all of them passed.  That is
# what `own_tests` actually requires: it exits 70 only when the reference reports
# no tests at all, and it compares the submission's passing count against the
# reference's count measured in that same run — so an upstream flake lowers the
# bar rather than breaking the module.  Asserting green here would make the image
# unbuildable over a test that has nothing to do with the migration.
#
# Written as a script with explicit reporting rather than a chain of `&&`: the
# point of this layer is to characterise a failure, and a chain that redirects
# Maven to a log and then trips an assertion prints nothing but "exit code: 1".
# ---------------------------------------------------------------------------
RUN set -u; \
    S=/opt/srb/settings-offline.xml; \
    export MAVEN_OPTS="-Dmaven.repo.local=/root/.m2/repository -Xmx2g -Djava.awt.headless=true"; \
    mkdir -p /tmp/proof; \
    tar -xzf /tests/behavioural/data/original.tar.gz -C /tmp/proof --strip-components=1; \
    find /tmp/proof -exec touch -d '2024-01-01T00:00:00Z' {} +; \
    cd /tmp/proof; \
    fail=0; \
    if ! mvn -B -ntp --settings "$S" -o -DskipTests package > /tmp/pkg.log 2>&1; then \
        echo "FATAL: the reference does not build offline in this image." >&2; \
        echo "       At grade time that makes the build module exit 70 and no" >&2; \
        echo "       submission gets judged.  A layer above missed something." >&2; \
        grep -nE 'ERROR|BUILD FAILURE|Could not resolve|Non-resolvable|cannot find symbol' \
            /tmp/pkg.log | head -40 >&2; \
        exit 1; \
    fi; \
    test -s web/target/graphhopper-web-11.0-SNAPSHOT.jar || { \
        echo "FATAL: the package succeeded but produced no web jar." >&2; fail=1; }; \
    test -s web-bundle/target/classes/com/graphhopper/maps/index.html || { \
        echo "FATAL: the map UI is absent from web-bundle's classes.  The npm" >&2; \
        echo "       cache or /root/.npmrc is not doing its job, and the assets" >&2; \
        echo "       module's /maps/ cases would fail on BOTH sides." >&2; fail=1; }; \
    src_tr=$(find core/src/main/resources/com/graphhopper/util -name '*.txt' -type f 2>/dev/null | wc -l); \
    out_tr=$(find core/target/classes/com/graphhopper/util -name '*.txt' -type f 2>/dev/null | wc -l); \
    if [ "$src_tr" -eq 0 ]; then \
        echo "FATAL: core ships no translation files under" >&2; \
        echo "       core/src/main/resources/com/graphhopper/util/*.txt, so the" >&2; \
        echo "       copy check below compares nothing against nothing.  They have" >&2; \
        echo "       moved; point this check wherever they went." >&2; fail=1; \
    elif [ "$out_tr" -ne "$src_tr" ]; then \
        echo "FATAL: core carries $src_tr translations but $out_tr reached" >&2; \
        echo "       target/classes.  That is the mtime-0 incremental-copy failure" >&2; \
        echo "       the re-stamp above exists to prevent: maven-resources-plugin" >&2; \
        echo "       skips a copy whose source is not newer than its target.  Routes" >&2; \
        echo "       would answer in the wrong language and nothing would say why." >&2; fail=1; \
    fi; \
    if ! mvn -B -ntp --settings "$S" -o test > /tmp/test.log 2>&1; then \
        echo "note: the reference's own suite is not green offline here; that is" >&2; \
        echo "      tolerated — own_tests floors the bar at the reference's own" >&2; \
        echo "      measured count — but it must still RUN." >&2; \
    fi; \
    ran=$(grep -oE 'Tests run: [0-9]+' /tmp/test.log | awk '{s+=$3} END {print s+0}'); \
    if [ "$ran" -lt 100 ]; then \
        echo "FATAL: \`mvn -o test\` over the whole tree ran only $ran test(s)." >&2; \
        echo "       own_tests needs this to execute offline for every module;" >&2; \
        echo "       a test scope that was never warmed fails at resolution and" >&2; \
        echo "       the module exits 70 for every submission." >&2; \
        grep -nE 'ERROR|Could not resolve|No tests to run|BUILD FAILURE' \
            /tmp/test.log | head -30 >&2; \
        fail=1; \
    fi; \
    if [ "$fail" -ne 0 ]; then exit 1; fi; \
    echo "proof: the reference packages offline, serves a built map UI, copies all"; \
    echo "       $src_tr of core's translations into target/classes, and runs $ran of"; \
    echo "       its own tests offline"; \
    cd /; rm -rf /tmp/proof /tmp/pkg.log /tmp/test.log

# ---------------------------------------------------------------------------
# The suite.  Last, so that editing a module does not rebuild the closure above.
# ---------------------------------------------------------------------------
COPY . /tests/behavioural

# ---------------------------------------------------------------------------
# Does the suite collect, and does it still derive from the corpus?
#
# Collection executes every import and every parametrize list, which is where a
# suite like this breaks: a fixture renamed in lib/ takes seven modules down at
# once, and the runner reads a module that collected nothing as rate 0.0 —
# indistinguishable from a submission that failed every check.
#
# The expected number is COMPUTED, not written down: one check per corpus case
# plus one coverage guard per pytest module.  A literal would be wrong the day a
# case is added, and the wrongness would look like a broken submission.  Computing
# it also keeps the assertion sharp in the direction that matters — a module that
# stopped consulting the corpus and hard-coded a plausible list of cases collects
# a count that no longer matches, and fails here.
# ---------------------------------------------------------------------------
# PP names both roots. `srbfixtures` imports swerefactor for the weight markers, so
# a PYTHONPATH of lib/ alone REPLACES the /opt/swerefactor this image sets globally
# and the plugin fails to import -- at which point pytest collects nothing, this
# gate reads 0, and the FATAL blames the corpus for what is a path bug two lines
# above it.  The grade-time invocation in run.py names both; so does the check
# below this one.  This is the same list.
RUN set -eu; \
    cd /tests/behavioural; \
    PP=/tests/behavioural/lib:/opt/swerefactor; \
    expected=$(PYTHONPATH="$PP" python3 -c "\
import pathlib; from harness import corpus; \
guards = len(list(pathlib.Path('modules').glob('*/test_*.py'))); \
print(len(corpus.all_cases()) + guards)"); \
    got=$(PYTHONPATH="$PP" python3 -m pytest \
            -c /tests/behavioural/pytest.ini -p srbfixtures \
            --collect-only -q /tests/behavioural/modules 2>/dev/null \
          | python3 /tests/behavioural/lib/count-collected.py); \
    echo "collected $got check(s), expected $expected"; \
    if [ "$got" -ne "$expected" ]; then \
        echo "FATAL: the suite collects $got checks and the corpus implies" >&2; \
        echo "       $expected.  Either a module no longer derives its cases from" >&2; \
        echo "       the corpus, or an import broke and a whole module collected" >&2; \
        echo "       nothing — which the runner would score as 0.0 and read as" >&2; \
        echo "       the submission's fault." >&2; \
        PYTHONPATH="$PP" python3 -m pytest \
            -c /tests/behavioural/pytest.ini -p srbfixtures \
            --collect-only -q /tests/behavioural/modules 2>&1 | tail -30 >&2; \
        exit 1; \
    fi

# Every module declared in suite.toml must have something to run, and the data the
# corpus is built from must be present.  Both are image bugs when they are wrong,
# and both are silent at grade time: a missing module directory is a zero, and a
# missing request body is a case that fails on both sides.
RUN set -eu; \
    PYTHONPATH=/tests/behavioural/lib:/opt/swerefactor python3 -c "\
import pathlib, sys; \
from harness import corpus; \
from swerefactor import tomlcompat; \
suite = pathlib.Path('/tests/behavioural'); \
declared = [m['id'] for m in tomlcompat.load(suite/'suite.toml').get('module') or []]; \
sys.exit('suite.toml declares no modules -- reading the wrong key') if not declared else None; \
missing = [m for m in declared \
           if not any((suite/'modules'/m/n).exists() \
                      for n in ('run.sh','run.py'))]; \
sys.exit('modules with no entry point: %s' % missing) if missing else None; \
stray = sorted(d.name for d in (suite/'modules').iterdir() \
               if d.is_dir() and d.name != '__pycache__' and d.name not in declared); \
sys.exit('module directories suite.toml does not declare: %s' % stray) if stray else None; \
bodies = {c['body'][1:] for c in corpus.all_cases() \
          if isinstance(c.get('body'), str) and c['body'].startswith('@')}; \
absent = sorted(b for b in bodies if not (suite/'data'/'bodies'/b).exists()); \
sys.exit('corpus names body files that are not in data/bodies: %s' % absent) \
    if absent else None; \
print('%d modules have entry points, none undeclared; %d corpus case(s), all body files present' \
      % (len(declared), len(corpus.all_cases())))"

# Compiled modules would otherwise ship as a layer that differs per build, and a
# .pyc from a local run is a file whose provenance nobody can state.
RUN find /tests/behavioural -name __pycache__ -prune -exec rm -rf {} + || true

# ---------------------------------------------------------------------------
# Run-time contract.
#
# The submission is mounted at /workspace/repo.  /opt/original is deliberately NOT
# used by this stage — the build module unpacks State A from data/original.tar.gz
# into the suite's own work directory, so the result does not depend on whether a
# runner happened to mount the original tree.
#
# MAVEN_OPTS is set for this container's own shell only.  Every graded Maven
# invocation composes its own environment in harness/maven.py, which drops
# JAVA_TOOL_OPTIONS and MAVEN_ARGS: a submitted tree that set either would
# otherwise inject flags into a command this harness composed deliberately.
# ---------------------------------------------------------------------------
RUN mkdir -p /workspace/repo /logs/verifier /tmp/srb-fw07-suite

ENV SRB_REPO=/workspace/repo \
    SRB_SUITE_DIR=/tests/behavioural \
    SRB_SUITE_WORK=/tmp/srb-fw07-suite \
    SRB_RESULTS=/logs/verifier \
    SRB_M2_REPO=/root/.m2/repository \
    SRB_OFFLINE_SETTINGS=/opt/srb/settings-offline.xml

WORKDIR /tests/behavioural
