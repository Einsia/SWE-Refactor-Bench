# SWERefactorBench — build-toolchain / Gson: Maven reactor -> Gradle multi-project
#
# Agent environment (State A). Ships the toolchain for *both* build systems so
# the agent can observe the Maven build it is replacing.
#
# The three grading images do not all look like this one, and the differences
# are the point. Stage 1 reviews source and has no JDK and neither build system,
# so a reviewer cannot answer "are the four jars produced by rules" with a build
# log. Stage 2 has Gradle and asserts Maven is absent, so a tree that still
# needs Maven is unbuildable there by construction. Stage 3 has both, because
# "the Maven build ships this and yours does not" is a claim that must be
# executed on both sides.
#
# Base image pinned by digest: maven:3.9.9-eclipse-temurin-17
# -> Ubuntu noble, Maven 3.9.9, Eclipse Temurin JDK 17. The digest is the
# version contract: every stage image starts from this same line, which is what
# makes comparing class bytes across containers sound rather than lucky.
FROM maven:3.9.9-eclipse-temurin-17@sha256:f58d59b6273e785ac0a4477f6e9b5ba1d7731c75b906c0f7b34076f1851318cc

# GRADLE_OPTS carries -Dmaven.repo.local for a reason that is not obvious and
# that silently zeroed this task for every harness that runs the agent as a
# non-root uid.  srb-offline.gradle points every repository at `mavenLocal()`,
# and so does any build.gradle a submission writes -- but `mavenLocal()` resolves
# against $HOME/.m2/repository, NOT against GRADLE_USER_HOME and not against
# Maven's own global settings (Gradle does not read $MAVEN_HOME/conf/settings.xml
# at all; that was measured, not assumed).  Under root the two coincide, because
# HOME is /root, so the coincidence hid the coupling.  Under any other uid
# `mavenLocal()` silently becomes an empty directory and every plugin id in
# instruction.md fails to resolve -- the agent sees
# `MavenLocal(file:/home/<user>/.m2/repository)` in the failure and no way to fix
# it from inside /workspace/repo.  The system property is the one form that is
# HOME-independent, is honoured by `mavenLocal()` wherever it is written, and
# names the same path root already resolved to, so it changes nothing for root.
#
# GRADLE_USER_HOME below is a SECOND, independent coupling to $HOME, and the two
# are easy to conflate.  The system property above fixes where vendored *artifacts*
# are found.  This variable fixes whether the vendored *repository declaration* is
# read at all.  srb-offline.gradle is the only place `pluginManagement.repositories`
# is ever populated -- the Gradle Plugin Portal is unreachable from a trial
# container -- and Gradle loads init.d scripts exclusively from its own user home,
# which is GRADLE_USER_HOME or, absent that, $HOME/.gradle.  Under root the two
# coincide at /root/.gradle and the script is found.  Under any other uid the user
# home moves with HOME, the init script is never loaded, and the failure is not a
# permission error at all but
#     Plugin [id: 'biz.aQute.bnd.builder'] was not found in any of the following sources
# with no vendored repository among the sources listed and nothing an agent can do
# about it from inside /workspace/repo.  Naming the path explicitly is
# HOME-independent and names what root already resolves to, so like the system
# property it changes nothing for a root harness.
#
# This is the same defect, in this same image, as the doubled settings-offline.xml
# further down: a repository declaration installed only under $HOME reaches only
# the uid that owns that HOME.  Maven had a global file to install it into as well.
# Gradle has no equivalent outside its distribution directory, so this variable
# carries it instead.  Setting it in task.toml's [environment.env] does NOT work
# and was measured: that table does not reach the agent container for this
# environment type, and the image ENV is the only channel that does.
ENV DEBIAN_FRONTEND=noninteractive \
    LC_ALL=C.UTF-8 \
    LANG=C.UTF-8 \
    TZ=UTC \
    MAVEN_OPTS="-Dmaven.repo.local=/root/.m2/repository" \
    GRADLE_OPTS="-Dorg.gradle.daemon=false -Dmaven.repo.local=/root/.m2/repository" \
    GRADLE_USER_HOME=/root/.gradle \
    GRADLE_HOME=/opt/gradle/gradle-8.10.2 \
    PATH=/opt/gradle/gradle-8.10.2/bin:$PATH

# ---------------------------------------------------------------------------
# OS tooling. Installed while the image build still has network; the trial
# container has none.
# ---------------------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
      git unzip zip xz-utils \
      python3 \
      file binutils \
      less procps vim-tiny ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && java -version && mvn -v | head -1

