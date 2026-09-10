#!/usr/bin/env bash
# Run one scan module.  Every scan module's run.sh is one line calling this.
#
# The interpreter is the image's own, not a venv built from the submission:
# nothing here installs the submission and nothing here imports it.  That is the
# stage-1 contract, and it is the reason this script is four lines while stage 2's
# equivalent has to build both trees before it can ask either one a question.
set -uo pipefail

# Every list a module needs that describes State A comes from the frozen contract
# or from walking /opt/original, never from a literal in a test.  The contract
# ships in this suite's data/ directory, and `swerefactor validate` holds that copy
# byte-identical to the environment's -- so the scan derives its lists from the same
# file the agent's own environment carried.  Exported rather than defaulted in
# srbscan.py so a suite moved to another path keeps working.
export SRB_CONTRACT="${SRB_CONTRACT:-${SRB_SUITE_DIR:?}/data/source-contract.json}"

exec python -m pytest \
    -c "${SRB_SUITE_DIR:?}/pytest.ini" \
    -p srbscan \
    -p swerefactor.pytest_module \
    --junit-xml="${SRB_WORK:?}/junit.xml" \
    "${SRB_MODULE_DIR:?}" \
    "$@"
