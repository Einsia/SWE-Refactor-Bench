#!/usr/bin/env bash
# Run one pytest module.  Every module's run.sh is one line calling this.
#
# The module contract comes from the environment, so nothing here is
# task-specific.  Two plugins are loaded by name rather than by conftest, because
# a module's rootdir is its own directory and a conftest at the suite root would
# never be found:
#
#   swerefactor.pytest_module   turns each test outcome into a scored check
#   srbfixtures               this suite's fixtures, and the parametrisation that
#                             expands a battery over the cases a module owns
#
# -c is explicit for the same reason: discovery would walk up and find
# pytest.ini today, and stop finding it the first time a module is run from
# somewhere else.
#
# What this deliberately does NOT do is check for a build.  The `build` module
# publishes $SRB_SUITE_WORK/actual.json and every module here reads it, so a
# missing recording is a real failure -- but it is one the modules report per
# check, through replay.MissingRecording, rather than one this script turns into a
# bare non-zero exit with no checks attached.  A module that exits without writing
# checks is an error to the runner and scores nothing; a module that writes 300
# failed checks explains what went wrong.
set -uo pipefail

exec python3 -m pytest \
    -c "${SRB_SUITE_DIR:?}/pytest.ini" \
    -p swerefactor.pytest_module \
    -p srbfixtures \
    --junit-xml="${SRB_WORK:?}/junit.xml" \
    "${SRB_MODULE_DIR:?}" \
    "$@"
