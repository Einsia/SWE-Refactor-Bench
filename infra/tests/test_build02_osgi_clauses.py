"""build02 grades an OSGi header as clauses, the way its instruction says.

Reordering must pass; every difference the instruction calls graded must fail.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

#: The module under test declares the stage markers its own suite.toml
#: registers, and this root does not: importing it here is not the place to
#: relitigate that, so the resulting warnings are silenced rather than the
#: shared ini file widened for one test.
pytestmark = pytest.mark.filterwarnings(
    "ignore::pytest.PytestUnknownMarkWarning")

REPO = Path(__file__).resolve().parents[2]
SUITE = REPO / "tasks" / "build02-gson-maven-to-gradle" / "tests" / "behavioural"

STATE_A_EXPORT = (
    'com.google.gson;uses:="com.google.gson.reflect,com.google.gson.stream";'
    'version="2.10.1",'
    'com.google.gson.annotations;version="2.10.1",'
    'com.google.gson.reflect;version="2.10.1",'
    'com.google.gson.stream;version="2.10.1"'
)
STATE_A_IMPORT = 'sun.misc;resolution:=optional,com.google.gson.annotations'


@pytest.fixture(scope="module")
def manifest_module():
    """build02's manifest module, imported without a stage image."""
    sys.path.insert(0, str(SUITE / "lib"))
    os.environ["SRB_SUITE_DIR"] = str(SUITE)
    try:
        spec = importlib.util.spec_from_file_location(
            "build02_test_manifest", SUITE / "modules" / "manifest" / "test_manifest.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.path.remove(str(SUITE / "lib"))


class FakeJar:
    """Just the one thing the manifest checks read."""

    def __init__(self, headers):
        self.manifest = dict(headers)


def _check(module, header, produced, want):
    """Run the real check, and say whether it passed."""
    jar = FakeJar({header: produced})
    try:
        module.test_required_header(jar, header, want)
    except AssertionError:
        return False
    return True


# ------------------------------------------------ what must stop being graded
REORDERINGS = {
    "attributes within a clause": (
        'com.google.gson;version="2.10.1";'
        'uses:="com.google.gson.reflect,com.google.gson.stream",'
        'com.google.gson.annotations;version="2.10.1",'
        'com.google.gson.reflect;version="2.10.1",'
        'com.google.gson.stream;version="2.10.1"'
    ),
    "the uses set": (
        'com.google.gson;uses:="com.google.gson.stream,com.google.gson.reflect";'
        'version="2.10.1",'
        'com.google.gson.annotations;version="2.10.1",'
        'com.google.gson.reflect;version="2.10.1",'
        'com.google.gson.stream;version="2.10.1"'
    ),
    "the clauses themselves": (
        'com.google.gson.stream;version="2.10.1",'
        'com.google.gson.reflect;version="2.10.1",'
        'com.google.gson.annotations;version="2.10.1",'
        'com.google.gson;uses:="com.google.gson.reflect,com.google.gson.stream";'
        'version="2.10.1"'
    ),
    "whitespace around the separators": (
        'com.google.gson; uses:="com.google.gson.reflect, com.google.gson.stream"; '
        'version="2.10.1", '
        'com.google.gson.annotations; version="2.10.1", '
        'com.google.gson.reflect; version="2.10.1", '
        'com.google.gson.stream; version="2.10.1"'
    ),
}


@pytest.mark.parametrize("what", sorted(REORDERINGS), ids=sorted(REORDERINGS))
def test_reordering_passes(manifest_module, what):
    """The header means the same thing, so it is the same header."""
    assert _check(manifest_module, "Export-Package",
                  REORDERINGS[what], STATE_A_EXPORT), what


def test_the_reorderings_really_are_reorderings():
    """Each case above differs from State A only in order."""
    for what, produced in REORDERINGS.items():
        assert produced != STATE_A_EXPORT, what
        assert sorted(produced.replace(" ", "")) == \
            sorted(STATE_A_EXPORT.replace(" ", "")), what


# ------------------------------------------------- what must still be graded
DEFECTS = {
    "a dropped uses directive":
        'com.google.gson;version="2.10.1",'
        'com.google.gson.annotations;version="2.10.1",'
        'com.google.gson.reflect;version="2.10.1",'
        'com.google.gson.stream;version="2.10.1"',
    "a uses set with a package missing":
        'com.google.gson;uses:="com.google.gson.reflect";version="2.10.1",'
        'com.google.gson.annotations;version="2.10.1",'
        'com.google.gson.reflect;version="2.10.1",'
        'com.google.gson.stream;version="2.10.1"',
    "a uses set with a package added":
        'com.google.gson;uses:="com.google.gson.reflect,com.google.gson.stream,'
        'com.google.gson.internal";version="2.10.1",'
        'com.google.gson.annotations;version="2.10.1",'
        'com.google.gson.reflect;version="2.10.1",'
        'com.google.gson.stream;version="2.10.1"',
    "a wrong export version":
        'com.google.gson;uses:="com.google.gson.reflect,com.google.gson.stream";'
        'version="2.10.0",'
        'com.google.gson.annotations;version="2.10.1",'
        'com.google.gson.reflect;version="2.10.1",'
        'com.google.gson.stream;version="2.10.1"',
    "a package that is not exported":
        'com.google.gson;uses:="com.google.gson.reflect,com.google.gson.stream";'
        'version="2.10.1",'
        'com.google.gson.annotations;version="2.10.1",'
        'com.google.gson.reflect;version="2.10.1"',
    "an internal package exported as well":
        'com.google.gson;uses:="com.google.gson.reflect,com.google.gson.stream";'
        'version="2.10.1",'
        'com.google.gson.annotations;version="2.10.1",'
        'com.google.gson.reflect;version="2.10.1",'
        'com.google.gson.stream;version="2.10.1",'
        'com.google.gson.internal;version="2.10.1"',
    "a version attribute with no value":
        'com.google.gson;uses:="com.google.gson.reflect,com.google.gson.stream",'
        'com.google.gson.annotations;version="2.10.1",'
        'com.google.gson.reflect;version="2.10.1",'
        'com.google.gson.stream;version="2.10.1"',
}


@pytest.mark.parametrize("what", sorted(DEFECTS), ids=sorted(DEFECTS))
def test_a_real_difference_still_fails(manifest_module, what):
    assert not _check(manifest_module, "Export-Package",
                      DEFECTS[what], STATE_A_EXPORT), what


IMPORT_DEFECTS = {
    "resolution:=optional dropped":
        "sun.misc,com.google.gson.annotations",
    "resolution:=mandatory instead":
        "sun.misc;resolution:=mandatory,com.google.gson.annotations",
    "an import that is not there":
        "sun.misc;resolution:=optional",
}


@pytest.mark.parametrize("what", sorted(IMPORT_DEFECTS), ids=sorted(IMPORT_DEFECTS))
def test_a_real_import_difference_still_fails(manifest_module, what):
    assert not _check(manifest_module, "Import-Package",
                      IMPORT_DEFECTS[what], STATE_A_IMPORT), what


def test_import_clause_order_passes(manifest_module):
    assert _check(manifest_module, "Import-Package",
                  "com.google.gson.annotations,sun.misc;resolution:=optional",
                  STATE_A_IMPORT)


def test_a_directive_is_not_an_attribute(manifest_module):
    """``resolution=optional`` is a different claim from ``resolution:=optional``."""
    assert not _check(manifest_module, "Import-Package",
                      "sun.misc;resolution=optional,com.google.gson.annotations",
                      STATE_A_IMPORT)


# --------------------------------------------------- the other eleven headers
def test_a_scalar_header_is_still_byte_exact(manifest_module):
    """Only the two clause headers moved; the rest are unchanged."""
    header = "Bundle-RequiredExecutionEnvironment"
    want = "JavaSE-1.7, JavaSE-1.8"
    assert _check(manifest_module, header, want, want)
    assert not _check(manifest_module, header, "JavaSE-1.8, JavaSE-1.7", want)
    assert not _check(manifest_module, header, "JavaSE-1.7,JavaSE-1.8", want)


def test_an_absent_header_still_fails(manifest_module):
    assert not _check(manifest_module, "Export-Package", None, STATE_A_EXPORT)


def test_state_a_passes_itself(manifest_module):
    """The ground truth in data/manifest.json agrees with the strings above."""
    ground = manifest_module.MF["required"]
    assert ground["Export-Package"] == STATE_A_EXPORT
    assert ground["Import-Package"] == STATE_A_IMPORT
    for header, want in ground.items():
        assert _check(manifest_module, header, want, want), header
