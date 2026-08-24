#!/usr/bin/env bash
# The entry point of every module in this suite.
#
# Each modules/<id>/run.sh is a symlink to this file.  There is one script rather
# than twelve copies because every module here asks the same question of a
# different slice of the same measurement: build the submission for three targets
# once, run the programs, and grade a named subset of keys against the answers
# frozen into this image when it was built.  The slice is named by $SRB_MODULE_ID
# and defined in lib/driver.py, so a module that needed its own script would be a
# module whose subject the driver does not understand.
#
# If a future module does need its own logic, it replaces its symlink with a real
# script and nothing else changes: the runner only cares that run.sh exists and
# writes $SRB_RESULT.
set -euo pipefail

: "${SRB_MODULE_ID:?the behavioural runner sets this}"
: "${SRB_SUITE_DIR:?the behavioural runner sets this}"
: "${SRB_RESULT:?the behavioural runner sets this}"

# -I: isolated mode.  The engine must not pick up a module from a user site
# directory or from anywhere a submission could have written, and the driver puts
# its own directory back on sys.path itself.
#
# -B: no bytecode caches.  -I implies -E, so the image's PYTHONDONTWRITEBYTECODE
# does not reach here; without the flag twelve modules race to write the same
# __pycache__ beside targets.py, in a directory grading has no reason to modify.
exec python3 -I -B "${SRB_SUITE_DIR}/lib/driver.py" --module "${SRB_MODULE_ID}"
