#!/usr/bin/env bash
# Sign in, sign up, and the error rendering the server's 422 shape produces.
set -euo pipefail
exec bash "${SRB_SUITE_DIR:?}/lib/run-pytest.sh" test_screens.py
