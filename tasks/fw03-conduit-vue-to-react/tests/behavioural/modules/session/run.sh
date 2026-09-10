#!/usr/bin/env bash
# What survives in browser storage, under which key, across all 100 scenarios.
set -euo pipefail
exec bash "${SRB_SUITE_DIR:?}/lib/run-pytest.sh" test_session.py
