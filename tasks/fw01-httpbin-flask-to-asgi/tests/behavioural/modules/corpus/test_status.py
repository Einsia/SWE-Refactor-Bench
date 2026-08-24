"""Status code of every corpus case must match frozen State A.

One test per case, so a report names exactly which routes regressed instead of
collapsing 437 behaviours into a single red line.
"""

from __future__ import annotations

import pytest

from harness import corpus

pytestmark = pytest.mark.behaviour

IDS = [c["id"] for c in corpus.CASES]


@pytest.mark.parametrize("case_id", IDS)
def test_status(case_id, replay, golden):
    if case_id in replay["errors"]:
        pytest.fail(
            f"request could not be completed: {replay['errors'][case_id]}"
        )
    case = corpus.CASES_BY_ID[case_id]
    got = replay["responses"][case_id]["status"]
    want = golden[case_id]["status"]
    assert got == want, (
        f"{case['method']} {case['path']}"
        f"{'?' + case['query'] if case['query'] else ''}"
        f" -> {got}, State A returned {want}"
        f"{' (' + case['note'] + ')' if case.get('note') else ''}"
    )
