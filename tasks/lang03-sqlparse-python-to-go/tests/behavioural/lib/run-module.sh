#!/usr/bin/env bash
# The entry point of every module in this suite.
#
# Each modules/<id>/run.sh is a symlink to this file.  There is one script rather
# than fifteen copies of it because every module in this task runs the same engine
# over a different slice of the case catalog -- the slice is named by
# $SRB_MODULE_ID and defined in lib/driver.py, so a module that needed its own
# script would be a module whose subject the driver does not understand.
#
# If a future module does need its own logic, it replaces its symlink with a real
# script and nothing else changes: the runner only cares that run.sh exists and
# writes $SRB_RESULT.
set -euo pipefail

: "${SRB_MODULE_ID:?the behavioural runner sets this}"
: "${SRB_SUITE_DIR:?the behavioural runner sets this}"
: "${SRB_RESULT:?the behavioural runner sets this}"

# -I: isolated mode.  The engine must not pick up a module from a user site
# directory or from anywhere a submission could have written.  -I also implies -P,
# which drops the script's own directory from sys.path, so driver.py puts that
# directory back itself -- every entry point in lib/ does the same, for the same
# reason.
exec python3 -I "${SRB_SUITE_DIR}/lib/driver.py" --module "${SRB_MODULE_ID}"
