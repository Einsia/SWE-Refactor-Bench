"""The same corpus through `src/node`, on the synchronous path.

This is the compatibility half of the port.  Existing Node users call
`stylus(str).render()` and get a String back; §1.5 keeps that promise, which
means the async core has to be drivable synchronously.  A submission that only
ever returns a Promise here loses this dimension even when the CSS is right --
`wasPromise` is recorded rather than quietly awaited away.
"""
from __future__ import annotations

import pytest

from harness import cases, layout, runners
from srbstylus import permitted_skip


def _require_adapter():
    """Fail when the tree has no Node-facing API at all -- in either shape.

    `layout.face()` decides which one that is: §1.5's `src/node/index.js` once the
    port exists, the package's own CommonJS entry before it.  A tree with neither
    has nothing for the upstream corpus to be compiled by, and that is the failure
    this dimension exists to catch.
    """
    if not layout.node_face_available():
        pytest.fail(
            f"there is no Node-facing entry point: neither {layout.NODE_ENTRY}, which "
            f"instruction.md §1.5 makes the entry point existing Node users get, nor "
            f"{layout.LEGACY_ENTRY}, the one it replaces."
        )


@pytest.fixture(scope="module")
def sweep():
    _require_adapter()
    ops = [cases.op_for(c) for c in cases.cases()]
    return runners.node_adapter(ops)


@pytest.fixture(scope="module")
def expected():
    ops = [cases.op_for(c) for c in cases.cases()]
    return runners.oracle(ops)


def test_adapter_imports(sweep):
    assert sweep.load_error is None, (
        "src/node/index.js failed to import:\n"
        f"{sweep.load_error.get('name')}: {sweep.load_error.get('message')}"
    )


@pytest.mark.parametrize("name", cases.case_names())
def test_case_matches_state_a(name, sweep, expected):
    exp = expected.get(name)
    if not exp.ok:
        permitted_skip(f"oracle could not compile {name}")

    got = sweep.get(name)
    assert got.ok, (
        f"{name}: src/node raised where State A succeeded\n"
        f"  {got.error.get('name')}: {got.error.get('message')}"
    )
    assert got.css == exp.css, (
        f"{name}: CSS from src/node differs from State A "
        f"({len(exp.css)} vs {len(got.css or '')} bytes)"
    )


def test_render_is_synchronous(sweep):
    """§1.5: `render()` returns a String, not a Promise."""
    promised = [
        name for name, r in sweep.results.items()
        if r.ok and r.value and r.value.get("wasPromise")
    ]
    assert not promised, (
        f"{len(promised)} of {len(sweep.results)} renders returned a Promise from "
        "the Node adapter. State A's render() is synchronous and §1.5 preserves "
        f"that. First few: {promised[:5]}"
    )
