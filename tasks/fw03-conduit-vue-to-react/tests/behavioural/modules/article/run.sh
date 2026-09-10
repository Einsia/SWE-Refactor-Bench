#!/usr/bin/env bash
# The article page: body, meta, comments, favourite and follow.
set -euo pipefail
exec bash "${SRB_SUITE_DIR:?}/lib/run-pytest.sh" test_screens.py
