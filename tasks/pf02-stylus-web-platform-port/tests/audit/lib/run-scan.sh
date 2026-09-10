#!/usr/bin/env bash
# Run one scan module.  Every scan module's run.sh is one line calling this.
#
# The interpreter is the image's own, and it is a Python one: nothing here loads
# the submission's JavaScript, and there is no `node` in this image to load it
# with.  That is the stage-1 contract, and it is the reason this script is four
# lines while stage 2's equivalent has to reinstall the submission from its own
# lockfile before a module can measure anything.
#
# `python` is what the image has and is found first there; the fallback is for
# running this suite by hand on a host that only ships the versioned name.  That
# is not a hypothetical convenience -- every check in this suite was calibrated by
# running it outside the image against a real tree, and a scan that can only
# execute inside its own container is a scan nobody sweeps for false positives.
set -uo pipefail

PY="$(command -v python || command -v python3)"

exec "$PY" -m pytest \
    -c "${SRB_SUITE_DIR:?}/pytest.ini" \
    -p srbscan \
    -p swerefactor.pytest_module \
    --junit-xml="${SRB_WORK:?}/junit.xml" \
    "${SRB_MODULE_DIR:?}" \
    "$@"
