"""build01's 25 "optional" macros are required, and the suite says so.

State A's configure defines all 25; §1.4 asks for the same macro set.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.filterwarnings(
    "ignore::pytest.PytestUnknownMarkWarning")

REPO = Path(__file__).resolve().parents[2]
SUITE = REPO / "tasks" / "build01-libsodium-autotools-to-cmake" / "tests" \
    / "behavioural"


@pytest.fixture(scope="module")
def macros_module():
    os.environ["SRB_SUITE_DIR"] = str(SUITE)
    spec = importlib.util.spec_from_file_location(
        "build01_test_macros", SUITE / "modules" / "macros" / "test_macros.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _markers(fn):
    return {m.name for m in getattr(fn, "pytestmark", [])}


def _verdict(module, macro, macros):
    """"pass", "fail", or "skip" -- the raw pytest outcome."""
    try:
        module.test_optional_macro_value_when_present(macros, macro)
    except AssertionError:
        return "fail"
    except BaseException as exc:
        if type(exc).__name__ == "Skipped":
            return "skip"
        raise
    return "pass"


def test_the_check_carries_no_skip_licence(macros_module):
    """Without `srb_skip_ok` the harness rewrites the skip to a failure."""
    assert "srb_skip_ok" not in _markers(
        macros_module.test_optional_macro_value_when_present)


def test_this_module_no_longer_calls_an_omission_permitted(macros_module):
    """The word was the exemption's last trace here."""
    offenders = []
    for path in sorted((SUITE / "modules" / "macros").rglob("*.py")):
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if "permitted)" in line:
                offenders.append(f"{path.relative_to(SUITE)}:{n}")
    assert offenders == [], offenders


#: The nine the reported submission left undefined.
REPORTED_ABSENT = [
    "LT_OBJDIR", "PACKAGE", "PACKAGE_BUGREPORT", "PACKAGE_NAME",
    "PACKAGE_STRING", "PACKAGE_TARNAME", "PACKAGE_URL", "PACKAGE_VERSION",
    "VERSION",
]


@pytest.mark.parametrize("macro", REPORTED_ABSENT)
def test_an_absent_macro_does_not_pass(macros_module, macro):
    assert _verdict(macros_module, macro, {"CONFIGURED": "1"}) != "pass"


def test_no_optional_macro_passes_by_being_absent(macros_module):
    for macro in sorted(macros_module.OPTIONAL):
        assert _verdict(macros_module, macro, {}) != "pass", macro


def test_state_a_s_own_values_pass(macros_module):
    """The 25 are answerable, which is why they are charged."""
    for macro, want in sorted(macros_module.OPTIONAL.items()):
        assert _verdict(macros_module, macro, {macro: want}) == "pass", macro


def test_a_wrong_value_still_fails(macros_module):
    for macro in sorted(macros_module.OPTIONAL):
        assert _verdict(macros_module, macro, {macro: '"wrong"'}) == "fail", macro


def test_the_group_is_still_the_twenty_five_this_is_about(macros_module):
    data = json.loads((SUITE / "data" / "macros.json").read_text())
    assert len(data["optional"]) == 25
    assert len(data["required"]) == 64
    assert set(REPORTED_ABSENT) <= set(data["optional"])
    assert not set(data["required"]) & set(data["optional"])
