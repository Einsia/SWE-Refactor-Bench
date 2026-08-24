"""The 356-case corpus through `src/core`, inside the web realm.

The heaviest dimension, and the one that says whether the port actually happened:
if these pass, a Stylus that never touches Node compiles the whole upstream
corpus to the same bytes upstream does.

One driver process compiles the whole sweep; the parametrised tests then compare
one case each, so a single broken case costs one check rather than the family.
"""
from __future__ import annotations

import pytest

from harness import cases, layout, runners
from srbstylus import permitted_skip, require_core


@pytest.fixture(scope="module")
def sweep():
    require_core()
    ops = [cases.op_for(c) for c in cases.cases()]
    return runners.sandbox(ops)


@pytest.fixture(scope="module")
def expected():
    ops = [cases.op_for(c) for c in cases.cases()]
    return runners.oracle(ops)


def test_core_module_graph_loads(sweep):
    """Nothing else in this file can pass if the graph did not link."""
    assert sweep.load_error is None, (
        "src/core failed to load in the web realm:\n"
        f"{sweep.load_error.get('name')}: {sweep.load_error.get('message')}"
    )
    assert sweep.module_count > 0


@pytest.mark.parametrize("name", cases.case_names())
def test_case_matches_state_a(name, sweep, expected):
    exp = expected.get(name)
    if not exp.ok:
        permitted_skip(f"oracle could not compile {name}: {exp.error}")

    got = sweep.get(name)
    assert got.ok, (
        f"{name}: src/core raised where State A succeeded\n"
        f"  {got.error.get('name')}: {got.error.get('message')}"
    )
    assert got.css == exp.css, _diff(name, exp.css, got.css)


def _diff(name: str, want: str, got: str) -> str:
    import difflib

    if got is None:
        return f"{name}: no CSS returned"
    delta = list(
        difflib.unified_diff(
            want.splitlines(), got.splitlines(),
            fromfile="state-a", tofile="src/core", lineterm="", n=2,
        )
    )
    head = "\n".join(delta[:40])
    return f"{name}: CSS differs from State A ({len(want)} vs {len(got)} bytes)\n{head}"


def test_sweep_used_the_capability_object(sweep):
    """Right answers with an empty read log mean the bytes came from elsewhere.

    Several corpus cases `@import` from disk and every case loads the built-in
    library, so a correct sweep cannot have an empty access log.
    """
    assert sweep.access_log, (
        "src/core produced output without a single call to platform.readFile / "
        "readDir / stat. The bytes did not come through the capability object."
    )
    reads = [e for e in sweep.access_log if e.startswith("readFile")]
    assert len(reads) >= 10, f"only {len(reads)} readFile calls across the whole corpus: {sweep.access_log[:20]}"


def test_builtin_library_was_loaded_from_runtime_root(sweep):
    """§1.4: the built-in `.styl` library comes through platform.readFile."""
    hits = [e for e in sweep.access_log if layout.VRUNTIME in e]
    assert hits, (
        f"nothing was read from platform.runtimeRoot ({layout.VRUNTIME}). "
        "§1.4 requires the built-in .styl library to be loaded through the "
        "capability object rather than found with __dirname."
    )


def test_core_wrote_nothing_to_console(sweep):
    """A port that leaves debug output on changes what a host embeds."""
    noisy = [line for line in sweep.console_log if not line.startswith("debug")]
    assert not noisy, f"src/core wrote to console during a clean compile: {noisy[:10]}"
