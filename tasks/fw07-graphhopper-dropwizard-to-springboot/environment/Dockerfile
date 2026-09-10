# SWERefactorBench — framework / GraphHopper: Dropwizard 4 -> Spring Boot 3
#
# Agent environment (State A).  GraphHopper 11.0 with its whole web layer built
# on Dropwizard: Jersey resources, an HK2 object graph, a YAML-configured
# Application/Configuration pair, codahale metrics on a second connector, an
# argparse4j command line, and an AssetsBundle serving the bundled map UI.  The
# job is to put the same HTTP and CLI surface on Spring Boot instead.
#
# WHAT THIS IMAGE DELIBERATELY DOES NOT CONTAIN.  No grader, no gate list, no
# test suite of the benchmark's own, and no self-check.  An environment that
# ships the checklist it will be graded against teaches the checklist.  What the
# agent gets instead is State A itself, buildable and runnable offline: it can
# start the Dropwizard server, curl it, read the responses, and compare its own
# work against that — which is the same oracle the behavioural stage uses, and no
# more.
#
# THE OFFLINE CLOSURE IS THE BAN.  A local Maven repository with no
# dropwizard-core jar is not an opinion a submission can argue with, and there
# is no network to fall back on.  So the retirement is enforced here, by
# omission, and the grading stages do not have to repeat it — see prune-m2.sh
# and retired-stack.txt for the mechanism and for the one asymmetry in it (the
# jars go, the poms stay, because State A's root pom imports
# dropwizard-dependencies as a BOM and a BOM with no pom stops Maven before it
# can load the reactor at all).
#
# Base image pinned by digest: maven:3.9.11-eclipse-temurin-17
# -> Debian bookworm, Maven 3.9.11, Eclipse Temurin JDK 17.  GraphHopper 11
# targets 17 and Spring Boot 3.5 requires 17 or newer, so one JDK serves both
# sides of the migration.  Every stage image starts from this same digest, which
# is what makes "the two jars behave the same" a measurement rather than a
# coincidence about compilers.
FROM maven:3.9.11-eclipse-temurin-17@sha256:e4a7ace3dc0d645ed97f8d9ad0b0d3f0b14fa8d150138f27f116d7105a639b82

# NPM_CONFIG_USERCONFIG and NPM_CONFIG_CACHE are here for the same reason
# MAVEN_OPTS carries -Dmaven.repo.local, and they silently zeroed this task for
# every harness that runs the agent as a non-root uid.  npm reads its user config
# from $HOME/.npmrc and caches in $HOME/.npm, both HOME-relative; the offline
# plumbing this image ships -- `offline=true` and a _cacache holding the map
# bundle's packument and tarball -- lives under /root and is therefore invisible
# to any other uid.  What follows is not that the frontend build is slower: it
# reaches for the registry, there is no egress, and it dies at EAI_AGAIN inside
# `npm pack` -- a step the submission did not write and cannot fix, in a module
# it is being graded on.  npmrc.offline's own header says that is what it exists
# to prevent.  These two variables are the HOME-independent form of the same
# thing, they name the paths root already resolved to, and npm honours them for
# any uid (measured against the vendored node 20.14.0 / npm 10.7.0, not assumed).
ENV DEBIAN_FRONTEND=noninteractive \
    LC_ALL=C.UTF-8 \
    LANG=C.UTF-8 \
    TZ=UTC \
    SOURCE_DATE_EPOCH=1705276800 \
    MAVEN_OPTS="-Dmaven.repo.local=/root/.m2/repository" \
    NPM_CONFIG_USERCONFIG=/root/.npmrc \
    NPM_CONFIG_CACHE=/root/.npm
# Offline is not set as MAVEN_ARGS here: the layers below still have network and
# have to use it to warm the closure.  It arrives with settings-offline.xml,
# copied in after the last fetching layer, so `mvn` is offline for the agent
# without every command needing a flag.

# ---------------------------------------------------------------------------
# OS tooling.  Installed while the image build still has network; the trial
# container has none.  curl and jq are here because the intended workflow is to
# run State A and talk to it.
# ---------------------------------------------------------------------------
RUN apt-get -o Acquire::Retries=8 update \
    && apt-get -o Acquire::Retries=8 install -y --no-install-recommends \
      git unzip zip xz-utils \
      python3 \
      curl jq xmlstarlet \
      file binutils \
      less procps vim-tiny ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && java -version && mvn -v | head -1

