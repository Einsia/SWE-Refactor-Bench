#!/usr/bin/env python3
"""Behavioural: the source package (contract §1.9).

State A's `make dist` produces a tarball that a downstream packager can build
from. State B has to keep that capability through CPack's source generator. So
the archive is what is read here and the target that produced it is the fixture's
business: `Build.source_package()` runs CPack for a CMake tree and `make dist` for
an Autotools one, and every check below asks the same question of what came out.

Two kinds of check live here and they are weighted differently on purpose. The
scored ones ask what State A's own `make dist` tarball answers: it carries the
sources, the headers, the test programs and the license, and it carries no `.o`,
no `.libs/`, no `.git/`. A submission that regresses any of those has lost a
capability, and that is this stage's business.

The `srb_weight(0.0)` ones ask whether the Autotools files are gone from the
archive. State A's `make dist` ships `configure.ac`, the `Makefile.am` files, `m4/`
and `build-aux/` deliberately -- they are its source distribution, not litter --
so those checks are failed by the very tree every expectation in `data/` was
recorded from. They run and they are recorded, because "the archive still ships
configure.ac" is a useful thing to read in a report; they carry no weight, because
the judgement they encode belongs to `autotools_retired` in stage 1, which asks it
over the whole tree with a reviewer rather than over one tarball with a basename
match.
"""
import json
import os
import re
import subprocess
import tarfile
import zipfile

import pytest

import flavour

pytestmark = pytest.mark.behaviour

# data/ and lib/ live at the suite root, one level above modules/.  The
# runner exports SRB_SUITE_DIR; the fallback keeps a module runnable by hand.
SUITE = os.environ.get("SRB_SUITE_DIR") or os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import builder  # noqa: E402

with open(os.path.join(SUITE, "data", "baseline.json")) as _fh:
    BASE = json.load(_fh)

DIST_FILES = BASE["dist_files"]
VERSION = BASE["package_version"]

# Representative content that State A's `make dist` tarball carries and that a
# source package must still carry for the project to be buildable from it.
MUST_CONTAIN = [
    "src/libsodium/sodium/core.c",
    "src/libsodium/include/sodium.h",
    "src/libsodium/include/sodium/version.h.in",
    "src/libsodium/crypto_generichash/blake2b/ref/blake2b-ref.c",
    "src/libsodium/crypto_scalarmult/curve25519/sandy2x/fe51_mul.S",
    "test/default/auth.c",
    "test/default/auth.exp",
    "test/default/cmptest.h",
    "LICENSE",
    "README.markdown",
    "AUTHORS",
    "ChangeLog",
]

MUST_NOT_CONTAIN_SUFFIX = (".o", ".lo", ".la", ".so", ".a", ".res",
                           "CMakeCache.txt", "compile_commands.json")

MUST_NOT_CONTAIN_NAME = ("configure", "configure.ac", "Makefile.am",
                         "Makefile.in", "aclocal.m4", "autogen.sh",
                         "libsodium-uninstalled.pc.in",
                         "config.status", "libtool")

# Forbidden only at the archive root: a CMake-owned cmake/libsodium.pc.in is
# legitimate (§1.7), the root-level Autotools template is not.
MUST_NOT_CONTAIN_ROOT_NAME = ("libsodium.pc.in",)

# Hygiene: a source archive that carries these is malformed whoever built it.
# State A's own `make dist` excludes every one, so these are scored.
MUST_NOT_CONTAIN_DIR = (".git/", ".svn/", "autom4te.cache/", ".deps/",
                        ".libs/", "CMakeFiles/", "_CPack_Packages/")

# Detection: State A's `make dist` ships m4/ and build-aux/ on purpose -- they are
# part of its source distribution, not build litter. Absence of them is a fact
# about whether the migration happened, which is `autotools_retired`'s question in
# stage 1. Recorded here at weight 0. See the module docstring.
RETIRED_TOOLCHAIN_DIR = ("m4/", "build-aux/")


@pytest.fixture(scope="session")
def srcpkg(b_default):
    """Produce a source archive with whatever the delivered build system offers.

    CPack's source generator for a CMake tree, `make dist` for an Autotools one --
    §1.9's capability is "a downstream packager can build from a tarball this
    build system produced", and both spell it.  Everything below reads the
    archive, so only this fixture has to know which was run.
    """
    if not b_default.configured:
        pytest.fail("default configure failed:\n%s" % b_default.failure_summary())
    return dict(b_default.source_package(timeout=1200), build=b_default)


@pytest.fixture(scope="session")
def srcpkg_members(srcpkg):
    if not srcpkg["archives"]:
        return None
    path = srcpkg["archives"][0]
    try:
        if path.endswith(".zip"):
            with zipfile.ZipFile(path) as zf:
                names = zf.namelist()
        else:
            with tarfile.open(path) as tf:
                names = tf.getnames()
    except Exception as exc:                       # noqa: BLE001
        return {"error": str(exc), "names": []}
    # strip the single top-level directory so paths are project-relative
    stripped = set()
    for n in names:
        parts = n.split("/", 1)
        stripped.add(parts[1] if len(parts) == 2 and parts[1] else parts[0])
    return {"error": None, "names": sorted(stripped), "raw": names, "path": path}


