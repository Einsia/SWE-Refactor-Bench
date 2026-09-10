#!/usr/bin/env bash
# The awkward corners: unknown routes, a stale token, an in-flight navigation.
set -euo pipefail
exec bash "${SRB_SUITE_DIR:?}/lib/run-pytest.sh" test_screens.py
