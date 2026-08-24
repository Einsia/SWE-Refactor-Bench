#!/bin/sh
# One module, one pytest process; the shared runner is in lib/.
exec bash "$SRB_SUITE_DIR/lib/run-pytest.sh" "$@"
