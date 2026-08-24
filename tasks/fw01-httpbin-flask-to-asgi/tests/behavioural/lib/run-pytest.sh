#!/usr/bin/env bash
# Run one pytest module against the interpreter the `install` module built.
#
# Every module's run.sh is one line calling this.  The module contract is in the
# environment, so nothing here is task-specific except the venv path:
#
#   $SRB_SUITE_WORK/venv    built by the `install` module from the submission's
#                           own source, offline, against the grader's wheelhouse
#
# That venv is the reason `install` is declared first and `required = true`: it
# is the only thing shared between modules, and a suite that cannot install the
# submission has nothing behavioural left to measure.  Falling back to the image
# venv would hide exactly that, so a missing venv is an error here.
set -uo pipefail

PY="${SRB_SUITE_WORK:?}/venv/bin/python"
if [ ! -x "$PY" ]; then
    echo "no interpreter at $PY -- the install module did not run or did not" >&2
    echo "finish; this module cannot report on the submission." >&2
    exit 1
fi

exec "$PY" -m pytest \
    -c "${SRB_SUITE_DIR:?}/pytest.ini" \
    -p srbfixtures \
    -p swerefactor.pytest_module \
    --junit-xml="${SRB_WORK:?}/junit.xml" \
    "${SRB_MODULE_DIR:?}" \
    "$@"
