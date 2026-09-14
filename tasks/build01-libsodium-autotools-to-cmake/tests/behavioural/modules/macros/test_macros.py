#!/usr/bin/env python3
"""Behavioural: the same macro set reaches every library translation unit.

Contract §1.4. There is no config.h in this project — `configure` puts all 89
macros on the compile line, and `private/common.h` refuses to compile unless
`CONFIGURED` is 1. So the compile database *is* the interface being checked.
"""
import json
import os

import pytest

pytestmark = pytest.mark.behaviour

# data/ and lib/ live at the suite root, one level above modules/.  The
# runner exports SRB_SUITE_DIR; the fallback keeps a module runnable by hand.
SUITE = os.environ.get("SRB_SUITE_DIR") or os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
with open(os.path.join(SUITE, "data", "macros.json")) as _fh:
    MACROS = json.load(_fh)

REQUIRED = MACROS["required"]          # 64, presence and value checked here
OPTIONAL = MACROS["optional"]          # 25, value checked in one check
MINIMAL_EXTRA = MACROS["minimal_extra"]  # ["MINIMAL"]


def macro_value(v):
    """A macro value with shell escaping removed, so two spellings compare equal.

    State A's compile line carries `-DPACKAGE_STRING="\\"libsodium\\ 1.0.20\\""`:
    automake escapes the space because the value passes through a shell on its way
    to the compiler. The compiler then reads `\\ ` inside a string literal as the
    unknown escape `\\040` and resolves it to a space -- verified by compiling both
    spellings and comparing the resulting binaries, which hold the identical
    string. So `libsodium\\ 1.0.20` and `libsodium 1.0.20` are the same macro, and
    a CMake build that writes the unescaped form has not changed the value.

    Applied to the recorded value and the observed one alike. Normalising only the
    observation would make State A disagree with its own ground truth, which is
    where this value was recorded from.
    """
    return v.replace("\\ ", " ") if isinstance(v, str) else v


@pytest.fixture(scope="session")
def macros(b_default):
    if not b_default.built:
        pytest.fail("default build failed:\n%s" % b_default.failure_summary())
    m = b_default.macro_map()
    if not m:
        pytest.fail(
            "no macros could be read from the build. Either no compile database "
            "was produced or no library translation unit was compiled.")
    return m


@pytest.fixture(scope="session")
def macros_minimal(b_minimal):
    if not b_minimal.built:
        pytest.fail("minimal build failed:\n%s" % b_minimal.failure_summary())
    return b_minimal.macro_map()


@pytest.mark.parametrize("macro", sorted(REQUIRED))
def test_required_macro_defined(macros, macro):
    assert macro in macros, (
        "%s is not defined for library translation units. State A's configure "
        "defines it; §1.4 requires the same macro set." % macro)


@pytest.mark.parametrize("macro", sorted(REQUIRED))
def test_required_macro_value(macros, macro):
    if macro not in macros:
        pytest.fail("%s is not defined" % macro)
    want = macro_value(REQUIRED[macro])
    got = macro_value(macros[macro])
    assert got == want, (
        "%s is defined as %r but State A defines it as %r (§1.4)"
        % (macro, got, want))


def test_configured_macro_is_one(macros):
    """private/common.h hard-errors unless CONFIGURED == 1."""
    assert macros.get("CONFIGURED") == "1", (
        "CONFIGURED=%r; src/libsodium/include/sodium/private/common.h refuses "
        "to compile without CONFIGURED=1" % macros.get("CONFIGURED"))


def test_macro_set_is_uniform_across_translation_units(b_default):
    """§1.4: 'the same macro set, to every library translation unit'."""
    per = b_default.macros_per_source()
    if not per:
        pytest.fail("no compile database")
    required = set(REQUIRED)
    offenders = {}
    for src, m in sorted(per.items()):
        missing = sorted(required - set(m))
        if missing:
            offenders[src] = missing
    assert not offenders, (
        "%d translation unit(s) were compiled without the full macro set, e.g. "
        "%s missing %s (§1.4)"
        % (len(offenders), next(iter(offenders)),
           offenders[next(iter(offenders))][:6]))


@pytest.mark.parametrize("macro", sorted(OPTIONAL))
def test_optional_macro_value_when_present(macros, macro):
    """PACKAGE_*/VERSION/portability macros carry State A's value (§1.4)."""
    if macro not in macros:
        pytest.skip(
            "%s is not defined for library translation units. State A's "
            "configure defines it; §1.4 requires the same macro set." % macro)
    want = macro_value(OPTIONAL[macro])
    got = macro_value(macros[macro])
    assert got == want, (
        "%s is defined as %r; State A defines it as %r" % (macro, got, want))


def test_minimal_defines_minimal_macro(macros_minimal):
    for m in MINIMAL_EXTRA:
        assert m in macros_minimal, (
            "-DSODIUM_MINIMAL=ON must define %s, the macro State A's "
            "--enable-minimal defines (§1.2)" % m)


def test_default_does_not_define_minimal(macros):
    for m in MINIMAL_EXTRA:
        assert m not in macros, (
            "%s is defined in a default build; it belongs to the minimal "
            "configuration only" % m)


def test_minimal_keeps_the_required_macros(macros_minimal):
    missing = sorted(set(REQUIRED) - set(macros_minimal))
    assert missing == [], (
        "the minimal configuration lost %d platform macros: %s"
        % (len(missing), missing[:10]))


# Whether a `config.h` appears in the delivered tree is a statement about the
# repository, so the scan makes it and this module does not. The stronger form -- a
# config.h the *build* writes into the sources, which is what actually breaks a
# second build -- is `configure`'s out-of-source check.
#
# What stays in this module is the thing that matters about State A having no
# AC_CONFIG_HEADERS: the macros have to reach the compile line. Every check above
# reads them off the compile database, and a submission is free to generate a
# config.h in its build tree as long as they do.


def test_macro_count_is_plausible(macros):
    """A build delivering a handful of macros has not ported the probes."""
    assert len(macros) >= len(REQUIRED), (
        "only %d macros reach the compile line; State A delivers %d required "
        "plus %d optional" % (len(macros), len(REQUIRED), len(OPTIONAL)))


def test_generator_agnostic_macro_set(b_default, b_make):
    """§1.1: the two generators must produce the same configuration."""
    if not b_make.built:
        pytest.fail("Unix Makefiles build failed:\n%s" % b_make.failure_summary())
    a = {k: v for k, v in b_default.macro_map().items() if k in REQUIRED}
    b = {k: v for k, v in b_make.macro_map().items() if k in REQUIRED}
    assert a == b, (
        "Ninja and Unix Makefiles disagree about the macro set: %s"
        % sorted(set(a.items()) ^ set(b.items()))[:8])