def _members(srcpkg_members):
    if srcpkg_members is None:
        pytest.fail("no source archive was produced (§1.9)")
    if srcpkg_members["error"]:
        pytest.fail("the source archive could not be read: %s"
                    % srcpkg_members["error"])
    return srcpkg_members["names"]


@flavour.only(flavour.CMAKE, reason=(
    "CPackSourceConfig.cmake is the file `include(CPack)` writes, and §1.9 names it "
    "as the entry point. An Autotools tree reaches the same capability through the "
    "`dist` target automake generates, which has no configuration file to require; "
    "that the target exists and produces an archive is the two checks below"))
def test_cpack_source_config_generated(srcpkg):
    assert os.path.isfile(srcpkg["config"]), (
        "no CPackSourceConfig.cmake in the build tree; §1.9 requires the CPack "
        "source generator to be configured (include(CPack) with the source "
        "settings)")


def test_source_package_target_succeeds(srcpkg):
    s = srcpkg["step"]
    assert s is not None and s.ok, (
        "the build system's source-package target did not succeed (§1.9):\n%s"
        % (s.tail(30) if s else "not run"))


def test_source_archive_produced(srcpkg):
    assert srcpkg["archives"], (
        "the source package step left no archive behind (§1.9)")


def test_source_archive_names_the_version(srcpkg):
    if not srcpkg["archives"]:
        pytest.fail("no source archive produced (§1.9)")
    names = [os.path.basename(p) for p in srcpkg["archives"]]
    assert any(VERSION in n for n in names), (
        "the source archive names %s carry no version; State A produces "
        "libsodium-%s.tar.gz (§1.9)" % (names, VERSION))


@pytest.mark.parametrize("member", MUST_CONTAIN)
def test_source_archive_contains(srcpkg_members, member):
    names = _members(srcpkg_members)
    assert member in names, (
        "the source archive omits %s; a downstream packager could not build "
        "from it (§1.9)" % member)


def test_source_archive_contains_all_c_sources(srcpkg_members):
    names = set(_members(srcpkg_members))
    wanted = [f for f in DIST_FILES if f.endswith(".c") and f.startswith("src/")]
    missing = sorted(set(wanted) - names)
    assert missing == [], (
        "the source archive omits %d library sources, e.g. %s (§1.9)"
        % (len(missing), missing[:5]))


def test_source_archive_contains_all_public_headers(srcpkg_members):
    names = set(_members(srcpkg_members))
    wanted = [f for f in DIST_FILES
              if f.startswith("src/libsodium/include/") and f.endswith(".h")]
    missing = sorted(set(wanted) - names)
    assert missing == [], (
        "the source archive omits %d headers, e.g. %s (§1.9)"
        % (len(missing), missing[:5]))


def test_source_archive_contains_test_suite(srcpkg_members):
    names = set(_members(srcpkg_members))
    wanted = [f for f in DIST_FILES if f.startswith("test/default/") and
              f.endswith((".c", ".exp", ".h"))]
    missing = sorted(set(wanted) - names)
    assert missing == [], (
        "the source archive omits %d test-suite files, e.g. %s — §1.9 requires "
        "the test suite to travel with the sources"
        % (len(missing), missing[:5]))


def test_source_archive_contains_cmake_build_system(srcpkg_members):
    """The archive must carry the build system it was produced by.

    Named for CMake because that is what §1.9 requires of State B; asked of
    whichever build system was delivered, because an archive that does not carry
    its own build files cannot be built from, and that is the capability. The
    stronger form of this is `test_unpacked_source_archive_configures` below.
    """
    names = _members(srcpkg_members)
    wanted = ["CMakeLists.txt"] if flavour.is_cmake() else \
        ["configure", "configure.ac", "Makefile.in", "Makefile.am"]
    assert any(w in names for w in wanted), (
        "the source archive carries none of %s, so it cannot be built (§1.9)"
        % wanted)


@pytest.mark.parametrize("suffix", MUST_NOT_CONTAIN_SUFFIX)
def test_source_archive_excludes_build_products(srcpkg_members, suffix):
    names = _members(srcpkg_members)
    bad = [n for n in names if n.endswith(suffix)]
    assert bad == [], (
        "the source archive ships build products ending in %s: %s (§1.9)"
        % (suffix, bad[:5]))


@pytest.mark.srb_weight(0.0)
@pytest.mark.parametrize("name", MUST_NOT_CONTAIN_NAME)
def test_source_archive_excludes_autotools_files(srcpkg_members, name):
    """Recorded, not charged: State A's `make dist` ships these by design."""
    names = _members(srcpkg_members)
    bad = [n for n in names if os.path.basename(n) == name]
    assert bad == [], (
        "the source archive still ships the Autotools file %s: %s (§1.9/§2)"
        % (name, bad[:5]))


