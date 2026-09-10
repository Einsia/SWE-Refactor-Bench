#!/usr/bin/env python3
"""Behavioural: the install tree is the one State A produces.

Contract §1.3. State A's tree is the reference, with two deliberate differences:
`libsodium.la` must be gone (it is a libtool artifact) and the CMake package
config must be present (§1.6).

Those two are the only entries here that are not preserved capability -- one has
to disappear and one has to appear -- so they are the two that are not charged
against a delivery that cannot have them: the `.la` check is recorded at weight 0
because it is a removal requirement stage 1 gates on, and the package-directory
checks are gated to a CMake delivery because an Autotools tree has no such
directory. Everything else is State A's tree, required of both.
"""
import json
import os
import re

import pytest

import flavour

pytestmark = pytest.mark.behaviour

# data/ and lib/ live at the suite root, one level above modules/.  The
# runner exports SRB_SUITE_DIR; the fallback keeps a module runnable by hand.
SUITE = os.environ.get("SRB_SUITE_DIR") or os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import elfutil  # noqa: E402

with open(os.path.join(SUITE, "data", "install.json")) as _fh:
    INSTALL = json.load(_fh)
with open(os.path.join(SUITE, "data", "baseline.json")) as _fh:
    BASE = json.load(_fh)

REALNAME = BASE["solib_realname"]     # libsodium.so.26.2.0
SONAME = BASE["solib_soname"]         # libsodium.so.26

CONFIGS = {
    "default": ("b_default", "default"),
    "minimal": ("b_minimal", "minimal"),
    "shared-only": ("b_shared_only", "shared"),
    "static-only": ("b_static_only", "static"),
}

# Everything State A installs except the libtool archive, which §1.3 forbids.
def expected_non_headers(key):
    out = []
    for e in INSTALL[key]:
        p = e["path"]
        if p.startswith("include/sodium/") or p == "include/sodium.h":
            continue
        if os.path.basename(p) == "libsodium.la":
            continue
        out.append((e["kind"], p))
    return sorted(out, key=lambda x: x[1])


CASES = [(cfg, kind, path)
         for cfg, (_fx, key) in sorted(CONFIGS.items())
         for kind, path in expected_non_headers(key)]


def _build(request, cfg):
    fixture = CONFIGS[cfg][0]
    b = request.getfixturevalue(fixture)
    if not b.installed:
        pytest.fail("%s did not install:\n%s" % (cfg, b.failure_summary()))
    return b


def _entries(b):
    return {p: k for k, p in b.install_entries()}


@pytest.mark.parametrize("cfg,kind,path", CASES,
                         ids=["%s:%s" % (c, p) for c, _k, p in CASES])
def test_installed_entry(request, cfg, kind, path):
    b = _build(request, cfg)
    full = os.path.join(b.prefix, path)
    if kind == "d":
        assert os.path.isdir(full), (
            "%s: directory %s was not created by cmake --install (§1.3)"
            % (cfg, path))
    elif kind == "l":
        assert os.path.islink(full), (
            "%s: %s must be a symlink (§1.3)" % (cfg, path))
    else:
        assert os.path.isfile(full) and not os.path.islink(full), (
            "%s: %s must be installed as a regular file (§1.3)" % (cfg, path))


@pytest.mark.srb_weight(0.0)
@pytest.mark.parametrize("cfg", sorted(CONFIGS))
def test_libtool_archive_not_installed(request, cfg):
    """Recorded, not charged: `libsodium.la` is what libtool installs.

    §1.3 forbids it in State B, and it is the one entry in State A's install tree
    that must *disappear* rather than survive. That makes it a removal
    requirement, which `autotools_retired` in stage 1 decides over the whole tree
    with a reviewer; charging for it here would mean the oracle every expectation
    in `data/install.json` was recorded from fails the module those expectations
    make up. Left running because "the submission still ships a .la" is worth
    reading in a report.
    """
    b = _build(request, cfg)
    bad = [p for p in _entries(b) if p.endswith(".la")]
    assert bad == [], (
        "%s installs libtool archive(s) %s; §1.3 forbids libsodium.la in State B"
        % (cfg, bad))


