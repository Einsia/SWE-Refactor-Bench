#!/usr/bin/env bash
# Prove, at image-build time, that this environment builds State A completely
# offline — and that the toolchain the migration targets is present and usable
# too.
#
# This is a check on the environment, not a rehearsal of the grading. It knows
# nothing about how a submission is scored, asserts nothing a submission has to
# satisfy, and is deleted in the layer that runs it.
#
# Runs on a scratch copy of the repository. /workspace/repo is left pristine so
# every trial container starts from the same clean State A tree with no build
# products and no populated Gradle caches beyond what this check needs.
#
# Any failure here fails the image build. That is the point: a task whose State A
# cannot be reproduced offline is not a task.
set -euo pipefail

SRC=/workspace/repo
SCRATCH=/tmp/srb-state-a
LOG=/tmp/srb-state-a.log

echo "verify_environment: staging scratch copy"
rm -rf "$SCRATCH"
mkdir -p "$SCRATCH"
tar -C "$SRC" --exclude=.git -cf - . | tar -C "$SCRATCH" -xf -
test -f "$SCRATCH/pom.xml"
test ! -e "$SCRATCH/.git"

# ---------------------------------------------------------------------------
# 1. The Maven reactor builds, offline, with its tests.
#    This is the ground truth the migration must preserve, so it is not merely
#    compiled: the full test suite runs.
# ---------------------------------------------------------------------------
echo "verify_environment: mvn -o package (this is State A, tests included)"
cd "$SCRATCH"
if ! mvn -o -B --no-transfer-progress package > "$LOG" 2>&1; then
    echo "FATAL: State A does not build offline" >&2
    tail -80 "$LOG" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# 2. The artifacts State A is defined by are all there.
# ---------------------------------------------------------------------------
for jar in gson/target/gson-2.10.1.jar \
           extras/target/gson-extras-2.10.1.jar \
           metrics/target/gson-metrics-2.10.1.jar \
           proto/target/gson-proto.jar; do
    test -s "$SCRATCH/$jar" || { echo "FATAL: missing $jar" >&2; exit 1; }
done

# The three features most likely to be lost in a migration, checked directly.
unzip -p gson/target/gson-2.10.1.jar META-INF/MANIFEST.MF \
    | tr -d '\r' | grep -q 'Bundle-SymbolicName: com.google.gson' \
    || { echo "FATAL: no OSGi manifest in State A" >&2; exit 1; }
unzip -l gson/target/gson-2.10.1.jar \
    | grep -q 'META-INF/versions/9/module-info.class' \
    || { echo "FATAL: no Multi-Release module descriptor in State A" >&2; exit 1; }
unzip -p gson/target/gson-2.10.1.jar \
        com/google/gson/internal/GsonBuildConfig.class \
    | grep -aq '2\.10\.1' \
    || { echo "FATAL: version was not filtered into GsonBuildConfig" >&2; exit 1; }

# Test counts, so a silently empty surefire run cannot pass for a build.
cases=$(grep -ho 'tests="[0-9]*"' gson/target/surefire-reports/TEST-*.xml \
        | grep -o '[0-9]*' | awk '{s+=$1} END {print s+0}')
