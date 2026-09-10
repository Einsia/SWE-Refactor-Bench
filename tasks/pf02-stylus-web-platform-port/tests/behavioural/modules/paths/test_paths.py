"""POSIX path algebra: `src/core`'s own arithmetic against `node:path.posix`.

State A calls `path.join` in 72 places, and the port cannot import `node:path`.
Reimplementing it is required and explicitly not a shim (§2.3) -- it is pure
computation.  It is also the part of the port most likely to be almost right:
`extname('.hidden')` is `''`, `join()` is `'.'`, `relative('a','')` is `'..'`,
and a reimplementation that guesses any of those produces subtly wrong `url()`
rewriting and sourcemap paths.

Expectations come from `node:path.posix` running in the oracle container, so
this suite encodes no answers of its own.
"""
from __future__ import annotations

import pytest

from srbstylus import require_core
from harness import pathvectors as pv
from harness import runners, vfs

VECTORS = list(pv.vectors())


@pytest.fixture(scope="module")
def expected() -> dict[str, str]:
    batch = runners.oracle([{"id": "p", "kind": "pathAlgebra", "vectors": VECTORS}])
    r = batch.results["p"]
    assert r.ok, f"oracle could not answer the path vectors: {r.error}"
    return r.value["values"]


@pytest.fixture(scope="module")
def actual():
    require_core()
    batch = runners.sandbox(
        [{"id": "p", "kind": "pathAlgebra", "vectors": VECTORS}], files=vfs.full(), sync=False
    )
    return batch


def test_core_exports_path_helpers(actual):
    """§1.1/§1.2: the algebra is reachable as `path` on the default export.

    The one row in this module about a surface rather than about arithmetic.  The
    359 rows below are answered on either face -- they ask whether the tree's path
    arithmetic agrees with `node:path.posix`, and before the port it agrees by
    being it, which is the agreement §1.2 asks the port to preserve.  This one is
    not: §1.1 introduces the export, so a tree that has not been ported has not got
    it, and this row is where that shows up as a number.

    Asked of every tree, including the pre-migration face, and charged there.  The
    grading rule is that a task asks every submission the same questions, so a
    module whose size depended on how far the submission got would be measuring
    two different things and calling them one number.  The row stays and the
    weight stays: the export is what §1.1 asks for, not having produced it is the
    answer, and the migration is the task.
    """
    r = actual.results["p"]
    assert r.ok, f"the pathAlgebra probe failed to run: {r.error}"
    assert "unavailable" not in r.value, (
        "src/core does not export its path helpers. §1.1 lists `path` on the "
        "default export and §1.2 requires the algebra to live in the core, so a "
        "host can share exactly the arithmetic the compiler uses."
    )


@pytest.mark.parametrize("vid", pv.ids())
def test_vector_matches_node_path_posix(vid: str, expected: dict, actual):
    r = actual.results["p"]
    if not r.ok:
        pytest.fail(f"the pathAlgebra probe failed: {r.error}")
    if "unavailable" in r.value:
        pytest.fail(f"no path helpers exported from src/core: {r.value['unavailable']}")

    want = expected[vid]
    got = r.value["values"].get(vid, "<missing>")
    assert got == want, (
        f"{pv.describe(vid)}\n"
        f"  node:path.posix -> {want!r}\n"
        f"  src/core        -> {got!r}"
    )
