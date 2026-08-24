#!/usr/bin/env bash
# Run one pytest module against what the `build` module produced.
#
# Every module's run.sh is one line calling this. The module contract arrives in
# the environment, so nothing here is task-specific except the ledger path:
#
#   $SRB_SUITE_WORK/builds.json   written by the `build` module; names every
#                                 configuration, its wheel, its install tree and
#                                 its log
#
# That ledger is why `build` is declared first and required. A missing one is not
# fatal here: the run proceeds and every check that needed a tree fails naming
# the configuration it wanted, which is a truthful report of a submission whose
# build never finished. Refusing to start would produce no checks at all, and a
# module with no checks is a harness error rather than a score.
set -uo pipefail

LEDGER="${SRB_BUILD_LEDGER:-${SRB_SUITE_WORK:?}/builds.json}"
if [ ! -f "$LEDGER" ]; then
    echo "no build ledger at $LEDGER -- the build module did not finish;" >&2
    echo "checks needing a wheel will fail rather than be skipped." >&2
fi

exec python3 -m pytest \
    -c "${SRB_SUITE_DIR:?}/pytest.ini" \
    -p srbinputs \
    -p swerefactor.pytest_module \
    --junit-xml="${SRB_WORK:?}/junit.xml" \
    "${SRB_MODULE_DIR:?}" \
    "$@"
