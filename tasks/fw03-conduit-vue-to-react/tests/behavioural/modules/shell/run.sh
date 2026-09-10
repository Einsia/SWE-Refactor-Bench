#!/usr/bin/env bash
# The app shell: boot, header, navigation between the top-level routes.
set -euo pipefail
exec bash "${SRB_SUITE_DIR:?}/lib/run-pytest.sh" test_screens.py
