"""`feature_switch()` finds the stack-protector switch, whatever a port calls it.

Runs against caches written to disk: no cmake, no container, no submission.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
LIB = REPO / "tasks" / "build01-libsodium-autotools-to-cmake" / "tests" \
    / "behavioural" / "lib"

NOEXECSTACK_PROBE = "SODIUM_ACCEPTS_NOEXECSTACK:INTERNAL=1"
NOEXECSTACK_OPTION = "SODIUM_ACCEPTS_NOEXECSTACK:BOOL=ON"
SSP_OPTION = "SODIUM_USE_SSP:BOOL=ON"

PATTERNS = (r"SSP", r"STACK_?PROTECT")


@pytest.fixture(scope="module")
def builder():
    sys.path.insert(0, str(LIB))
    try:
        import builder as module
        yield module
    finally:
        sys.path.remove(str(LIB))


def _switch(builder, tmp_path, entries, patterns=PATTERNS):
    """`feature_switch` against a CMake tree whose cache holds `entries`."""
    root = tmp_path / "tree"
    (root / "build").mkdir(parents=True, exist_ok=True)
    (root / "build" / "CMakeCache.txt").write_text(
        "# This is the CMakeCache file.\n"
        "//A comment CMake writes above every entry\n"
        + "\n".join(entries) + "\n",
        encoding="utf-8")
    build = builder.Build("t", root=str(root))
    build._flavour = builder.CMAKE
    return build.feature_switch(*patterns)


def test_the_ssp_option_wins_over_a_probe_that_sorts_before_it(builder, tmp_path):
    """The reported case: both present, and the answer is the switch."""
    got = _switch(builder, tmp_path, [NOEXECSTACK_PROBE, SSP_OPTION])
    assert got == ("SODIUM_USE_SSP", "OFF")


def test_it_wins_even_when_the_probe_is_declared_as_an_option(builder, tmp_path):
    """Ordering carries this one; the cache type is no help."""
    got = _switch(builder, tmp_path, [NOEXECSTACK_OPTION, SSP_OPTION])
    assert got == ("SODIUM_USE_SSP", "OFF")


@pytest.mark.parametrize("name", [
    "SODIUM_USE_SSP",
    "LIBSODIUM_ENABLE_SSP",
    "WITH_SSP",
    "SODIUM_STACK_PROTECTOR",
    "ENABLE_STACKPROTECTOR",
])
def test_the_switch_is_found_whatever_the_port_called_it(builder, tmp_path, name):
    """§1.10 does not name the variable, so neither does the lookup."""
    got = _switch(builder, tmp_path,
                  [NOEXECSTACK_PROBE, "%s:BOOL=ON" % name,
                   "CMAKE_BUILD_TYPE:STRING=Release"])
    assert got == (name, "OFF")


def test_a_capability_probe_alone_is_not_a_switch(builder, tmp_path):
    """Nothing to turn off is a fact about the build system, not a defect."""
    assert _switch(builder, tmp_path, [
        NOEXECSTACK_PROBE,
        "HAVE_STACK_PROTECTOR:INTERNAL=1",
        "CMAKE_C_FLAGS:STRING=",
        "CMAKE_CACHE_MAJOR_VERSION:INTERNAL=3",
    ]) is None


def test_an_empty_cache_is_not_a_switch(builder, tmp_path):
    assert _switch(builder, tmp_path, []) is None


def test_an_unconfigured_tree_is_not_a_switch(builder, tmp_path):
    """No CMakeCache.txt at all -- nothing was configured to read."""
    root = tmp_path / "bare"
    (root / "build").mkdir(parents=True)
    build = builder.Build("t", root=str(root))
    build._flavour = builder.CMAKE
    assert build.feature_switch(*PATTERNS) is None


def test_the_broad_pattern_still_works_on_its_own(builder, tmp_path):
    """A port with no SSP in any name is still found by the second pattern."""
    got = _switch(builder, tmp_path,
                  [NOEXECSTACK_PROBE, "SODIUM_STACK_PROTECT:BOOL=ON"])
    assert got == ("SODIUM_STACK_PROTECT", "OFF")


def test_the_narrowed_pattern_no_longer_matches_the_probe(builder, tmp_path):
    """`STACK` alone described both flags; `STACK_?PROTECT` describes one."""
    assert _switch(builder, tmp_path, [NOEXECSTACK_OPTION]) is None
    assert _switch(builder, tmp_path, [NOEXECSTACK_OPTION],
                   patterns=(r"SSP", r"STACK")) == \
        ("SODIUM_ACCEPTS_NOEXECSTACK", "OFF")


def test_ordering_alone_decides_the_reported_cache(builder, tmp_path):
    """The reported call, unchanged, now answers with the switch."""
    assert _switch(builder, tmp_path, [NOEXECSTACK_OPTION, SSP_OPTION],
                   patterns=(r"SSP", r"STACK")) == ("SODIUM_USE_SSP", "OFF")


def test_the_check_no_longer_licenses_a_missing_switch():
    """A port with no such control has not reproduced the §1.10 capability."""
    source = (REPO / "tasks" / "build01-libsodium-autotools-to-cmake" / "tests"
              / "behavioural" / "modules" / "hardening"
              / "test_hardening.py").read_text()
    body = source.split("def test_ssp_can_be_turned_off")[0]
    decorators = body.rsplit("\n\n\n", 1)[-1]
    assert "srb_skip_ok" not in decorators, decorators
    assert "(permitted)" not in source.split(
        "def test_ssp_can_be_turned_off")[1]
