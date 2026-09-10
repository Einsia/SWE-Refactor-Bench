#!/bin/bash
# The extensions this release builds, measured against the reference.
#
# 348 cases over 119 operations: FTS3/4 and FTS5 including the ranking
# functions, RTREE, JSON1, dbstat, the stmt vtab, .archive, fsdir, and the
# generate_series/completion helpers the shell registers.
#
# These are the parts of the tree most likely to be quietly dropped from a port:
# each is behind its own SQLITE_ENABLE_ flag, each adds work to the build, and a
# tree that never defines the flag builds cleanly and passes any test that does
# not ask. So the cases ask.
set -uo pipefail

# The module id is read from $SRB_MODULE_ID by run-cases.py, which is why the five
# case modules are byte-identical below this line.
exec python3 "$SRB_SUITE_DIR/lib/run-cases.py"
