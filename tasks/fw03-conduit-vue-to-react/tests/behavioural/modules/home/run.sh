#!/usr/bin/env bash
# The home feed: global and personal tabs, pagination, the tag sidebar.
set -euo pipefail
exec bash "${SRB_SUITE_DIR:?}/lib/run-pytest.sh" test_screens.py
