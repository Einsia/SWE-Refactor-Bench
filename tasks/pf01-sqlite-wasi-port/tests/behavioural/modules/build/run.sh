#!/bin/bash
# Build the submission, once, and publish where it landed.
#
# This module runs first: everything else in the suite compares two binaries, and
# without this one there is only the reference.  It is also the only module that
# compiles anything.  Its failure needs no veto -- it costs this module's own weight
# in the rate, the comparisons downstream then fail on their own evidence, having
# only one binary to look at, and the stage pays only for a submission that passed
# every scored check in every weighted module.
#
# What it does NOT do is decide whether the port is any good.  It answers exactly
# three questions -- did the declared build script exit 0, did it produce the
# declared artefact, and is that artefact loadable -- and leaves every question
# about behaviour to the modules that run the thing.
#
# The reference is not built here.  It was built when this image was built, from
# the same State A payload the agent was handed, and its provenance is recorded at
# /opt/reference/provenance.json.  See lib/builds.py on why that is not a run-time
# job: a reference that fails to build on grading day would otherwise be reported
# as a submission that fails every comparison.
set -uo pipefail

exec python3 "$SRB_SUITE_DIR/lib/do-build.py"