@pytest.mark.srb_weight(0.0)
@pytest.mark.parametrize("name", MUST_NOT_CONTAIN_ROOT_NAME)
def test_source_archive_excludes_root_autotools_templates(srcpkg_members, name):
    """Recorded, not charged: see `test_source_archive_excludes_autotools_files`."""
    names = _members(srcpkg_members)
    bad = [n for n in names if n == name]
    assert bad == [], (
        "the source archive still ships the root-level Autotools template %s "
        "(§1.7/§1.9)" % name)


@pytest.mark.parametrize("d", MUST_NOT_CONTAIN_DIR)
def test_source_archive_excludes_directory(srcpkg_members, d):
    names = srcpkg_members and srcpkg_members.get("raw") or _members(srcpkg_members)
    bad = [n for n in names if ("/" + d) in ("/" + n) or n.startswith(d)]
    assert bad == [], (
        "the source archive contains %s: %s (§1.9)" % (d, bad[:5]))


@pytest.mark.srb_weight(0.0)
@pytest.mark.parametrize("d", RETIRED_TOOLCHAIN_DIR)
def test_source_archive_excludes_retired_toolchain_directory(srcpkg_members, d):
    """Recorded, not charged: State A's source distribution contains these."""
    names = srcpkg_members and srcpkg_members.get("raw") or _members(srcpkg_members)
    bad = [n for n in names if ("/" + d) in ("/" + n) or n.startswith(d)]
    assert bad == [], (
        "the source archive contains %s: %s (§1.9)" % (d, bad[:5]))


def test_source_archive_excludes_install_tree(srcpkg_members, b_default):
    names = _members(srcpkg_members)
    bad = [n for n in names if n.startswith(("inst/", "build/", "destdir/"))]
    assert bad == [], "the source archive contains a build tree: %s" % bad[:5]


def test_source_archive_has_single_root(srcpkg_members):
    if srcpkg_members is None:
        pytest.fail("no source archive produced (§1.9)")
    raw = srcpkg_members.get("raw") or []
    roots = {n.split("/", 1)[0] for n in raw if n and not n.startswith("/")}
    roots.discard("")
    assert len(roots) == 1, (
        "the source archive unpacks into %d top-level entries (%s); State A's "
        "tarball unpacks into one libsodium-%s directory (§1.9)"
        % (len(roots), sorted(roots)[:5], VERSION))


def test_source_archive_size_is_plausible(srcpkg_members):
    if srcpkg_members is None:
        pytest.fail("no source archive produced (§1.9)")
    path = srcpkg_members.get("path")
    size = os.path.getsize(path) if path and os.path.isfile(path) else 0
    assert size > 400 * 1024, (
        "the source archive is only %d bytes; State A's is over a megabyte, so "
        "content is missing (§1.9)" % size)


def test_source_archive_file_count_is_plausible(srcpkg_members):
    names = _members(srcpkg_members)
    files = [n for n in names if not n.endswith("/")]
    assert len(files) >= 500, (
        "the source archive holds only %d entries; State A's `make dist` ships "
        "%d (§1.9)" % (len(files), len(DIST_FILES)))


def test_unpacked_source_archive_configures(srcpkg_members, tmp_path_factory):
    """The strongest form of §1.9: build the project out of its own tarball."""
    if srcpkg_members is None:
        pytest.fail("no source archive produced (§1.9)")
    path = srcpkg_members["path"]
    dest = str(tmp_path_factory.mktemp("srcpkg"))
    try:
        if path.endswith(".zip"):
            with zipfile.ZipFile(path) as zf:
                zf.extractall(dest)
        else:
            with tarfile.open(path) as tf:
                tf.extractall(dest)
    except Exception as exc:                       # noqa: BLE001
        pytest.fail("could not unpack the source archive: %s" % exc)
    roots = [os.path.join(dest, d) for d in os.listdir(dest)
             if os.path.isdir(os.path.join(dest, d))]
    root = roots[0] if len(roots) == 1 else dest
    bld = os.path.join(dest, "b")
    os.makedirs(bld, exist_ok=True)
    if flavour.is_cmake():
        if not os.path.isfile(os.path.join(root, "CMakeLists.txt")):
            pytest.fail("the unpacked archive has no CMakeLists.txt at its root")
        argv, cwd = ["cmake", "-S", root, "-B", bld, "-G", "Ninja"], root
    else:
        script = os.path.join(root, "configure")
        if not os.path.isfile(script):
            pytest.fail("the unpacked archive has no `configure` at its root")
        argv, cwd = [script], bld
    s = builder.run("srcpkg-configure", argv, cwd, 900,
                    builder.clean_env(stubs=False))
    assert s.ok, (
        "the project cannot be configured from its own source package "
        "(§1.9):\n%s" % s.tail(30))
