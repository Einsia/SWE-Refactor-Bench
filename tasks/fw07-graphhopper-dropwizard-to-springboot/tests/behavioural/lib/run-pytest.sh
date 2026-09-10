#!/usr/bin/env bash
# Run one module's pytest files against the ledger the build module published.
#
# Every pytest module's run.sh is one line calling this, so the plugin list, the
# ini file and the JUnit path are declared once.  `swerefactor.pytest_module` turns
# the run into the JSON at $SRB_RESULT; `srbfixtures` supplies the ledger and the
# per-case pairs.
set -euo pipefail

LEDGER="${SRB_SUITE_WORK:?}/capture.json"
if [ ! -f "$LEDGER" ]; then
    # Said here as well as in the plugin, because this is the version that
    # appears when someone runs one module by hand: the build module runs the
    # corpus against both sides and publishes the ledger every other module
    # reads, and it has not run.
    echo "no capture at $LEDGER: the build module did not run, or did not finish" >&2
    exit 70
fi

exec python3 -m pytest \
    -c "${SRB_SUITE_DIR:?}/pytest.ini" \
    -p srbfixtures \
    -p swerefactor.pytest_module \
    --junit-xml="${SRB_WORK:?}/junit.xml" \
    "${SRB_MODULE_DIR:?}" \
    "$@"
