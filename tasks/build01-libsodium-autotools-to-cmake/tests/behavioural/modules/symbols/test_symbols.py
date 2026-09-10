#!/usr/bin/env python3
"""Behavioural: the ABI is unchanged, symbol by symbol.

Contract §3. A consumer built against State A must keep working against State B,
which means every one of the 650 dynamic symbols is still exported, none of the
130 symbols that `--enable-minimal` removes leaks back in, and the static
archive still carries the whole library.
"""
import json
import os

import pytest

pytestmark = pytest.mark.behaviour

# data/ and lib/ live at the suite root, one level above modules/.  The
# runner exports SRB_SUITE_DIR; the fallback keeps a module runnable by hand.
SUITE = os.environ.get("SRB_SUITE_DIR") or os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import elfutil  # noqa: E402

with open(os.path.join(SUITE, "data", "symbols.json")) as _fh:
    SYM = json.load(_fh)
with open(os.path.join(SUITE, "data", "baseline.json")) as _fh:
    BASE = json.load(_fh)

DEFAULT = SYM["default"]                # 650 exported in a default build
MINIMAL = SYM["minimal"]                # 520 exported with --enable-minimal
MINIMAL_REMOVED = SYM["minimal_removed"]  # 130 that must disappear
PUBLIC = SYM["public_declared"]         # 644 declared SODIUM_EXPORT
ARCHIVE = SYM["archive"]                # 757 in libsodium.a
REALNAME = BASE["solib_realname"]


@pytest.fixture(scope="session")
def exports(b_default):
    if not b_default.installed:
        pytest.fail("default build did not install:\n%s"
                    % b_default.failure_summary())
    p = os.path.join(b_default.libdir(), REALNAME)
    if not os.path.isfile(p):
        pytest.fail("%s was not installed, so the ABI cannot be checked" % REALNAME)
    s = elfutil.dynamic_exports(p)
    if not s:
        pytest.fail("no dynamic symbols found in %s" % p)
    return s


@pytest.fixture(scope="session")
def exports_minimal(b_minimal):
    if not b_minimal.installed:
        pytest.fail("minimal build did not install:\n%s"
                    % b_minimal.failure_summary())
    p = os.path.join(b_minimal.libdir(), REALNAME)
    if not os.path.isfile(p):
        pytest.fail("%s missing from the minimal install" % REALNAME)
    return elfutil.dynamic_exports(p)


@pytest.fixture(scope="session")
def archive_symbols(b_default):
    if not b_default.installed:
        pytest.fail(b_default.failure_summary())
    p = b_default.static_lib()
    if not p:
        pytest.fail("libsodium.a was not installed")
    return elfutil.archive_defined_symbols(p)


# ------------------------------------------------------ exported symbol set --
@pytest.mark.parametrize("symbol", DEFAULT)
def test_symbol_exported(exports, symbol):
    assert symbol in exports, (
        "%s is not exported by the shared library; State A exports it, and a "
        "consumer linked against State A would fail to resolve it (§3)" % symbol)


@pytest.mark.parametrize("symbol", PUBLIC)
def test_public_api_symbol_exported(exports, symbol):
    """Declared SODIUM_EXPORT in a public header, therefore part of the API."""
    assert symbol in exports, (
        "public API function %s is declared SODIUM_EXPORT in a public header but "
        "is not exported by the built library (§3)" % symbol)


def test_no_extra_symbols_exported(exports):
    """Visibility must stay as it is: -fvisibility=hidden plus explicit exports."""
    extra = sorted(s for s in exports - set(DEFAULT)
                   if not s.startswith(("_ITM_", "__gnu", "_fini", "_init",
                                        "__bss_start", "_edata", "_end")))
    assert extra == [], (
        "%d symbol(s) are exported that State A hides: %s — the migration lost "
        "-fvisibility=hidden or the export control (§3)" % (len(extra), extra[:15]))


def test_exported_symbol_count(exports):
    assert len(exports & set(DEFAULT)) == len(DEFAULT), (
        "%d of State A's %d exported symbols are present"
        % (len(exports & set(DEFAULT)), len(DEFAULT)))


# -------------------------------------------------------- minimal build ABI --
@pytest.mark.parametrize("symbol", MINIMAL_REMOVED)
def test_minimal_removes_symbol(exports_minimal, symbol):
    assert symbol not in exports_minimal, (
        "%s is still exported with SODIUM_MINIMAL=ON; State A's "
        "--enable-minimal removes it (§1.2)" % symbol)


@pytest.mark.parametrize("symbol", MINIMAL)
def test_minimal_retains_symbol(exports_minimal, symbol):
    assert symbol in exports_minimal, (
        "%s disappeared from the minimal build; State A's --enable-minimal keeps "
        "it (§1.2)" % symbol)


def test_minimal_symbol_count(exports_minimal):
    got = len(exports_minimal & set(MINIMAL))
    assert got == len(MINIMAL), (
        "the minimal build exports %d of State A's %d minimal symbols"
        % (got, len(MINIMAL)))


def test_minimal_is_a_strict_subset(exports, exports_minimal):
    extra = sorted((exports_minimal & set(DEFAULT)) - (exports & set(DEFAULT)))
    assert extra == [], (
        "the minimal build exports symbols the default build does not: %s"
        % extra[:10])


# ------------------------------------------------------------ static archive --
@pytest.mark.parametrize("symbol", [s for s in ARCHIVE if not s.startswith("_")])
def test_archive_defines_symbol(archive_symbols, symbol):
    assert symbol in archive_symbols, (
        "libsodium.a does not define %s; State A's archive does (§1.3)" % symbol)


def test_archive_member_count(b_default):
    if not b_default.installed:
        pytest.fail(b_default.failure_summary())
    p = b_default.static_lib()
    if not p:
        pytest.fail("libsodium.a missing")
    members = elfutil.archive_members(p)
    assert len(members) >= 119, (
        "libsodium.a holds %d object files; State A compiles 119 translation "
        "units into it" % len(members))


def test_archive_and_shared_agree_on_public_api(archive_symbols, exports):
    missing = sorted(set(PUBLIC) - archive_symbols)
    assert missing == [], (
        "%d public API functions are missing from libsodium.a: %s"
        % (len(missing), missing[:10]))


# ------------------------------------------------------------- link identity --
def test_shared_library_needed_entries(b_default):
    if not b_default.installed:
        pytest.fail(b_default.failure_summary())
    p = os.path.join(b_default.libdir(), REALNAME)
    if not os.path.isfile(p):
        pytest.fail("%s missing" % REALNAME)
    needed = elfutil.needed(p)
    assert any(n.startswith("libc.so") for n in needed), (
        "the shared library does not link against libc: NEEDED=%s" % needed)
    unexpected = [n for n in needed
                  if not n.startswith(("libc.so", "libpthread.so", "libm.so",
                                       "ld-linux", "libgcc_s.so", "libdl.so",
                                       "librt.so"))]
    assert unexpected == [], (
        "the shared library gained dependencies State A does not have: %s"
        % unexpected)


def test_no_undefined_symbols_beyond_state_a(b_default):
    if not b_default.installed:
        pytest.fail(b_default.failure_summary())
    p = os.path.join(b_default.libdir(), REALNAME)
    if not os.path.isfile(p):
        pytest.fail("%s missing" % REALNAME)
    undef = elfutil.dynamic_undefined(p)
    known = set(SYM["undefined_default"])
    extra = sorted(u for u in undef - known if not u.startswith("__"))
    assert extra == [], (
        "the library needs symbols State A does not: %s — something is being "
        "linked in that should not be" % extra[:12])