# ---------------------------------------------------------------------------
# Repository snapshot.  Verified against environment/original.sha256, then
# unpacked to /workspace/repo.  The tarball carries no VCS metadata, no build
# output, and no .mvn wrapper directory — Maven comes from the image.
#
# It is upstream tag 11.0 with exactly one line removed: client-hc/pom.xml
# declared a test-scoped dependency on dropwizard-testing that nothing in
# client-hc/src references — grep finds zero mentions of dropwizard anywhere in
# that module, and its 24 tests pass without it.  Left in, it would make the
# retirement below break a module that has nothing to do with the migration:
# `-am` pulls client-hc into any whole-repo build, its test scope cannot resolve
# against a pruned repository, and the reactor then dies on client-hc before the
# web layer is ever compiled.  The agent's first command would fail naming a
# module it was never asked to touch.
#
# That is the only deviation.  Nothing in the migration surface — no web module,
# no resource, no configuration — differs from the released tag.
#
# Every entry in the tarball carries mtime 0, because the snapshot builder fixes
# timestamps so the archive bytes depend only on content.  That is right for the
# archive and wrong for a working tree: maven-resources-plugin copies unfiltered
# resources incrementally, comparing source.lastModified() against the
# destination's, and a destination that does not exist yet reports 0.  With a
# source also at 0 the comparison 0 > 0 is false, so the plugin creates the
# destination directories, logs "Copying 76 resources", and writes nothing.  The
# effect is invisible until something reads a resource at runtime — core's 49
# translation files and web-bundle's test config.yml both vanish this way.
# Filtered resources are unaffected, since interpolation forces a rewrite.
#
# So: give the tree one uniform, non-zero mtime.  Any fixed date works; this one
# is arbitrary and stable, which keeps the layer reproducible.
# ---------------------------------------------------------------------------
COPY original.tar.gz /tmp/original.tar.gz
COPY original.sha256 /tmp/original.sha256
RUN cd /tmp && sha256sum -c original.sha256 \
    && mkdir -p /workspace/repo \
    && tar -xzf /tmp/original.tar.gz -C /workspace/repo --strip-components=1 \
    && find /workspace/repo -exec touch -d '2024-01-01T00:00:00Z' {} + \
    && rm -f /tmp/original.tar.gz /tmp/original.sha256 \
    && test -f /workspace/repo/pom.xml \
    && test -f /workspace/repo/web/pom.xml \
    && test -f /workspace/repo/web-bundle/pom.xml \
    && test -f /workspace/repo/navigation/pom.xml \
    && test -f /workspace/repo/config-example.yml \
    && test -d /workspace/repo/web-bundle/src/main/resources/com/graphhopper/maps \
    && test ! -e /workspace/repo/.git \
    && test -z "$(find /workspace/repo -name '.git*' -print -quit)" \
    && test -z "$(find /workspace/repo -name target -type d -print -quit)"

# ---------------------------------------------------------------------------
# Offline closure, part 1 of 2: everything State A resolves.
#
# Built by building State A rather than from a list of coordinates, because a
# list is only ever as exact as the person who wrote it, while a build is exact
# by construction — it warms the plugins, the plugin's own dependencies, and the
# surefire provider, which are the parts a hand-written list always misses.
#
# Three passes, each warming something the previous one does not:
#   install -DskipTests  the compile closure, every module's jar, the plugins
#   test-compile         every module's test-scope jars
#   test on the web tier the surefire provider, and a proof that State A's web
#                        layer — the layer being migrated — is green to begin
#                        with.  211 tests, ~90s.  If this fails the image build
#                        fails, which is correct: an environment whose State A
#                        does not work is not an environment.
#
# Runs on a scratch copy.  /workspace/repo stays pristine, so the agent starts
# from a tree with no build products and a clean `git status`.
# ---------------------------------------------------------------------------
RUN cp -a /workspace/repo /tmp/warm \
    && cd /tmp/warm \
    && mvn -B -DskipTests install \
    && mvn -B -DskipTests test-compile \
    && mvn -B -pl navigation,web-bundle,web test \
    && cd / \
    && rm -rf /tmp/warm \
    # Reachable by whichever uid the agent phase runs as.  A harness that runs
    # the agent as root needs none of this; one that drops to an unprivileged uid
    # -- which Claude Code forces, since it refuses every bypass-permissions mode
    # under root -- cannot even traverse /root at the base image's 0700, so the
    # entire offline closure this layer just warmed is invisible and the task
    # becomes unattemptable rather than hard.  Nothing under /root here is
    # private: a local Maven repository, an npm cache, and two offline config
    # files, all of which instruction.md already documents to the agent.
    #
    # Directories get +w, files keep their modes.  The write is needed because
    # both tools create paths inside their caches even on a pure read -- Maven a
    # resolver-status marker, npm a _logs entry and a content-address it did not
    # have -- and a new file only needs its parent writable.  Done here, in the
    # layer that created the files, so it costs no copy-up; the same chmod in a
    # later layer would duplicate all 434 MB of them.
    && chmod 0755 /root \
    && find /root/.m2 /root/.npm -type d -exec chmod a+rwX {} +

# The repository under test must not be resolvable as a binary.  The warm above
# installed the reactor's own modules into the local repository; leaving them
# there would let a submission put State A's jars on its classpath and call the
# migration done.
#
# Exactly the eleven reactor artifacts, named rather than removed by group
# prefix: `com.graphhopper.external:jackson-datatype-jts` is an upstream fork
# published under the project's own group, `graphhopper-web-api` depends on it,
# and deleting the group wholesale takes it too -- which breaks State A's build
# with a message that has nothing to do with anything the agent did.
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
# State A's build cannot warm this half — Spring Boot appears nowhere in it —
# so offline-deps/pom.xml stands in for the build that does not exist yet.  It
# names the Spring Boot starters the graded surface needs, all three embedded
# containers so the choice of container stays the submission's, and the Jackson
# and metrics modules State A's responses depend on.  See its header for what is
# deliberately absent and why.
#
# `go-offline` fetches dependencies and plugins; `javac` then proves the jars are
# not merely present but usable, which is a different claim and the one that
# matters.  Warm.java compiles against the pieces a rebuilt surface actually
# touches: a Boot application, a REST controller, an actuator HealthIndicator, an
# XmlMapper, and a Micrometer registry.
# ---------------------------------------------------------------------------
COPY offline-deps/pom.xml /tmp/offline-deps/pom.xml
COPY offline-deps/Warm.java /tmp/offline-deps/src/main/java/local/swerefactor/Warm.java
RUN cd /tmp/offline-deps \
    && mvn -B -DskipTests dependency:go-offline \
    && mvn -B -DskipTests compile \
    && test -f target/classes/local/swerefactor/Warm.class \
    && cd / && rm -rf /tmp/offline-deps \
    # The State B half of the closure, chmodded in its own layer for the same
    # reason the State A half was: this is where these directories exist for the
    # first time, so it is the only place the mode change is free.  `! -perm -o+w`
    # walks the State A half without touching it, so the layer above is not
    # copied up a second time.  Deliberately not `-prune`d on the already-writable
    # ones: a Spring Boot artifact lands INSIDE a group directory the warm
    # created, so pruning at the parent would skip exactly the new directories
    # this is here to fix.
    && find /root/.m2 -type d ! -perm -o+w -exec chmod a+rwX {} +

