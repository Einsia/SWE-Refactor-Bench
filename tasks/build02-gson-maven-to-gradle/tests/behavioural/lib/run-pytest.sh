#!/usr/bin/env bash
# Run one pytest module against the build matrix the `build` module produced.
#
# Every module's run.sh is one line calling this.  The module contract arrives in
# the environment, so nothing here is task-specific except the ledger path:
#
#   $SRB_SUITE_WORK/builds.json   written by the `build` module; names every
#                                 configuration, its argv, its exit codes and its
#                                 Gradle logs
#
# That ledger is why `build` is declared first and `required = true`.  A missing
# one is not fatal here: the run proceeds and every check that needs a tree fails
# naming the configuration it wanted, which is a truthful report of a submission
# whose build never completed.  Refusing to start would produce no checks at all,
# and a module with no checks is a harness error rather than a score.
#
# The stub directory goes first on PATH for the same reason it does inside a
# build: the runtime probes launch javac and java, and neither may find a real
# `mvn` on the way.  `gradle` is not stubbed -- it is the tool the migration is
# *to*, and the builds this module reads were made with it.
set -uo pipefail

LEDGER="${SRB_BUILD_LEDGER:-${SRB_SUITE_WORK:?}/builds.json}"
if [ ! -f "$LEDGER" ]; then
    echo "no build ledger at $LEDGER -- the build module did not finish;" >&2
    echo "checks needing a build tree will fail rather than be skipped." >&2
fi

export SRB_STUB_DIR="${SRB_STUB_DIR:-/tmp/srb-stubs}"
[ -d "$SRB_STUB_DIR" ] && export PATH="$SRB_STUB_DIR:$PATH"
export JAVA_HOME="${JAVA_HOME:-/opt/java/openjdk}"
# Nothing may leak in from the agent's environment: a check that only passes
# because MAVEN_OPTS is set is not measuring the Gradle build.
unset MAVEN_OPTS MAVEN_ARGS M2_HOME ANT_HOME ANT_OPTS GRADLE_OPTS CLASSPATH
unset JAVA_TOOL_OPTIONS _JAVA_OPTIONS

exec python3 -m pytest \
    -c "${SRB_SUITE_DIR:?}/pytest.ini" \
    -p srbfixtures \
    -p swerefactor.pytest_module \
    --junit-xml="${SRB_WORK:?}/junit.xml" \
    "${SRB_MODULE_DIR:?}" \
    "$@"
