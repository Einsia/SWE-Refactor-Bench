#!/usr/bin/env bash
# Run one scan module.  Every scan module's run.sh is one line calling this.
#
# The interpreter is the image's own, not a venv built from the submission:
# nothing here installs the submission and nothing here imports it.  That is the
# stage-1 contract, and it is the reason this script is four lines while stage 2's
# equivalent has to find the toolchain the build module used.
set -uo pipefail

# The image's interpreter is `python` -- the base is python:3.11-slim, which does
# not install a `python3` name.  A workstation is usually the other way round.
# Resolving both means this script runs by hand outside the image, which is how the
# scan gets exercised against real trees before a build; hard-coding `python` made
# every module exit 127 and, because a module that writes nothing is recorded with
# no checks, the digest then said "the scan produced no findings" -- which reads
# exactly like a clean tree.
PY="$(command -v python || command -v python3)"

exec "$PY" -m pytest \
    -c "${SRB_SUITE_DIR:?}/pytest.ini" \
    -p srbscan \
    -p swerefactor.pytest_module \
    --junit-xml="${SRB_WORK:?}/junit.xml" \
    "${SRB_MODULE_DIR:?}" \
    "$@"