echo "verify_environment: gson surefire test cases = $cases"
[ "$cases" -ge 1200 ] || { echo "FATAL: only $cases test cases" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 3. State B's toolchain is present, offline-capable, and sees the vendored
#    plugin modules. Checked on a throwaway project, not on the repository:
#    the agent must find nothing here that resembles a partial solution.
# ---------------------------------------------------------------------------
#    Every plugin id instruction.md advertises that comes from a vendored jar is
#    APPLIED here, not merely resolved.  (Gradle's own built-ins -- `java-library`,
#    `maven-publish` -- are advertised too and are not the risk: they cannot fail to
#    resolve from an offline repository, which is the failure this probe exists to
#    catch.)  That distinction is the whole point: an earlier
#    revision advertised `com.guardsquare.proguard` and `proguard`, both of which
#    resolve as jars and neither of which can apply -- one registers no such id,
#    the other demands the Android plugin -- and the agent burned four turns on
#    them because this script applied only one id and resolved the rest.
#    `apply false` is not a substitute: it resolves every mapped id quietly,
#    including one no jar registers, because Gradle defers the descriptor lookup
#    to apply time.  If a new id backed by a vendored jar is advertised, add it to
#    the plugins block.
echo "verify_environment: gradle offline smoke test"
PROBE=/tmp/srb-gradle-probe
rm -rf "$PROBE"; mkdir -p "$PROBE"
cat > "$PROBE/settings.gradle" <<'EOF'
rootProject.name = 'srb-probe'
EOF
cat > "$PROBE/build.gradle" <<'EOF'
plugins { id 'java-library'; id 'biz.aQute.bnd.builder'; id 'com.google.protobuf' }
java { toolchain { languageVersion = JavaLanguageVersion.of(17) } }
configurations { probe }
dependencies {
    probe 'junit:junit:4.13.2'
    probe 'com.guardsquare:proguard-base:7.2.2'
    probe 'com.google.protobuf:protobuf-java:4.0.0-rc-2'
}
tasks.register('resolveProbe') {
    def files = configurations.probe
    doLast {
        def n = files.files.size()
        // junit, proguard-base and protobuf-java plus their transitive closure.
        if (n < 3) { throw new GradleException("resolved only ${n} artifacts") }
        println "SRB_PROBE_OK ${n}"
    }
}
EOF
cd "$PROBE"
if ! gradle --offline --no-daemon --console=plain resolveProbe \
        > /tmp/srb-gradle-probe.log 2>&1; then
    echo "FATAL: Gradle cannot resolve the vendored closure offline" >&2
    tail -60 /tmp/srb-gradle-probe.log >&2
    exit 1
fi
grep -q 'SRB_PROBE_OK' /tmp/srb-gradle-probe.log \
    || { echo "FATAL: unexpected Gradle probe output" >&2
         tail -30 /tmp/srb-gradle-probe.log >&2; exit 1; }
echo "verify_environment: $(grep -o 'SRB_PROBE_OK [0-9]*' /tmp/srb-gradle-probe.log) (offline)"

# Protoc must be executable in this image: the proto module's codegen needs it.
PROTOC=$(find /root/.m2/repository/com/google/protobuf/protoc -name '*linux-x86_64.exe' | head -1)
test -n "$PROTOC" || { echo "FATAL: no protoc binary vendored" >&2; exit 1; }
install -m 0755 "$PROTOC" /tmp/protoc-probe
/tmp/protoc-probe --version || { echo "FATAL: vendored protoc will not run" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 4. The same toolchain, reached by an UNPRIVILEGED uid.
#
#    Everything above ran as root, and root cannot fail any of it: it ignores
#    permission bits entirely, and its HOME is /root, so `mavenLocal()` lands on
#    the vendored repository by coincidence.  Both of those stop being true the
#    moment the agent phase runs as anyone else -- which is not hypothetical but
#    the default for one of the two harnesses that drive this benchmark, because
#    Claude Code refuses every bypass-permissions mode under root and its loop
#    therefore drops to a fixed unprivileged uid.
#
#    Under that uid, and with no fix, this image is not merely harder.  There are
#    three separate couplings to root, and each alone is fatal:
#      - /root is 0700, so the whole vendored closure is unreachable;
#      - `mavenLocal()` follows $HOME, not GRADLE_USER_HOME, so vendored artifacts
#        resolve against an empty $HOME/.m2/repository;
#      - Gradle reads init.d only from its own user home, so with GRADLE_USER_HOME
#        unset srb-offline.gradle is never loaded and `pluginManagement` ends up
#        with NO repository at all.  This one is the least visible of the three:
#        it fails as `Plugin [id: ...] was not found in any of the following
#        sources` rather than as a permission or a path error, so it reads like a
#        missing artifact when the artifacts are all present and only the
#        declaration that points at them is missing.
#    Together they produced a published score of zero for fourteen trials with no
#    signal in them about the model, which is exactly what this check exists to
#    prevent -- and it did not prevent it, because it supplied by hand the one
#    variable the runtime lacks.  Hence the assertions below rather than settings.
#
#    So the same probe runs again as that uid.  A fresh HOME, deliberately not
#    /root, so a pass here cannot be borrowing root's coincidence; the daemon and
#    cache directories cleared first, because the root run above left them 0700
#    and an inherited root-owned daemon dir fails with `error=13` on the JDK
#    rather than on anything diagnostic.
# ---------------------------------------------------------------------------
UIDPROBE_UID="${SRB_VERIFY_AGENT_UID:-1000}"
UIDPROBE_HOME=/tmp/srb-uidprobe-home
echo "verify_environment: re-running the offline probe as uid $UIDPROBE_UID"
rm -rf "$UIDPROBE_HOME"
mkdir -p "$UIDPROBE_HOME"
chmod 0777 "$UIDPROBE_HOME"
rm -rf /root/.gradle/caches /root/.gradle/daemon /root/.gradle/native \
       /root/.gradle/workers /root/.gradle/build-scan-data
chmod 0755 /root
chmod -R a+rwX /root/.gradle
chmod -R a+rwX "$PROBE"

# Every variable below must arrive the way the trial container gets it -- from the
# image ENV -- and NOT be supplied here.  An earlier version of this check set
# GRADLE_USER_HOME=/root/.gradle by hand, and so it proved a condition no container
# ever has: task.toml's [environment.env] does not reach the agent container for
# this environment type (measured: the variables that exist only there are absent
# from both `printenv` and `docker inspect .Config.Env`), and the loop execs the
# agent with HOME=/opt/claude-home.  The image built green while the real runtime
# could not resolve a single vendored plugin.  So it is asserted, not set.
[ -n "${GRADLE_USER_HOME:-}" ] || {
    echo "FATAL: GRADLE_USER_HOME is not in this image's ENV." >&2
    echo "       Gradle reads init.d only from its user home, so without this the" >&2
    echo "       agent's user home becomes \$HOME/.gradle, srb-offline.gradle is" >&2
    echo "       never loaded, and pluginManagement has no repository at all." >&2
    echo "       Set it in the Dockerfile ENV block; task.toml cannot deliver it." >&2
    exit 1; }
[ -d "${GRADLE_USER_HOME}/init.d" ] || {
    echo "FATAL: GRADLE_USER_HOME=$GRADLE_USER_HOME has no init.d directory," >&2
    echo "       so it does not name the home srb-offline.gradle was installed to." >&2
    exit 1; }

# Not `su`/`runuser`: those need a passwd entry and a login shell for the uid,
# which a minimal image need not have, and the loop this mirrors uses setpriv for
# that same reason.  HOME is set by hand for that same reason -- and deliberately
# NOT to /root, so a pass here cannot be borrowing root's coincidence.
as_agent() {
    setpriv --reuid="$UIDPROBE_UID" --regid="$UIDPROBE_UID" --clear-groups \
        --inh-caps=-all env HOME="$UIDPROBE_HOME" \
        GRADLE_USER_HOME="${GRADLE_USER_HOME:-}" \
        GRADLE_OPTS="${GRADLE_OPTS:-}" \
        MAVEN_OPTS="${MAVEN_OPTS:-}" \
        PATH="$PATH" \
        sh -c "$1"
}

if ! as_agent "cd '$PROBE' && gradle --offline --no-daemon --console=plain resolveProbe" \
        > /tmp/srb-uidprobe.log 2>&1; then
    echo "FATAL: uid $UIDPROBE_UID cannot use the vendored toolchain." >&2
    echo "       root can and it does not, so this is a permission or a" >&2
    echo "       \$HOME-coupling problem in this image, not a Gradle problem." >&2
    echo "       If the failure names MavenLocal(file:/home/...), GRADLE_OPTS has" >&2
    echo "       lost -Dmaven.repo.local and mavenLocal() is following HOME." >&2
    tail -40 /tmp/srb-uidprobe.log >&2
    exit 1
fi
grep -q 'SRB_PROBE_OK' /tmp/srb-uidprobe.log \
    || { echo "FATAL: the uid $UIDPROBE_UID probe did not report success" >&2
         tail -30 /tmp/srb-uidprobe.log >&2; exit 1; }
echo "verify_environment: $(grep -o 'SRB_PROBE_OK [0-9]*' /tmp/srb-uidprobe.log) as uid $UIDPROBE_UID"

# Writing to the local repository too, since a Gradle migration may publish to it
# and a read-only closure would fail only at that late point.  Removed again
# immediately: nothing this check installs may be resolvable by a submission.
as_agent 'mkdir -p /root/.m2/repository/srb/uidprobe/1 \
          && : > /root/.m2/repository/srb/uidprobe/1/uidprobe-1.jar' \
    || { echo "FATAL: uid $UIDPROBE_UID cannot write to the local Maven repository," >&2
         echo "       so a build that publishes to it would fail." >&2; exit 1; }
rm -rf /root/.m2/repository/srb
echo "verify_environment: uid $UIDPROBE_UID can also write the local repository"

# The git baseline too, which is a different failure with the same cause. The
# Dockerfile creates /workspace/repo's repository as root; git refuses to operate
# in a repository owned by somebody else and says `detected dubious ownership` on
# `status`, `diff` and `log` alike. The harness loop chmods the tree for the agent
# uid but does not chown it, so what an agent at another uid loses is exactly the
# `git diff` against State A that instruction.md tells it to use. The Dockerfile's
# system-level safe.directory is what fixes it; this is where that is checked.
as_agent 'cd /workspace/repo && git status --short >/dev/null && git log --oneline -1' \
    > /tmp/srb-uidgit.log 2>&1 \
    || { echo "FATAL: uid $UIDPROBE_UID cannot use the git baseline." >&2
         cat /tmp/srb-uidgit.log >&2; exit 1; }
rm -f /tmp/srb-uidgit.log
echo "verify_environment: uid $UIDPROBE_UID can diff against the State A baseline"

# ---------------------------------------------------------------------------
# 5. Clean up. Nothing this check produced may survive into the trial: no build
#    output, no scratch project, and no gson artifact in the local repository.
# ---------------------------------------------------------------------------
echo "verify_environment: cleaning up"
cd /
rm -rf "$UIDPROBE_HOME" /tmp/srb-uidprobe.log
rm -rf "$SCRATCH" "$PROBE" /tmp/protoc-probe /tmp/srb-gradle-probe.log "$LOG"
rm -rf /root/.gradle/caches /root/.gradle/daemon /root/.gradle/native \
       /root/.gradle/workers /root/.gradle/build-scan-data
# Matched by namespace rather than by an enumeration of artifact ids, because an
# enumeration is a thing to get wrong: the proto module's artifactId is `proto`
# and only its <finalName> is `gson-proto`, so a list naming `gson-proto` checks
# a path that cannot exist and leaves the real one unchecked.
installed=$(find /root/.m2/repository/com/google/code/gson \
                 -mindepth 2 -maxdepth 2 -type d -name 2.10.1 2>/dev/null || true)
if [ -n "$installed" ]; then
    echo "FATAL: the State A build installed artifacts under migration into the" >&2
    echo "local repository, which would make them resolvable to a submission:" >&2
    echo "$installed" >&2
    exit 1
fi
test -z "$(find /workspace/repo -name target -type d -print -quit)" \
    || { echo "FATAL: build output leaked into /workspace/repo" >&2; exit 1; }

# The permissions the trial actually ships with, applied last so that the state
# the checks above proved is the state that survives into the image.
#
# `! -perm -o+w` selects only the directories that still need it, so the vendored
# closure -- already chmodded in the layer that created it -- is walked but not
# touched, and 107 MB of files are not copied into this layer for nothing.  What
# is left to catch is what the checks above created: the resolver markers the
# State A build wrote, and the Gradle directories re-made after the cleanup.
# Deliberately not `-prune`d on the already-writable ones: a directory the State A
# build created sits INSIDE a writable parent, so pruning at the parent would skip
# exactly the new directories this is here to fix.
chmod 0755 /root
find /root/.m2 -type d ! -perm -o+w -exec chmod a+rwX {} +
chmod -R a+rwX /root/.gradle

# Asserted rather than assumed, because every failure this script exists to catch
# would be re-introduced by a later layer silently resetting one of these.
test -r /root/.m2/settings.xml || { echo "FATAL: settings.xml unreadable" >&2; exit 1; }
for d in /root /root/.m2 /root/.m2/repository /root/.gradle /root/.gradle/init.d; do
    case "$(stat -c '%A' "$d")" in
        drwxr-xr-x|drwxrwxrwx) : ;;
        *) echo "FATAL: $d is $(stat -c '%A' "$d"), which an unprivileged agent" >&2
           echo "       cannot traverse; see section 4." >&2; exit 1 ;;
    esac
done

echo "verify_environment: OK — State A builds offline, State B toolchain ready,"
echo "verify_environment:      and both are reachable by an unprivileged uid"
