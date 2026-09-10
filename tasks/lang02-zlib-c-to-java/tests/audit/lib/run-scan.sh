#!/usr/bin/env bash
# Run one scan module.  Every scan module's run.sh is one line calling this.
#
# The interpreter is the image's own, not a venv built from the submission:
# nothing here installs the submission and nothing here imports it.  That is the
# stage-1 contract, and it is the reason this script is four lines while stage 2's
# equivalent has to find the venv the install module built.
set -uo pipefail

# Every list a module needs that describes State A comes from the frozen contract
# or from walking /opt/original, never from a literal in a test.  The contract
# ships in this suite's data/ directory, and `swerefactor validate` holds that copy
# byte-identical to the environment's -- so the scan derives its lists from the same
# file the agent's own environment carried.  Exported rather than defaulted in
# srbscan.py so a suite moved to another path keeps working.
export SRB_CONTRACT="${SRB_CONTRACT:-${SRB_SUITE_DIR:?}/data/source-contract.json}"

# Nothing is validated here, and an empty /opt/original in particular is not.  A
# guard at this level exits before pytest, so the module contributes an error *unit*
# and zero *checks* -- and `scan.digest` renders checks, so the reviewer's
# {{findings}} section becomes "(the scan produced no findings)" while all three
# modules are dead.  Measured: `result.status` stayed 'ok' and `scan.summary()`
# reported `errored: 0`, since that counts checks too.  A refusal here reads to a
# reviewer as reassurance.  See `srbscan.authored` for the whole measurement.
exec python -m pytest \
    -c "${SRB_SUITE_DIR:?}/pytest.ini" \
    -p srbscan \
    -p swerefactor.pytest_module \
    --junit-xml="${SRB_WORK:?}/junit.xml" \
    "${SRB_MODULE_DIR:?}" \
    "$@"
