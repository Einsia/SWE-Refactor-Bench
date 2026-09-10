#!/usr/bin/env bash
# What each screen asks the API for, in order, across all 100 scenarios.
set -euo pipefail
exec bash "${SRB_SUITE_DIR:?}/lib/run-pytest.sh" test_traffic.py
