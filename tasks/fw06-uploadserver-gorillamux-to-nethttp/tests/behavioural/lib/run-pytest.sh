#!/usr/bin/env bash
# Run one module's pytest files against the binary the build module published.
#
# Every pytest module's run.sh is one line that calls this, so the plugin list,
# the ini file and the JUnit path are declared once.  `swerefactor.pytest_module`
# turns the run into the JSON at $SRB_RESULT; `srbfixtures` supplies the binary,
# the recording and the probe launches.
set -euo pipefail

BIN="${SRB_SUITE_WORK:?}/upload-server"
if [ ! -x "$BIN" ]; then
    # Said here as well as in the plugin, because this is the version that
    # appears when someone runs one module by hand: the build module publishes
    # the binary every other module measures, and it has not run.
    echo "no binary at $BIN: the build module did not run, or did not finish" >&2
    exit 70
fi

exec python3 -m pytest \
    -c "${SRB_SUITE_DIR:?}/pytest.ini" \
    -p srbfixtures \
    -p swerefactor.pytest_module \
    --junit-xml="${SRB_WORK:?}/junit.xml" \
    "${SRB_MODULE_DIR:?}" \
    "$@"