# ---------------------------------------------------------------------------
# Gradle 8.10.2, pinned by SHA-256 of the official binary distribution.
# Installed under /opt so it is on PATH, and seeded into the wrapper cache so a
# generated ./gradlew resolves offline against the same distribution.
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
# Offline dependency closure.
#
# deps.txt is the measured closure of the State A Maven build plus every module
# a Gradle build of the same graph additionally resolves (Gradle mediates to the
# highest version, Maven to the nearest, so the two closures are not equal).
# Fetched here, with network; never fetched again.
#
# It contains no gson 2.10.1 artifact of any kind — that is asserted below, so
# a submission cannot resolve a prebuilt jar instead of building one.
# ---------------------------------------------------------------------------
COPY deps.txt /opt/srb/deps.txt
COPY fetch_deps.sh /opt/srb/fetch_deps.sh
RUN chmod +x /opt/srb/fetch_deps.sh && /opt/srb/fetch_deps.sh /opt/srb/deps.txt \
    # Reachable by whichever uid the agent phase runs as.  A harness that runs the
    # agent as root needs none of this; one that drops to an unprivileged uid --
    # which Claude Code forces, since it refuses every bypass-permissions mode
    # under root -- cannot even traverse /root at the base image's 0700, so the
    # whole vendored closure is invisible and this task becomes unattemptable
    # rather than hard.  Nothing in /root here is private: the local Maven
    # repository, an offline settings file, and a Gradle init script, all of which
    # instruction.md already documents to the agent.
    #
    # Directories get +w, files keep 0644.  The write is needed because Maven and
    # Gradle both create paths inside the repository (a `publishToMavenLocal`, a
    # resolver-status marker), and a new file only needs its parent writable.
    # Done in this layer, where the files were just created, so it costs no
    # copy-up; the same chmod in a later layer would duplicate all 107 MB.
    && chmod 0755 /root \
    && find /root/.m2 -type d -exec chmod a+rwX {} +

# ---------------------------------------------------------------------------
# Gradle offline plumbing, supplied by the environment rather than the agent:
#   - repositories default to the local Maven repository above
#   - plugin ids used by this build map to the vendored plugin modules, since
#     the Gradle Plugin Portal is unreachable
# This is environment infrastructure, exactly like Maven's own settings.xml. It
# is present, identically, in the stage images that run a build. instruction.md
# documents it.
# ---------------------------------------------------------------------------
COPY srb-offline.gradle /root/.gradle/init.d/srb-offline.gradle
COPY settings-offline.xml /root/.m2/settings.xml
# The same settings installed a second time as Maven's *global* file, because
# `<offline>true</offline>` and the mirror-to-an-unusable-URL trap are the two
# things in it that a user-level file cannot deliver to a non-root uid: Maven
# reads the user file from $HOME/.m2/settings.xml, so an agent at any other uid
# gets neither, and an accidental remote resolution then hangs to its timeout
# instead of failing loudly.  `<localRepository>` is in it too and is harmless
# here, since MAVEN_OPTS already names the same path.  Gradle is NOT covered by
# this file -- see the GRADLE_OPTS comment at the top for why it needs the system
# property instead.
RUN cp /root/.m2/settings.xml "${MAVEN_HOME:-/usr/share/maven}/conf/settings.xml" \
    && chmod 0644 /root/.m2/settings.xml \
                  "${MAVEN_HOME:-/usr/share/maven}/conf/settings.xml" \
    && chmod -R a+rwX /root/.gradle

# ---------------------------------------------------------------------------
# Repository snapshot. Verified against environment/original.sha256, then
# unpacked to /workspace/repo. The tarball carries no VCS metadata and no build
# system other than Maven.
# ---------------------------------------------------------------------------
COPY original.tar.gz /tmp/original.tar.gz
COPY original.sha256 /tmp/original.sha256
RUN cd /tmp && sha256sum -c original.sha256 \
    && mkdir -p /workspace/repo \
    && tar -xzf /tmp/original.tar.gz -C /workspace/repo \
    && rm -f /tmp/original.tar.gz /tmp/original.sha256 \
    && test -f /workspace/repo/pom.xml \
    && test -f /workspace/repo/gson/pom.xml \
    && test -f /workspace/repo/gson/bnd.bnd \
    && test -f /workspace/repo/gson/src/main/java/module-info.java \
    && test ! -e /workspace/repo/.git \
    && test ! -e /workspace/repo/.gitignore \
    && test -z "$(find /workspace/repo -name '.git*' -print -quit)"

# ---------------------------------------------------------------------------
# Baseline git snapshot: one commit, no history, no remotes. Gives the agent a
# clean `git diff` against State A without revealing upstream history.
# ---------------------------------------------------------------------------
COPY gitignore.baseline /workspace/repo/.gitignore
RUN cd /workspace/repo \
    && git init -q -b main \
    && git config user.email "bench@swerefactor.local" \
    && git config user.name  "SWERefactorBench" \
    && git config commit.gpgsign false \
    && git add -A \
    && git commit -q -m "State A: Gson 2.10.1, Maven multi-module reactor build" \
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
# Environment validation, run once at image-build time and then deleted.
#
# It says nothing about grading and contains no part of a solution: it builds
# State A with State A's own build system, checks the three artifacts most likely
# to be lost in a migration are there to begin with, and proves the vendored
# closure is enough for an offline resolution on the Gradle side too. If any of
# that is untrue the image build fails, which is the point — an environment whose
# State A cannot be reproduced offline is not an environment.
#
# Runs on a scratch copy so /workspace/repo stays pristine: every trial starts
# from the same clean State A tree with no build products. The script is removed
# in the same layer, so the agent never receives it.
# ---------------------------------------------------------------------------
COPY verify_environment.sh /tmp/verify_environment.sh
RUN chmod +x /tmp/verify_environment.sh \
    && /tmp/verify_environment.sh \
    && rm -f /tmp/verify_environment.sh

WORKDIR /workspace/repo
CMD ["/bin/bash"]
