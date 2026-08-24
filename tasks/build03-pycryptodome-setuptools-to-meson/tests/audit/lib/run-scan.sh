#!/usr/bin/env bash
# Every scan module runs this. `-p srbscan` loads the fixtures from lib/ (on
# PYTHONPATH via the image), `-p swerefactor.pytest_module` turns each collected
# test into one check in the module result. The module directory is the only
# argument, so a module cannot collect its neighbour's tests.
set -euo pipefail

exec python -m pytest \
    -c "${SRB_SUITE_DIR:?}/pytest.ini" \
    -p srbscan \
    -p swerefactor.pytest_module \
    --junit-xml="${SRB_WORK:?}/junit.xml" \
    "${SRB_MODULE_DIR:?}" \
    "$@"
