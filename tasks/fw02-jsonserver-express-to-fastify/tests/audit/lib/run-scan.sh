#!/usr/bin/env bash
# Run one scan module.  Every scan module's run.sh is one line calling this.
#
# The interpreter is the image's own.  There is no node in this image and nothing
# here installs the submission, so a module that tried to import it would fail on
# a missing interpreter rather than quietly grade a build -- which is the stage-1
# contract, and the reason this script is five lines while stage 2's equivalent
# has to locate the venv its install module produced.
set -uo pipefail

exec python -m pytest \
    -c "${SRB_SUITE_DIR:?}/pytest.ini" \
    -p srbscan \
    -p swerefactor.pytest_module \
    --junit-xml="${SRB_WORK:?}/junit.xml" \
    "${SRB_MODULE_DIR:?}" \
    "$@"