# ---------------------------------------------------------------------------
# Maven's and npm's side of the offline plumbing.  The last fetching layer is
# above; from here nothing reaches the network.  Environment infrastructure,
# exactly like a corporate settings file; instruction.md documents that both
# files are there.
#
# npm is in this picture at all because State A builds its map UI rather than
# checking it in -- see npmrc.offline, which explains the three-step frontend
# build and which of its steps the warm above has already paid for.
# ---------------------------------------------------------------------------
COPY settings-offline.xml /root/.m2/settings.xml
COPY npmrc.offline /root/.npmrc
# The Maven file installed a second time as the *global* one, because
# `<offline>true</offline>` and the mirror-to-an-unusable-URL trap are the two
# things in it that a user-level file cannot deliver to a non-root uid: Maven
# reads the user file from $HOME/.m2/settings.xml, so an agent at any other uid
# gets neither, and an accidental remote resolution then hangs to its connect
# timeout instead of failing in one line -- which is the difference the file's own
# header says it exists to make.  MAVEN_ARGS="-o" in task.toml covers the offline
# flag alone, and only for as long as nothing overrides it; the mirror and
# `<localRepository>` are not covered by it at all.  Both copies are the same
# bytes, which is what `swerefactor validate` compares.
#
# The npmrc gets an explicit mode rather than inheriting COPY's: it arrives 0664
# from the build context, and the whole point of NPM_CONFIG_USERCONFIG naming it
# is that another uid can read it.
RUN cp /root/.m2/settings.xml "${MAVEN_HOME:-/usr/share/maven}/conf/settings.xml" \
    && chmod 0644 /root/.m2/settings.xml \
                  "${MAVEN_HOME:-/usr/share/maven}/conf/settings.xml" \
                  /root/.npmrc \
    && cmp /root/.m2/settings.xml \
           "${MAVEN_HOME:-/usr/share/maven}/conf/settings.xml"

# Maven records, per artifact, the id of the repository it came from, and refuses
# to use an artifact offline unless that id is one it is configured with now.  The
# settings above keep the id `central` so the warm's own records and its range
# metadata still match -- but a few artifacts were resolved through State A's
# `github` repository declaration, and those would look un-downloaded.  Deleting
# these records turns the whole closure into what it now is: a set of locally
# installed artifacts, always available, never re-fetched.
RUN find /root/.m2/repository -type f \
         \( -name '_remote.repositories' -o -name '*.lastUpdated' \) -delete \
    && test -z "$(find /root/.m2/repository -name '_remote.repositories' -print -quit)"

# The offline story proved rather than assumed, and proved BEFORE anything is
# taken away: State A builds from a clean tree with no network, and the map UI it
# serves at /maps/ is produced by the frontend build out of the npm cache.
# `verify` rather than `install`, so the proof leaves nothing of the repository
# under test behind in the local repository.
#
# This is the check most worth having in this image.  Every piece it exercises was
# warmed by a layer above; a warm that missed one produces an environment whose
# State A the agent cannot build, and that would surface mid-trial as an error
# about something the submission never touched.
#
# The jar this proof produces is also kept, and that is the other half of this
# layer's job -- see the oracle layer below for why it has to happen here.
# It runs as the UNPRIVILEGED uid, and that is the load-bearing detail rather
# than a hardening gesture.  Root cannot fail this proof: it ignores permission
# bits entirely, and its HOME is /root, so the offline Maven settings and the npm
# cache are found by coincidence.  Both stop being true the moment the agent phase
# runs as anyone else -- which is not hypothetical but the default for one of the
# two harnesses that drive this benchmark, because Claude Code refuses every
# bypass-permissions mode under root and its loop therefore drops to a fixed
# unprivileged uid.  Proving State A builds only as root proves it for one of the
# two, and this image shipped for the other with a frontend step that dies at
# EAI_AGAIN in a module the submission is graded on.
#
# So the proof is taken at that uid instead of at root, with a HOME that is
# deliberately NOT /root.  The build is byte-identical either way -- SOURCE_DATE_EPOCH
# is set, the closure is the same, and the jar it produces is the oracle -- so this
# costs nothing and makes the environment unable to ship in a state where the
# unprivileged path does not work.  `setpriv` rather than su/runuser: those want a
# login shell for the uid, and setpriv is what the harness loop itself uses.
ARG SRB_AGENT_UID=1000
RUN cp -a /workspace/repo /tmp/proof \
    && chown -R "${SRB_AGENT_UID}:${SRB_AGENT_UID}" /tmp/proof \
    && mkdir -p /tmp/proof-home && chmod 0777 /tmp/proof-home \
    && setpriv --reuid="${SRB_AGENT_UID}" --regid="${SRB_AGENT_UID}" \
         --clear-groups --inh-caps=-all \
         env HOME=/tmp/proof-home PATH="$PATH" \
             MAVEN_OPTS="$MAVEN_OPTS" \
             NPM_CONFIG_USERCONFIG="$NPM_CONFIG_USERCONFIG" \
             NPM_CONFIG_CACHE="$NPM_CONFIG_CACHE" \
             SOURCE_DATE_EPOCH="$SOURCE_DATE_EPOCH" \
             LC_ALL=C.UTF-8 LANG=C.UTF-8 TZ=UTC \
         sh -c 'cd /tmp/proof && mvn -B -o -DskipTests -pl web,navigation -am verify' \
    && cd /tmp/proof \
    # The frontend output, which is the assertion that actually covers npm: it
    # exists only if `npm pack` resolved the map bundle from the cache under
    # /root/.npm, offline, at a uid whose HOME is elsewhere.
    && test -s web-bundle/target/classes/com/graphhopper/maps/index.html \
    && test -s web/target/graphhopper-web-11.0-SNAPSHOT.jar \
    && mkdir -p /opt/oracle \
    && cp web/target/graphhopper-web-11.0-SNAPSHOT.jar /opt/oracle/state-a.jar \
    && chown root:root /opt/oracle/state-a.jar \
    && chmod 0644 /opt/oracle/state-a.jar \
    && cd / && rm -rf /tmp/proof /tmp/proof-home \
    # The resolver markers the build just wrote are new directories inside
    # already-writable parents, so they are caught here rather than by the pass
    # in the layer above.
    && find /root/.m2 -type d ! -perm -o+w -exec chmod a+rwX {} +