@pytest.mark.parametrize("cfg", ["default", "minimal", "shared-only"])
def test_shared_object_realname(request, cfg):
    b = _build(request, cfg)
    p = os.path.join(b.libdir(), REALNAME)
    assert os.path.isfile(p) and not os.path.islink(p), (
        "%s: the real shared object must be %s (§1.3); the library directory "
        "holds %s" % (cfg, REALNAME,
                      sorted(f for f in os.listdir(b.libdir())
                             if f.startswith("libsodium"))))


@pytest.mark.parametrize("cfg", ["default", "minimal", "shared-only"])
def test_soname_symlink(request, cfg):
    b = _build(request, cfg)
    p = os.path.join(b.libdir(), SONAME)
    assert os.path.islink(p), "%s: %s must be a symlink (§1.3)" % (cfg, SONAME)
    assert os.path.basename(os.readlink(p)) == REALNAME, (
        "%s: %s points at %s, expected %s"
        % (cfg, SONAME, os.readlink(p), REALNAME))


@pytest.mark.parametrize("cfg", ["default", "minimal", "shared-only"])
def test_devel_symlink(request, cfg):
    b = _build(request, cfg)
    p = os.path.join(b.libdir(), "libsodium.so")
    assert os.path.islink(p), "%s: libsodium.so must be a symlink (§1.3)" % cfg
    target = os.path.basename(os.readlink(p))
    assert target in (REALNAME, SONAME), (
        "%s: libsodium.so points at %s, expected %s or %s"
        % (cfg, target, REALNAME, SONAME))


@pytest.mark.parametrize("cfg", ["default", "minimal", "shared-only"])
def test_soname_recorded_in_elf(request, cfg):
    b = _build(request, cfg)
    p = os.path.join(b.libdir(), REALNAME)
    if not os.path.isfile(p):
        pytest.fail("%s: %s missing" % (cfg, REALNAME))
    got = elfutil.soname(p)
    assert got == SONAME, (
        "%s: SONAME is %r, State A's is %r — consumers linked against State A "
        "would not find this library (§1.3, §3)" % (cfg, got, SONAME))


@pytest.mark.parametrize("cfg", ["default", "minimal", "static-only"])
def test_static_archive_installed(request, cfg):
    b = _build(request, cfg)
    p = os.path.join(b.libdir(), "libsodium.a")
    assert os.path.isfile(p), "%s: libsodium.a was not installed (§1.3)" % cfg
    members = elfutil.archive_members(p)
    assert len(members) > 50, (
        "%s: libsodium.a holds only %d members; State A's holds every "
        "translation unit" % (cfg, len(members)))


def test_static_only_installs_no_shared_object(b_static_only):
    if not b_static_only.installed:
        pytest.fail(b_static_only.failure_summary())
    bad = [p for p in _entries(b_static_only) if ".so" in p]
    assert bad == [], (
        "SODIUM_BUILD_SHARED=OFF still installed %s (§1.2)" % bad)


def test_shared_only_installs_no_static_archive(b_shared_only):
    if not b_shared_only.installed:
        pytest.fail(b_shared_only.failure_summary())
    bad = [p for p in _entries(b_shared_only) if p.endswith(".a")]
    assert bad == [], (
        "SODIUM_BUILD_STATIC=OFF still installed %s (§1.2)" % bad)


@pytest.mark.parametrize("cfg", sorted(CONFIGS))
def test_pkgconfig_file_installed(request, cfg):
    b = _build(request, cfg)
    p = b.pc_file()
    assert p and os.path.isfile(p), (
        "%s: lib/pkgconfig/libsodium.pc was not installed (§1.3, §1.7)" % cfg)


@flavour.only(flavour.CMAKE, reason=(
    "$P/lib/cmake/libsodium/ holds what `install(EXPORT)` writes, and an Autotools "
    "tree has no such directory to install. The capability behind §1.6 -- an "
    "outside project can find and link the installed library -- is measured in the "
    "`cmake-package` module against whichever package interface was delivered, and "
    "the .pc file this configuration does install is the check above"))
@pytest.mark.parametrize("cfg", sorted(CONFIGS))
def test_cmake_package_dir_installed(request, cfg):
    b = _build(request, cfg)
    d = b.cmake_package_dir()
    assert d and os.path.isdir(d), (
        "%s: no CMake package config was installed; §1.6 requires "
        "$P/lib/cmake/libsodium/" % cfg)


@flavour.only(flavour.CMAKE, reason=(
    "same directory as the check above: there is no Autotools package config whose "
    "location could be right or wrong"))
