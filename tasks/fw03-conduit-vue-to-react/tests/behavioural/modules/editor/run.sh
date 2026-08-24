#!/usr/bin/env bash
# The editor: new and edit, the tag input, and what a publish leaves behind.
set -euo pipefail
exec bash "${SRB_SUITE_DIR:?}/lib/run-pytest.sh" test_screens.py
