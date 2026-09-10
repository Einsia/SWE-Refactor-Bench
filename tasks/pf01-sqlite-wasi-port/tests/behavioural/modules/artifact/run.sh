#!/bin/bash
# What the delivered wasm module can and cannot reach.
#
# The one module in this stage that does not run cases.  It reads the import and
# export sections of the binary the build produced -- not the source -- because
# "this port uses only what WASI offers" is a claim about the set of host functions
# the module is able to call, and no amount of running it can establish a negative
# about inputs nobody tried.
#
# See lib/do-artifact.py for the argument in full.
set -uo pipefail

exec python3 "$SRB_SUITE_DIR/lib/do-artifact.py"
