#!/usr/bin/env bash
# Profiles and settings: the two author tabs, and the settings form.
set -euo pipefail
exec bash "${SRB_SUITE_DIR:?}/lib/run-pytest.sh" test_screens.py
