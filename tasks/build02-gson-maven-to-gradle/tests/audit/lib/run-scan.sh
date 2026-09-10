#!/usr/bin/env bash
# Every scan module's entry point. `-p srbscan` puts the fixtures in scope without
# a conftest per module directory; `swerefactor.pytest_module` writes SRB_RESULT.
set -uo pipefail
exec python -m pytest \
    -c "${SRB_SUITE_DIR:?}/pytest.ini" \
    -p srbscan \
    -p swerefactor.pytest_module \
    --junit-xml="${SRB_WORK:?}/junit.xml" \
    "${SRB_MODULE_DIR:?}" \
    "$@"
