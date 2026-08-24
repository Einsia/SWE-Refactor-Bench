#!/bin/bash
# The SQL language, measured against the reference.
#
# 1,616 cases over 328 operations: expressions, the numeric tower, string and
# date functions, collation, comparison and cast rules, aggregates, window
# functions, joins, CTEs, recursive queries, DDL, triggers, views, UPSERT,
# generated columns, the pragma surface, and the error messages for all of it.
#
# Nothing here should change under a platform port, and that is exactly why it is
# the heaviest module: the engine is 90% of what SQLite is, it runs entirely in
# memory, and a port that broke it broke something it had no business touching.
# A 32-bit target with a different long-double width is the interesting risk, and
# the numeric cases are where it shows up.
set -uo pipefail

# The module id is read from $SRB_MODULE_ID by run-cases.py, which is why the five
# case modules are byte-identical below this line.
exec python3 "$SRB_SUITE_DIR/lib/run-cases.py"
