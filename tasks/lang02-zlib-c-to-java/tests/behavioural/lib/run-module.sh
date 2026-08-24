#!/usr/bin/env bash
# The entry point of every module in this suite.
#
# Each modules/<id>/run.sh is a symlink to this file.  There is one script rather
# than eighteen copies because every module runs the same engine over a different
# slice of the frozen case catalog -- the slice is named by $SRB_MODULE_ID and
# defined in lib/driver.py.  A module needing its own script would be a module
# whose subject the driver does not understand.
#
# If one ever does, it replaces its symlink with a real script and nothing else
# changes: the runner only requires that run.sh exists and writes $SRB_RESULT.
set -euo pipefail

: "${SRB_MODULE_ID:?the behavioural runner sets this}"
: "${SRB_SUITE_DIR:?the behavioural runner sets this}"
: "${SRB_RESULT:?the behavioural runner sets this}"

# -I: isolated mode, so the engine cannot pick up a module from a user site
# directory or from anywhere the submission could have written.  This is safe
# here only because -I implies -E (PYTHONPATH ignored) and -P (the script's own
# directory dropped from sys.path), and driver.py puts its directory back itself
# before importing its siblings.  Nothing in lib/ imports the infra package, so
# losing the runner's PYTHONPATH costs nothing.
exec python3 -I "${SRB_SUITE_DIR}/lib/driver.py" --module "${SRB_MODULE_ID}"
