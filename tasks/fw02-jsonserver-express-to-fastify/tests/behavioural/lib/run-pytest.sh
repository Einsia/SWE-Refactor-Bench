#!/usr/bin/env bash
# Run one pytest module against the tree the `install` module built.
#
# Every module's run.sh is one line calling this.  The module contract is in the
# environment, so nothing here is task-specific except the one shared path:
#
#   $SRB_SUITE_WORK/build   a copy of the submission with everything a build
#                           produces removed, its dependencies installed offline
#                           from the mirror, and its build script run
#
# That directory is the reason `install` is declared first and `required = true`:
# it is the only thing shared between modules, and a suite that cannot install
# the submission has nothing behavioural left to measure.  Booting the delivered
# tree instead would hide exactly that -- a shipped node_modules would answer for
# a package.json that cannot resolve -- so a missing build is an error here.
#
# The interpreter is the image's own.  Unlike a Python target, the thing being
# graded is not a library this process imports; it is a server this process talks
# to over a socket, so the grader's stack and the submission's stack never meet.
set -uo pipefail

BUILD="${SRB_SUITE_WORK:?}/build"
if [ ! -f "$BUILD/package.json" ]; then
    echo "nothing installed at $BUILD -- the install module did not run or did" >&2
    echo "not finish; this module cannot report on the submission." >&2
    exit 1
fi

exec python3 -m pytest \
    -c "${SRB_SUITE_DIR:?}/pytest.ini" \
    -p srbfixtures \
    -p swerefactor.pytest_module \
    --junit-xml="${SRB_WORK:?}/junit.xml" \
    "${SRB_MODULE_DIR:?}" \
    "$@"
