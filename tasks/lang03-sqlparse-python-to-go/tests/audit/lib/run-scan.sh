#!/usr/bin/env bash
# Run one scan module.  Every scan module's run.sh is one line calling this.
#
# The interpreter is the image's own, not one built from the submission: nothing here
# installs the submission and nothing here imports it.  That is the stage-1 contract,
# and for this task it is worth saying twice -- the submission's ancestor is a Python
# package, and `python -c 'import sqlparse'` would be a working oracle if the
# reference were importable here.  It is not, and the Dockerfile asserts it is not.
set -uo pipefail

exec python -m pytest \
    -c "${SRB_SUITE_DIR:?}/pytest.ini" \
    -p srbscan \
    -p swerefactor.pytest_module \
    --junit-xml="${SRB_WORK:?}/junit.xml" \
    "${SRB_MODULE_DIR:?}" \
    "$@"
