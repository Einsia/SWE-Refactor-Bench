#!/bin/bash
# The CLI surface, measured against the reference.
#
# 481 cases over 167 operations: the ten output modes, the dot commands, the
# option vector, .import/.dump/.read round trips, .backup, .clone, error
# reporting, and the exit statuses.
#
# shell.c is the file a platform port has to touch most after the VFS -- it is
# where popen, system and isatty live -- so it is the file most likely to be
# broken by accident on the way past. Kept as its own module so that a submission
# with a working engine and a mangled shell scores like one.
set -uo pipefail

# The module id is read from $SRB_MODULE_ID by run-cases.py, which is why the five
# case modules are byte-identical below this line.
exec python3 "$SRB_SUITE_DIR/lib/run-cases.py"
