#!/usr/bin/env bash
# Run one shared test file as this module's pytest run.
#
#   run-pytest.sh test_screens.py
#
# Every measuring module's run.sh is one line calling this. Nine modules share
# three test files: seven ask the same question of one scenario group each, and
# two ask a cross-cutting one of all 100. Which group a module sees comes from
# $SRB_GROUP, declared per module in suite.toml, so the file is identical for all
# seven and there is one place to fix a comparison bug rather than seven.
#
# What the module depends on, and the reason `build` is declared first and
# required: both observation files under $SRB_SUITE_WORK. `build` installs the
# submission offline, builds it, and drives the same 100 scenarios through the
# same driver against both bundles under one nonce. Without that pair there is
# nothing here to compare, so a missing file is an error rather than a fallback.
set -uo pipefail

TEST_FILE="${1:?usage: run-pytest.sh <test file in lib/tests>}"
SUITE="${SRB_SUITE_DIR:?}"
TESTS="$SUITE/lib/tests"

if [ ! -f "$TESTS/$TEST_FILE" ]; then
    echo "no such shared test file: $TESTS/$TEST_FILE" >&2
    exit 1
fi

for side in reference candidate; do
    if [ ! -f "${SRB_SUITE_WORK:?}/$side-observation.json" ]; then
        echo "no $side observation in $SRB_SUITE_WORK -- the build module did" >&2
        echo "not run or did not finish; this module cannot report on the" >&2
        echo "submission." >&2
        exit 1
    fi
done

# rootdir at lib/tests keeps the check ids short: the suite already namespaces
# every check with the module id, so `home/test_screens.py::test_node[...]` says
# what a longer path would.
exec python3 -m pytest \
    -c "$SUITE/pytest.ini" \
    --rootdir "$TESTS" \
    -p srbobserve \
    -p swerefactor.pytest_module \
    --junit-xml="${SRB_WORK:?}/junit.xml" \
    "$TESTS/$TEST_FILE" \
    "${@:2}"
