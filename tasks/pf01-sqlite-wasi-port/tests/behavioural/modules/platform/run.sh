#!/bin/bash
# The WASI contract: the behaviours the reference cannot demonstrate.
#
# 78 cases over 32 operations, and the only module in the suite whose
# expectations are written down rather than measured -- because native has pipes,
# dlopen, permission bits and an unrestricted filesystem, so asking it what
# `.shell echo hi` should print gets an answer that is right for Linux and
# meaningless here.
#
# Of 52 candidates probed on both targets, 48 agreed and were moved into the
# differential modules. What is left is the set where WASI genuinely removes
# something, upstream provides the porting macro, or this benchmark pins a name
# the source leaves open. lib/cases_platform.py argues each one, prints the table of
# what was probed, and records what native answered beside every literal.
set -uo pipefail

# The module id is read from $SRB_MODULE_ID by run-cases.py, which is why the five
# case modules are byte-identical below this line.
exec python3 "$SRB_SUITE_DIR/lib/run-cases.py"
