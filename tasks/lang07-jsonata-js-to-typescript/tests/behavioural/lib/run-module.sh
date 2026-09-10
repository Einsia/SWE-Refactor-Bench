#!/usr/bin/env bash
# The entry point of every module in this suite.
#
# Each modules/<id>/run.sh is a symlink to this file.  There is one script rather
# than sixteen copies because every module runs the same engine over a different
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
# directory or from anywhere the submission could have written.  A submission
# ships a package.json and may ship anything else; nothing it writes should be
# importable by the program grading it.
#
# -I implies -E and -P: PYTHONPATH is ignored and the script's own directory is
# dropped from sys.path.  Both matter here.  Losing PYTHONPATH costs nothing
# because no file in lib/ imports the infra package.  Losing the script directory
# would break every `import catalog` in the engine, so driver.py puts its own
# directory back before importing its siblings -- if that line is ever removed,
# every module in this suite fails to start and the stage reports a dead verifier
# rather than a bad submission.
exec python3 -I "${SRB_SUITE_DIR}/lib/driver.py" --module "${SRB_MODULE_ID}"
