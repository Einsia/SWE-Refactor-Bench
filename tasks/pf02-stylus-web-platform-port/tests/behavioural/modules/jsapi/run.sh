#!/usr/bin/env bash
exec bash "${SRB_SUITE_DIR:?}/lib/run-pytest.sh" "$@"
