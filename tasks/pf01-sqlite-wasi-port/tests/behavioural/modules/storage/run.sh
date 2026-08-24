#!/bin/bash
# The on-disk format, measured against the reference.
#
# 130 cases over 71 operations: the database header, page sizes, auto_vacuum and
# incremental vacuum, WAL and its two side files, journal modes including the
# persist/truncate variants, savepoints, ATTACH across files, VACUUM INTO,
# .backup, audit_check, and the byte-for-byte digests of the files the port
# writes.
#
# This is the module the VFS cannot fake. A stub that keeps pages in memory
# satisfies the engine cases; here the file is closed, reopened, digested, and --
# for the native_read cases -- handed to the native reference, which is asked to
# read a database it did not write and get the same answers out.
set -uo pipefail

# The module id is read from $SRB_MODULE_ID by run-cases.py, which is why the five
# case modules are byte-identical below this line.
exec python3 "$SRB_SUITE_DIR/lib/run-cases.py"