@pytest.mark.parametrize("cfg", sorted(CONFIGS))
def test_cmake_package_dir_location(request, cfg):
    b = _build(request, cfg)
    d = b.cmake_package_dir()
    if not d:
        pytest.fail("%s: no CMake package config installed" % cfg)
    rel = os.path.relpath(d, b.prefix).replace(os.sep, "/")
    assert rel == "lib/cmake/libsodium", (
        "%s: the package config is at %s; §1.6 specifies lib/cmake/libsodium"
        % (cfg, rel))


@pytest.mark.parametrize("cfg", sorted(CONFIGS))
def test_no_test_binaries_installed(request, cfg):
    b = _build(request, cfg)
    names = set()
    with open(os.path.join(SUITE, "data", "tests.json")) as fh:
        names = set(json.load(fh)["default"])
    bad = []
    for kind, p in b.install_entries():
        if kind == "d":
            continue
        if os.path.basename(p) in names:
            bad.append(p)
    assert bad == [], (
        "%s: test programs were installed: %s — §1.8 forbids installing them"
        % (cfg, bad[:10]))


@pytest.mark.parametrize("cfg", sorted(CONFIGS))
def test_no_unexpected_files_installed(request, cfg):
    """§1.3: 'Nothing outside the paths above (plus 1.6) may be installed.'

    `lib/libsodium.la` is in the allow-list even though §1.3 forbids it, because
    this check is the *set* comparison -- "did anything unexpected appear" -- and
    the one file State B must stop installing has a check of its own
    (`test_libtool_archive_not_installed`) that names it and says why it is
    recorded rather than charged. Two checks failing over one known difference
    would count it twice.
    """
    b = _build(request, cfg)
    allowed = re.compile(
        r"^(include(/sodium(/.*)?)?|include/sodium\.h"
        r"|lib(64)?|lib(64)?/libsodium\.(a|la|so(\.\d+)*)"
        r"|lib(64)?/pkgconfig(/libsodium\.pc)?"
        r"|lib(64)?/cmake(/libsodium(/.*)?)?"
        r"|share(/.*)?)$")
    unexpected = []
    for kind, p in b.install_entries():
        if allowed.match(p):
            continue
        unexpected.append("%s %s" % (kind, p))
    assert unexpected == [], (
        "%s installed files outside the documented layout (§1.3): %s"
        % (cfg, unexpected[:15]))


@pytest.mark.parametrize("cfg", ["default", "minimal", "shared-only"])
def test_no_static_library_shipped_as_shared(request, cfg):
    """`libsodium.so.26.2.0` is a shared object and not a renamed archive.

    The three configurations that build one, same as the four checks above.  Not
    asked of `static-only`: `SODIUM_BUILD_SHARED=OFF` means there is nothing to
    inspect, which is a question about the configuration rather than the
    submission, and that nothing is what
    `test_static_only_installs_no_shared_object` measures.  A row every submission
    skips is charged 0 rather than left out of the denominator, so it would cost
    all of them a check none of them was asked.
    """
    b = _build(request, cfg)
    p = os.path.join(b.libdir(), REALNAME)
    if not os.path.isfile(p):
        pytest.fail("%s: %s missing" % (cfg, REALNAME))
    assert elfutil.is_pic_shared(p), (
        "%s: %s is not an ELF shared object (%s)"
        % (cfg, REALNAME, elfutil.file_type(p)))


def test_shared_object_has_no_text_relocations(b_default):
    if not b_default.installed:
        pytest.fail(b_default.failure_summary())
    p = os.path.join(b_default.libdir(), REALNAME)
    if not os.path.isfile(p):
        pytest.fail("%s missing" % REALNAME)
    assert not elfutil.has_textrel(p), (
        "the shared object has text relocations, so it was built without -fPIC; "
        "State A's does not")


def test_install_tree_matches_state_a_shape(b_default):
    """Everything State A installs (bar libsodium.la) must be present."""
    if not b_default.installed:
        pytest.fail(b_default.failure_summary())
    got = _entries(b_default)
    missing = []
    for e in INSTALL["default"]:
        if os.path.basename(e["path"]) == "libsodium.la":
            continue
        if e["path"] not in got:
            missing.append(e["path"])
    assert missing == [], (
        "%d entries from State A's install tree are missing: %s"
        % (len(missing), missing[:12]))