# ---------------------------------------------------------------------------
# THE ORACLE, kept runnable.
#
# instruction.md §3 tells the agent that State A is the oracle and the only one
# it needs: build it, launch it, curl it, diff its answers against the rewrite's.
# Every later section leans on that -- §4 says to verify each clause against
# State A rather than against the document, §4.3 says to ask the oracle for each
# status code, and the closing line says to ask before assuming.
#
# But the retirement below removes the framework's jars from the local Maven
# repository, and that omission is deliberately the whole dependency ban.  So
# State A's web layer cannot be COMPILED in this image: `mvn -o -pl web -am
# package` dies at dependency resolution naming io.dropwizard:dropwizard-core,
# which the expect-fail layer further down asserts must happen.
#
# Those two facts contradict each other, and without this layer the brief's
# central affordance does not exist: an agent that follows §3 literally runs the
# documented command, watches it fail, and concludes -- correctly -- that there is
# no oracle.  It then has nothing left but the prose, which is exactly what §4
# tells it not to trust when the two disagree.
#
# What closes the gap is that `web/pom.xml` repackages with
# spring-boot-maven-plugin, so the jar is a fat jar: it carries its own
# dependencies and runs with an empty ~/.m2.  The proof layer above already built
# it while the framework was still resolvable, so keeping a copy costs one `cp`
# and no extra build.  Compiling against the retired stack stays impossible;
# running a jar that was compiled before the retirement becomes possible.  That is
# the asymmetry this task wants, and it is the same one the poms-survive-the-prune
# rule already relies on.
#
# It is not a hole in the ban.  The ban is enforced twice more, and both are
# stronger than a missing jar: stage 1 reads the submission's source and fails
# `framework_retired` if the retired stack is declared, imported, or reproduced by
# hand, and that gate is required, so failing it zeroes the submission before any
# behaviour is measured.  Unpacking this jar to put the retired artifacts back into
# ~/.m2 does not evade that -- it is precisely what the gate is looking for.
#
# Nothing here reaches the submission: artifact collection takes /workspace/repo
# and this lives in /opt, loose jars are excluded regardless, and the graph caches
# below are excluded as run products.  The agent cannot accidentally ship the
# oracle, and stage 2 builds both sides from source in its own image.
# ---------------------------------------------------------------------------
# The self-test below deliberately sits AFTER the retirement and after the
# expect-fail assertion, not here.  Running it here would prove the jar starts
# while the framework is still in ~/.m2, which is not the property that matters:
# what has to be true is that the oracle still runs once the jars are gone.  Only
# the `cp` above has to happen before the prune.
COPY oracle/config-base.yml oracle/config-pt.yml /opt/oracle/
COPY oracle/state-a /usr/local/bin/state-a
RUN chmod +x /usr/local/bin/state-a \
    && test -s /opt/oracle/state-a.jar \
    && unzip -p /opt/oracle/state-a.jar META-INF/MANIFEST.MF \
       | grep -E '^(Main|Start)-Class' \
    && echo "oracle jar: $(du -h /opt/oracle/state-a.jar | cut -f1)" \
    # A recorded digest and a pristine copy, so a change to the oracle is visible.
    #
    # The agent runs as root and /opt/oracle is in the writable layer, so it can
    # rewrite the reference it is told to compare itself against.  That is not
    # hypothetical: on the fw07 run this was written for, the agent repointed
    # config-pt.yml at its own graph directory and then restored it with `sed -i`
    # (its own comment on that turn was `# restore`).  The restore was byte-correct,
    # so the only trace left anywhere was an mtime -- and had it been imperfect, the
    # oracle would have quietly disagreed with itself for the rest of the run while
    # still answering every query confidently.
    #
    # Evidence, not prevention: root defeats any permission here, so the goal is
    # that an edit cannot pass unnoticed, and that an accidental one is recoverable
    # without a rebuild.  Hence both a digest to check and a copy to restore from.
    # 0444 and a dotted name keep it out of the way without pretending to be armour.
    && mkdir -p /opt/oracle/.pristine \
    && cp -p /opt/oracle/config-base.yml /opt/oracle/config-pt.yml \
             /opt/oracle/.pristine/ \
    && ( cd /opt/oracle \
         && sha256sum config-base.yml config-pt.yml state-a.jar \
            > .manifest.sha256 ) \
    && chmod 0444 /opt/oracle/.manifest.sha256 \
    && chmod 0444 /opt/oracle/.pristine/*.yml \
    # Verified here, at the only moment the answer is known to be right, so a
    # manifest that does not match what it was just generated from fails the build
    # rather than shipping as a check that can never pass.  Run from /opt/oracle
    # because the manifest holds relative paths, and without `||` because a masked
    # mismatch is the failure this whole block exists to prevent.
    && ( cd /opt/oracle && sha256sum -c .manifest.sha256 ) \
    && echo "oracle: digest recorded for 3 file(s), pristine configs stashed"

# ---------------------------------------------------------------------------
# The retirement, last, because every layer above needed the stack it removes.
# ---------------------------------------------------------------------------
COPY retired-stack.txt /opt/srb/retired-stack.txt
COPY prune-m2.sh /opt/srb/prune-m2.sh
RUN chmod +x /opt/srb/prune-m2.sh \
    && /opt/srb/prune-m2.sh /opt/srb/retired-stack.txt /root/.m2/repository \
    && find /root/.m2/repository -type f -name '_remote.repositories' -delete

# And the consequence, asserted so the image cannot ship without it being true:
# the tree in /workspace/repo no longer builds its web layer, because the
# framework it is written against is gone.
#
# The shape of that failure matters more than the fact of it.  Maven resolves a
# module's dependencies before it compiles anything, so with the jars pruned the
# web layer dies at resolution and names the retired coordinates outright --
# `Could not resolve dependencies for project graphhopper-web-bundle`, then
# io.dropwizard:dropwizard-core:jar:4.0.16 and its transitive closure.  It never
# reaches javac, so there is no COMPILATION ERROR to find.  That is the better
# diagnostic of the two: it tells an agent exactly which artifacts are gone
# instead of making it infer them from a wall of missing symbols.
#
# What must NOT happen is a Maven model error.  If the prune took a pom that some
# BOM imports, the reactor stops loading and the failure moves from "this one
# module cannot resolve its dependencies" to "this repository cannot be read" --
# and an agent whose very first command fails that way has nothing to work with.
# Keeping the poms while removing the jars is what buys that asymmetry, and the
# checks below are what stop the image shipping without it: the reactor must
# load, core must build, and only the web layer may fail.
#
# Written as a script with explicit reporting rather than a chain of && greps:
# the whole point of this layer is to characterise a failure, and a chain that
# redirects Maven to a log and then fails on an assertion prints nothing at all,
# leaving "exit code: 1" as the only evidence of which expectation broke.
RUN set -u; \
    cp -a /workspace/repo /tmp/expectfail; \
    cd /tmp/expectfail; \
    if mvn -B -o -DskipTests -pl web-bundle -am verify > /tmp/ef.log 2>&1; then \
        echo "FATAL: the web layer still builds after the prune, so the" >&2; \
        echo "       framework was not actually retired." >&2; \
        tail -30 /tmp/ef.log >&2; exit 1; \
    fi; \
    fail=0; \
    for want in \
        'Could not resolve dependencies for project [^ ]*graphhopper-web-bundle' \
        'io\.dropwizard:dropwizard-core:jar:[0-9]' \
        'has not been downloaded from it before' \
    ; do \
        grep -qE "$want" /tmp/ef.log || { \
            echo "FATAL: expected the failure to match /$want/ and it does not." >&2; \
            fail=1; }; \
    done; \
    grep -qE '^\[INFO\] GraphHopper Core[ .]+SUCCESS' /tmp/ef.log || { \
        echo "FATAL: GraphHopper Core did not build after the prune.  The prune" >&2; \
        echo "       was supposed to retire only the web framework; taking core" >&2; \
        echo "       with it changes the task from a migration into a repair." >&2; \
        fail=1; }; \
    for unwanted in 'Non-resolvable import POM' 'The build could not read'; do \
        if grep -q "$unwanted" /tmp/ef.log; then \
            echo "FATAL: the failure is a Maven MODEL error ('$unwanted'), not a" >&2; \
            echo "       compile error.  An agent cannot work in a tree whose" >&2; \
            echo "       reactor will not load -- the poms have to survive the prune." >&2; \
            fail=1; \
        fi; \
    done; \
    if [ "$fail" -ne 0 ]; then \
        echo "--- the actual failure, first 60 lines of interest ---" >&2; \
        grep -nE 'ERROR|BUILD FAILURE|does not exist|cannot find symbol|Could not resolve|Non-resolvable' \
            /tmp/ef.log | head -60 >&2; \
        exit 1; \
    fi; \
    echo "confirmed: the web layer no longer resolves its dependencies, core still"; \
    echo "           builds, and Maven still loads the whole reactor"; \
    cd /; rm -rf /tmp/expectfail /tmp/ef.log

# The oracle proved, and its graph caches warmed in the same layer.
#
# Proved because a jar that does not start is worse than no oracle at all: the
# agent would spend turns debugging the benchmark's artifact instead of its own
# work.  Both configurations are started and asked a real question -- a car route
# across Andorra under `base`, and a transit route under `pt` -- because the two
# exercise different code and only `pt` makes §4.1 and §4.2 observable.
#
# Warmed because the import plus the CH and LM preparations are minutes the agent
# would otherwise pay on its first curl, and an oracle that is expensive to
# consult is an oracle that gets consulted once and then guessed at.  The caches
# the two runs leave behind stay in the image, so the agent's first launch is a
# load rather than an import.
#
# `--network none` is not available at build time, so this also incidentally
# confirms the jar needs no network: nothing it does here reaches out, and the
# trial container it will actually run in has no egress at all.
RUN set -eu; \
    for which in base pt; do \
        echo "--- oracle self-test: config-$which.yml ---"; \
        state-a start "$which" || { \
            echo "FATAL: the oracle did not start under config-$which.yml." >&2; \
            echo "       instruction.md §3 promises a runnable State A; an image" >&2; \
            echo "       that ships a broken one is worse than one that ships none," >&2; \
            echo "       because the agent debugs the benchmark instead of its work." >&2; \
            exit 1; }; \
        if [ "$which" = base ]; then \
            q='http://localhost:8989/route?point=42.51,1.53&point=42.53,1.62&profile=car'; \
            body=$(curl -sS -m 60 "$q"); \
            echo "$body" | head -c 200; echo; \
            echo "$body" | grep -q '"distance"' || { \
                echo "FATAL: the oracle answered /route without a distance." >&2; \
                exit 1; }; \
            curl -sS -m 30 http://localhost:8990/healthcheck | grep -q . || { \
                echo "FATAL: the admin connector on 8990 did not answer." >&2; \
                exit 1; }; \
            code=$(curl -sS -o /dev/null -w '%{http_code}' -m 30 \
                   'http://localhost:8989/route-pt?point=42.51,1.53'); \
            [ "$code" = 404 ] || echo "note: /route-pt answered $code without a GTFS feed"; \
        else \
            A=36.914893,-116.76821; B=36.914944,-116.761472; \
            D=2007-01-01T08:00:00Z; \
            args="point=$A&point=$B&pt.earliest_departure_time=$D&profile=pt"; \
            direct=$(curl -sS -m 60 "http://localhost:8989/route-pt?$args"); \
            echo "$direct" | head -c 220; echo; \
            echo "$direct" | grep -q '"legs"' || { \
                echo "FATAL: /route-pt did not answer with a transit itinerary." >&2; \
                echo "       These are the coordinates and the departure instant the" >&2; \
                echo "       graded pt cases use, so if they do not answer here the" >&2; \
                echo "       whole pt module is comparing two error documents." >&2; \
                echo "$direct" | head -c 800 >&2; \
                exit 1; }; \
            via=$(curl -sS -m 60 "http://localhost:8989/route?$args"); \
            : 'compare .paths, not the whole body: info.took and'; \
            : 'hints.visited_nodes move between two identical requests'; \
            paths() { jq -cS '.paths' 2>/dev/null; }; \
            [ "$(echo "$via" | paths)" = "$(echo "$direct" | paths)" ] || { \
                echo "FATAL: /route?profile=pt did not answer as /route-pt did." >&2; \
                echo "       §4.1's redirect filter rewrites this in process, and it" >&2; \
                echo "       is the single most valuable clause in the brief -- if the" >&2; \
                echo "       oracle cannot demonstrate it, the agent cannot check it." >&2; \
                echo "$via" | head -c 800 >&2; \
                exit 1; }; \
            echo "/route?profile=pt is served in process by the pt router (§4.1)"; \
        fi; \
        state-a stop; \
    done; \
    : '--- and the same oracle, reached by the unprivileged uid ---------------'; \
    : 'The two runs above created run/ and the graph caches as root, 0755, which'; \
    : 'is enough for root and nothing else.  §3 tells the agent State A is the'; \
    : 'oracle and the only one it needs -- build it, launch it, curl it -- and at'; \
    : 'any other uid `state-a start` died on /opt/oracle/run/state-a.log with'; \
    : 'EACCES before the JVM was even spawned, so the brief pointed at an'; \
    : 'affordance the image did not have.'; \
    : ''; \
    : '/opt/oracle itself stays 0755 on purpose.  Making it writable would let'; \
    : 'any uid unlink .manifest.sha256 and .pristine/*.yml despite their 0444,'; \
    : 'because deletion is governed by the directory mode and not the file mode,'; \
    : 'and those two are the whole reason an edit to the oracle cannot pass'; \
    : 'unnoticed.  Only what has to be written is opened: run/, for the log and'; \
    : 'the pidfile, and the graph caches, for the gh.lock GraphHopper takes on'; \
    : 'load.  Done in this layer, where they were just created, so the 70 MB of'; \
    : 'cache is not copied up.'; \
    : 'Directories only.  The cache FILES stay 0644, which was measured to be'; \
    : 'enough -- GraphHopper takes gh.lock in the directory and reads the data'; \
    : 'files -- and leaving them read-only means an accidental write cannot'; \
    : 'silently corrupt a cache that no manifest covers.'; \
    chmod 0755 /root /opt/oracle; \
    chmod a+rwX /opt/oracle/run; \
    find /opt/oracle/graph-cache -mindepth 1 -type d -exec chmod a+rwX {} +; \
    UID_A="${SRB_AGENT_UID:-1000}"; \
    rm -rf /tmp/oracle-uid-home; mkdir -p /tmp/oracle-uid-home; \
    chmod 0777 /tmp/oracle-uid-home; \
    as_agent() { \
        setpriv --reuid="$UID_A" --regid="$UID_A" --clear-groups --inh-caps=-all \
            env HOME=/tmp/oracle-uid-home PATH="$PATH" TZ=UTC sh -c "$1"; \
    }; \
    echo "--- oracle reached as uid $UID_A ---"; \
    : 'Deliberately WITHOUT clearing run/ first.  The two root runs above left a'; \
    : 'root-owned state-a.log there, and that is the harder of the two states this'; \
    : 'has to work from: truncating it needs write permission on the FILE, which'; \
    : 'chmod on run/ does not grant, and it is exactly what failed here before'; \
    : 'state-a learned to unlink instead.  A container that is killed rather than'; \
    : 'stopped, or whose setup ran the oracle as root before the agent phase'; \
    : 'dropped uid, hands the agent precisely this.'; \
    as_agent 'state-a start base' || { \
        echo "FATAL: uid $UID_A cannot start the oracle over a root-owned log." >&2; \
        echo "       root can and it does, so this is a permission problem in" >&2; \
        echo "       this image, not a jar problem: instruction.md 3 promises a" >&2; \
        echo "       runnable State A and at this uid the agent would not have" >&2; \
        echo "       one.  If the message names state-a.log, fresh() regressed." >&2; \
        tail -40 /opt/oracle/run/state-a.log 2>/dev/null >&2; \
        exit 1; }; \
    body=$(as_agent "curl -sS -m 60 'http://localhost:8989/route?point=42.51,1.53&point=42.53,1.62&profile=car'"); \
    echo "$body" | head -c 160; echo; \
    echo "$body" | grep -q '"distance"' || { \
        echo "FATAL: the oracle started at uid $UID_A but answered no distance." >&2; \
        echo "$body" | head -c 600 >&2; exit 1; }; \
    as_agent 'state-a stop'; \
    : '--- and pt, at the same uid ------------------------------------------'; \
    : 'base alone would not cover this.  pt writes a DIFFERENT graph cache and'; \
    : 'reads two GTFS feed directories, and it is the configuration §4.1 and the'; \
    : 'pt module are graded on -- an agent that can start base and not pt has'; \
    : 'half an oracle for the half of the brief that pays most.  Run second, from'; \
    : 'a cleared run/, so this leg also covers the ordinary case: no stale log at'; \
    : 'all, which is what a trial container actually starts from.'; \
    rm -f /opt/oracle/run/state-a.log; \
    as_agent 'state-a start pt' || { \
        echo "FATAL: uid $UID_A cannot start the oracle under config-pt.yml." >&2; \
        echo "       base started at this uid, so the difference is pt's own" >&2; \
        echo "       paths: /opt/oracle/graph-cache/pt and the two GTFS feed" >&2; \
        echo "       directories under /workspace/repo." >&2; \
        tail -40 /opt/oracle/run/state-a.log 2>/dev/null >&2; \
        exit 1; }; \
    A=36.914893,-116.76821; B=36.914944,-116.761472; \
    ptargs="point=$A&point=$B&pt.earliest_departure_time=2007-01-01T08:00:00Z&profile=pt"; \
    ptbody=$(as_agent "curl -sS -m 60 'http://localhost:8989/route-pt?$ptargs'"); \
    echo "$ptbody" | head -c 160; echo; \
    echo "$ptbody" | grep -q '"legs"' || { \
        echo "FATAL: at uid $UID_A the pt oracle answered no itinerary.  These" >&2; \
        echo "       are the coordinates the graded pt cases use." >&2; \
        echo "$ptbody" | head -c 600 >&2; exit 1; }; \
    as_agent 'state-a stop'; \
    : 'The evidence files must still be evidence after all of the above.'; \
    as_agent 'rm -f /opt/oracle/.manifest.sha256' 2>/dev/null || true; \
    as_agent 'rm -f /opt/oracle/.pristine/config-base.yml' 2>/dev/null || true; \
    test -f /opt/oracle/.manifest.sha256 && test -f /opt/oracle/.pristine/config-base.yml || { \
        echo "FATAL: uid $UID_A can delete the oracle's own reference copies, so a" >&2; \
        echo "       change to the oracle would leave no trace.  /opt/oracle must" >&2; \
        echo "       not be group- or world-writable; only run/ and the caches." >&2; \
        exit 1; }; \
    ( cd /opt/oracle && sha256sum -c .manifest.sha256 ); \
    rm -rf /tmp/oracle-uid-home; \
    rm -f /opt/oracle/run/state-a.log; \
    echo "oracle: both configurations start, answer, and left warm graph caches,"; \
    echo "        as root and as uid $UID_A -- base over a stale root-owned log,"; \
    echo "        pt from a clean run/ -- while the reference copies stay"; \
    echo "        read-only to that uid"; \
    du -sh /opt/oracle/graph-cache/* 2>/dev/null || true

# ---------------------------------------------------------------------------
# Baseline git snapshot: one commit, no history, no remotes.  Gives the agent a
# clean `git diff` against State A without revealing upstream history.
# ---------------------------------------------------------------------------
COPY gitignore.baseline /workspace/repo/.gitignore
RUN cd /workspace/repo \
    && git init -q -b main \
    && git config user.email "bench@swerefactor.local" \
    && git config user.name  "SWERefactorBench" \
    && git config commit.gpgsign false \
    && git add -A \
    && git commit -q -m "State A: GraphHopper 11.0, Dropwizard 4 web layer" \
    && git tag state-a \
    && git --no-pager log --oneline \
    # Declared in the SYSTEM config, which is the only one that reaches every uid:
    # this repository is created here by root, and git refuses to operate in a
    # repository owned by somebody else -- `detected dubious ownership`, on
    # `status`, `diff` and `log` alike.  The harness loop chmods /workspace/repo
    # for the agent uid but does not chown it, so without this line an agent at
    # any other uid loses the `git diff` against State A that instruction.md tells
    # it to use, and gets a message about ownership rather than about its work.
    # A no-op for root, which owns the tree and passes the check already.
    && git config --system --add safe.directory /workspace/repo \
    && chmod 0644 /etc/gitconfig

# ---------------------------------------------------------------------------
# The state the trial actually ships with, asserted last.
#
# Everything above proved a property in the layer that established it, which is
# where a proof belongs -- and is also why this layer exists: each of those proofs
# would still pass if a LATER layer reset one of the modes it depends on, and the
# failure would then be invisible until a trial ran.  So the final image state is
# checked here, once, against the uid the agent phase will actually be.
#
# This asserts and it does not repair, apart from one cheap normalising pass over
# directories the pruning layers created.  A FATAL here means a layer above needs
# fixing, not that this one should paper over it.
# ---------------------------------------------------------------------------
RUN set -eu; \
    : 'The prune and expect-fail layers ran as root and may have created resolver'; \
    : 'directories inside already-writable parents.  Only those are touched:'; \
    : '`! -perm -o+w` walks the 434 MB closure without copying any of it up.'; \
    find /root/.m2 -type d ! -perm -o+w -exec chmod a+rwX {} +; \
    UID_A="${SRB_AGENT_UID:-1000}"; \
    for d in /root /root/.m2 /root/.m2/repository /root/.npm /opt/oracle \
             /opt/oracle/run; do \
        case "$(stat -c '%A' "$d")" in \
            drwxr-xr-x|drwxrwxrwx|drwxrwxr-x) : ;; \
            *) echo "FATAL: $d is $(stat -c '%A' "$d"), which an agent at uid" >&2; \
               echo "       $UID_A cannot traverse or write." >&2; exit 1 ;; \
        esac; \
    done; \
    : 'And the two files whose whole purpose is to be read by that uid.'; \
    for f in /root/.npmrc "${MAVEN_HOME:-/usr/share/maven}/conf/settings.xml" \
             /etc/gitconfig /opt/oracle/state-a.jar; do \
        test -r "$f" || { echo "FATAL: $f is not world-readable" >&2; exit 1; }; \
        case "$(stat -c '%A' "$f")" in \
            -r??r??r??) : ;; \
            *) echo "FATAL: $f is $(stat -c '%A' "$f")" >&2; exit 1 ;; \
        esac; \
    done; \
    grep -q '<offline>true</offline>' \
         "${MAVEN_HOME:-/usr/share/maven}/conf/settings.xml" \
        || { echo "FATAL: the global settings do not carry <offline>true</offline>," >&2; \
             echo "       so a non-root uid gets neither it nor the mirror trap." >&2; \
             exit 1; }; \
    : '--- and now as that uid, because a mode is a proxy and this is the thing --'; \
    rm -rf /tmp/finalprobe-home; mkdir -p /tmp/finalprobe-home; \
    chmod 0777 /tmp/finalprobe-home; \
    as_agent() { \
        setpriv --reuid="$UID_A" --regid="$UID_A" --clear-groups --inh-caps=-all \
            env HOME=/tmp/finalprobe-home PATH="$PATH" \
                MAVEN_OPTS="$MAVEN_OPTS" \
                NPM_CONFIG_USERCONFIG="$NPM_CONFIG_USERCONFIG" \
                NPM_CONFIG_CACHE="$NPM_CONFIG_CACHE" \
                TZ=UTC sh -c "$1"; \
    }; \
    as_agent 'test -r /root/.npmrc && grep -q "^offline=true" /root/.npmrc' \
        || { echo "FATAL: uid $UID_A cannot read the offline npmrc, so the frontend" >&2; \
             echo "       build will reach for the registry and die at EAI_AGAIN." >&2; \
             exit 1; }; \
    as_agent 'test -d /root/.npm/_cacache' \
        || { echo "FATAL: uid $UID_A cannot reach the npm cache." >&2; exit 1; }; \
    as_agent 'mkdir -p /root/.npm/_logs/srbprobe && rmdir /root/.npm/_logs/srbprobe' \
        || { echo "FATAL: uid $UID_A cannot write the npm cache; npm writes a log" >&2; \
             echo "       entry even on a pure cache hit and fails if it cannot." >&2; \
             exit 1; }; \
    as_agent 'mkdir -p /root/.m2/repository/srb/finalprobe/1 \
              && : > /root/.m2/repository/srb/finalprobe/1/finalprobe-1.jar' \
        || { echo "FATAL: uid $UID_A cannot write the local Maven repository, so a" >&2; \
             echo "       build that installs or publishes would fail late." >&2; \
             exit 1; }; \
    rm -rf /root/.m2/repository/srb; \
    : 'The Spring Boot side of the closure is what the migration resolves against,'; \
    : 'so it is checked for readability at that uid rather than assumed from a mode.'; \
    as_agent 'find /root/.m2/repository/org/springframework/boot -name "*.jar" \
              -readable -print -quit | grep -q .' \
        || { echo "FATAL: uid $UID_A cannot read the Spring Boot closure." >&2; exit 1; }; \
    as_agent 'cd /workspace/repo && git status --short >/dev/null && git log --oneline -1' \
        || { echo "FATAL: uid $UID_A cannot use the git baseline; instruction.md" >&2; \
             echo "       points the agent at a diff against State A." >&2; exit 1; }; \
    rm -rf /tmp/finalprobe-home; \
    echo "environment: the offline closure, the npm cache, the oracle and the git"; \
    echo "             baseline are all reachable by uid $UID_A, and the oracle's"; \
    echo "             own reference copies are not writable by it"

WORKDIR /workspace/repo
CMD ["/bin/bash"]
