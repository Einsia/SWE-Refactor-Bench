#!/usr/bin/env bash
# One line of real work.  The shared runner in lib/run-pytest.sh checks that the
# capture exists, explains itself if it does not, and execs pytest with the
# fixtures plugin and the module reporter loaded.  Nine modules invoke it the same
# way; keeping the logic in one file means a fix to it is a fix everywhere.
set -euo pipefail
exec bash /tests/behavioural/lib/run-pytest.sh test_admin.py
