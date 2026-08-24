#!/usr/bin/env bash
# The entry point of every module in this suite.
#
# Each modules/<id>/run.sh is a symlink to this file.  There is one script rather
# than twenty-two copies of it because every module here runs the same engine over
# a different slice of the case catalog -- the slice is named by $SRB_MODULE_ID and
# defined in lib/catalog.py, so a module needing its own script would be a module
# whose subject the driver does not understand.
#
# If a future module does need its own logic it replaces its symlink with a real
# script and nothing else changes: the runner only requires that run.sh exists and
# that it writes $SRB_RESULT.
set -euo pipefail

: "${SRB_MODULE_ID:?the behavioural runner sets this}"
: "${SRB_SUITE_DIR:?the behavioural runner sets this}"
: "${SRB_RESULT:?the behavioural runner sets this}"

# -I: isolated mode.  The engine must not pick up a module from a user site
# directory, from $PYTHONPATH, or from anywhere the submission could have written
# -- and this suite copies the submission into a scratch tree and runs a compiler
# over it, so "anywhere it could have written" is not hypothetical.
#
# -I also implies -P, which drops the script's own directory from sys.path.  That
# is why driver.py re-inserts it: without that line the first `import catalog`
# fails, all twenty-two modules write an `error` result, and the stage reports a
# dead verifier instead of a graded submission.  The two halves of this belong
# together, so if you change one, read the other.
exec python3 -I "${SRB_SUITE_DIR}/lib/driver.py" --module "${SRB_MODULE_ID}"
