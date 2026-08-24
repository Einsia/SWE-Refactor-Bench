#!/usr/bin/env bash
# Every pytest module's runner is this line; see lib/run-pytest.sh.
exec bash "${SRB_SUITE_DIR:?}/lib/run-pytest.sh"
