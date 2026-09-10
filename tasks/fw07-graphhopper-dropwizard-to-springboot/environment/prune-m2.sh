#!/usr/bin/env bash
# Retire the old web stack from a warmed Maven local repository, by omission.
#
# THE POINT.  A grader check that says "you still import Dropwizard" is an
# opinion a submission can argue with.  A local repository that has no
# dropwizard-core jar is a fact `javac` reports, and there is no network to fall
# back to.  So the ban lives in the AGENT's environment and the stage images do
# not have to repeat it: a tree that still compiles against the retired stack is
# a tree the agent could not have built.
#
# WHAT IS DELETED AND WHAT IS KEPT.  For every artifact whose groupId matches a
# `group` prefix in retired-stack.txt: the .jar goes, the .pom stays.  That
# asymmetry is deliberate and it is explained at length in retired-stack.txt --
# in short, State A's root pom imports `dropwizard-dependencies` as a BOM, and a
# BOM with no pom makes Maven refuse to load the reactor model at all.  An agent
# would then be unable to run any Maven command from the moment it started until
# the moment it finished removing the import, which punishes the honest path.
# With the poms kept, `mvn` runs, resolution succeeds, and the failure arrives
# where it should: a compile error naming the type that is gone.
#
# Sources and javadoc jars are deleted with the same rule; a -sources jar of the
# retired framework is the framework.
#
# Run once, at image-build time, in the environment image only.  The behavioural
# and verification stage images must be able to build State A, whose whole web
# layer is the retired stack, so they keep the complete closure.  Running this in
# a stage image would make the reference unbuildable, which is why it is not
# copied into one.
set -euo pipefail

MANIFEST="${1:?usage: prune-m2.sh <retired-stack.txt> [repository]}"
REPO="${2:-/root/.m2/repository}"

[ -d "$REPO" ] || { echo "prune-m2: no repository at $REPO" >&2; exit 1; }

# Not named GROUPS: bash keeps the caller's group ids in that name, assignment to
# it fails, and under `set -e` the script would exit 1 having printed nothing.
mapfile -t RETIRED < <(
    grep -E '^[[:space:]]*group[[:space:]]+' "$MANIFEST" \
        | awk '{print $2}' | grep -v '^$' | sort -u
)
[ "${#RETIRED[@]}" -gt 0 ] || {
    echo "prune-m2: $MANIFEST declares no group prefixes" >&2; exit 1; }

echo "prune-m2: retiring ${#RETIRED[@]} group prefix(es) from $REPO"

total_jars=0
total_poms=0
for group in "${RETIRED[@]}"; do
    # groupId prefix -> directory prefix.  A prefix match on the path, so
    # io.dropwizard also reaches io.dropwizard.metrics and io.dropwizard.logback.
    dir="$REPO/${group//.//}"
    [ -d "$dir" ] || { echo "  $group: nothing warmed under it"; continue; }

    jars=$(find "$dir" -type f \( -name '*.jar' -o -name '*.jar.sha1' \) | wc -l)
    poms=$(find "$dir" -type f -name '*.pom' | wc -l)
    find "$dir" -type f \( -name '*.jar' -o -name '*.jar.sha1' \) -delete
    # A jar Maven has already resolved once is recorded in these; leaving them
    # behind makes the next resolution report a checksum problem rather than an
    # absence, and an agent reading that message learns the wrong thing.
    find "$dir" -type f \( -name '_remote.repositories' -o -name '*.lastUpdated' \) -delete
    total_jars=$((total_jars + jars))
    total_poms=$((total_poms + poms))
    printf '  %-40s -%s jar file(s), %s pom(s) kept\n' "$group" "$jars" "$poms"
done

echo "prune-m2: removed $total_jars jar file(s), kept $total_poms pom(s)"

# The invariant, checked rather than assumed: no jar of any retired group is left
# anywhere in the repository, including under a group that only appeared as a
# transitive of something else.
leftover=$(
    for group in "${RETIRED[@]}"; do
        dir="$REPO/${group//.//}"
        if [ -d "$dir" ]; then find "$dir" -type f -name '*.jar' -print; fi
    done
)
if [ -n "$leftover" ]; then
    echo "FATAL: retired jars survived the prune:" >&2
    printf '  %s\n' "$leftover" >&2
    exit 1
fi

# And the converse, which is the half that fails silently: the poms must still be
# there.  A prune that took both would break `mvn` for every submission on its
# first command, and the error would name a BOM rather than anything the agent
# controls.  Checked against the artifact the root pom actually imports.
bom=$(find "$REPO/io/dropwizard" -type f -name 'dropwizard-dependencies-*.pom' \
        2>/dev/null | head -1)
[ -n "$bom" ] || {
    echo "FATAL: dropwizard-dependencies pom is gone; State A's root pom imports" >&2
    echo "       it as a BOM, so no Maven command would run at all" >&2
    exit 1; }
echo "prune-m2: the BOM pom survives at ${bom#$REPO/}"
