#!/usr/bin/env bash
# Run one pytest module.  Every dimension module's run.sh is one line calling
# this, so there is a single place where the pytest invocation lives.
#
# The module contract arrives in the environment, so nothing here is per-module.
# The one shared input is the tree the `prepare` module publishes:
#
#   $SRB_SUITE_WORK/tree   the submission, copied out of the graded directory
#                          with its own dependencies installed offline
#
# That is why `prepare` is declared first and `required = true`.  Measuring
# $SRB_REPO directly instead would mean writing inside the artifact being graded
# and loading whatever the agent left in node_modules, so a missing tree is an
# error here rather than a fallback.
set -uo pipefail

TREE="${SRB_SUITE_WORK:?}/tree"
if [ ! -d "$TREE" ]; then
    echo "no staged tree at $TREE -- the prepare module did not run or did not" >&2
    echo "finish; this module cannot report on the submission." >&2
    exit 1
fi

exec python3 -m pytest \
    -c "${SRB_SUITE_DIR:?}/pytest.ini" \
    -p srbstylus \
    -p swerefactor.pytest_module \
    --junit-xml="${SRB_WORK:?}/junit.xml" \
    "${SRB_MODULE_DIR:?}" \
    "$@"
