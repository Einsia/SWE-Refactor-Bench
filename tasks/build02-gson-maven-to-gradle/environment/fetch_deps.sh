#!/usr/bin/env bash
# Populate /root/.m2/repository from a pinned coordinate manifest.
#
# Runs only at image-build time, the one moment the build has network. Every
# coordinate is fully pinned; nothing is resolved by range or by "latest".
#
# The local repository is the *default* Maven local repository, so both build
# systems find it with no configuration: Maven uses it directly, and Gradle's
# mavenLocal() points at the same directory.
set -euo pipefail

MANIFEST="${1:?usage: fetch_deps.sh <manifest>}"
REPO=/root/.m2/repository
GET=org.apache.maven.plugins:maven-dependency-plugin:3.6.1:get

mkdir -p "$REPO"

mapfile -t COORDS < <(grep -vE '^\s*(#|$)' "$MANIFEST")
echo "fetch_deps: ${#COORDS[@]} coordinates"

# One JVM per artifact would cost ~20 minutes. Batch them: -Dartifact accepts a
# single coordinate, but a batch of parallel mvn processes shares the repository
# safely enough for distinct artifacts, and Maven's own locking covers metadata.
fail=0
batch=0
pids=()
for coord in "${COORDS[@]}"; do
    mvn -q -B --no-transfer-progress -Dmaven.repo.local="$REPO" \
        "$GET" -Dartifact="$coord" -Dtransitive=false \
        > "/tmp/fetch-$batch.log" 2>&1 &
    pids+=("$!:$coord:$batch")
    batch=$((batch + 1))
    if [ "${#pids[@]}" -ge 8 ]; then
        for entry in "${pids[@]}"; do
            pid="${entry%%:*}"; rest="${entry#*:}"
            c="${rest%:*}"; b="${rest##*:}"
            if ! wait "$pid"; then
                echo "FETCH FAILED: $c" >&2
                tail -5 "/tmp/fetch-$b.log" >&2 || true
                fail=$((fail + 1))
            fi
        done
        pids=()
        rm -f /tmp/fetch-*.log
    fi
done
for entry in "${pids[@]}"; do
    pid="${entry%%:*}"; rest="${entry#*:}"
    c="${rest%:*}"; b="${rest##*:}"
    if ! wait "$pid"; then
        echo "FETCH FAILED: $c" >&2
        tail -5 "/tmp/fetch-$b.log" >&2 || true
        fail=$((fail + 1))
    fi
done
rm -f /tmp/fetch-*.log

if [ "$fail" -gt 0 ]; then
    echo "fetch_deps: $fail coordinate(s) failed" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Invariant: the artifacts under migration are not resolvable offline. If any
# gson 2.10.1 artifact were present, a submission could satisfy the tests by
# resolving a prebuilt jar instead of building one.
# ---------------------------------------------------------------------------
# Matched by walking the group for a 2.10.1 directory rather than by naming the
# artifact ids: this list used to name them, and spelled the protobuf module
# `gson-proto` when its artifactId is plain `proto`, so that entry matched nothing.
# The group plus the version cannot be got wrong that way, and gson 2.8.x -- a
# legitimate dependency of one of the plugins -- stays resolvable.
found=$(find "$REPO/com/google/code/gson" -mindepth 2 -maxdepth 2 -type d \
            -name 2.10.1 2>/dev/null || true)
if [ -n "$found" ]; then
    echo "FATAL: offline repo contains artifacts of the repository under test:" >&2
    echo "$found" >&2
    exit 1
fi

# Resolution bookkeeping files record which remote a file came from; with no
# network they only cause Maven to re-check. Drop them so the offline repo
# behaves as a pure local repository.
find "$REPO" -name '_remote.repositories' -delete
find "$REPO" -name '*.lastUpdated' -delete

jars=$(find "$REPO" -name '*.jar' | wc -l)
poms=$(find "$REPO" -name '*.pom' | wc -l)
echo "fetch_deps: ok — $jars jars, $poms poms, $(du -sh "$REPO" | cut -f1)"
