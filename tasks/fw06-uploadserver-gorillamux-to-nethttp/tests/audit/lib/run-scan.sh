#!/usr/bin/env bash
# Run one scan module.  Every scan module's run.sh is one line calling this.
#
# The interpreter is the image's own.  Nothing here installs the submission and
# nothing here imports it -- that is the stage-1 contract, and it is why this
# script is four lines while stage 2's equivalent has to locate a binary the
# build module produced.
set -uo pipefail

exec python -m pytest \
    -c "${SRB_SUITE_DIR:?}/pytest.ini" \
    -p srbscan \
    -p swerefactor.pytest_module \
    --junit-xml="${SRB_WORK:?}/junit.xml" \
    "${SRB_MODULE_DIR:?}" \
    "$@"
