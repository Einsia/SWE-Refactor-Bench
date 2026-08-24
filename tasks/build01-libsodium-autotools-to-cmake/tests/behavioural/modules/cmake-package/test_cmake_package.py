#!/usr/bin/env python3
"""Behavioural: the installed library must be consumable from outside (§1.6).

Every check here is end-to-end: a *separate* project in its own directory, given
nothing but the install prefix, finds the library, compiles against it, links it
shared and static, and the resulting binary is executed and its output compared
to State A's.

Two layers, and the split is the whole design of this module. The capability is
"a downstream project can consume this install tree", and State A has it: it
installs `libsodium.pc`, and `pkg-config --cflags --libs` is how a separate
project reaches it. The *interface* §1.6 pins is a CMake package -- package
`libsodium`, target `libsodium::sodium`, joined by `libsodium::sodium_static` in
the configuration that builds both library kinds, version 1.0.20, under
<libdir>/cmake/libsodium -- and State A ships none of it, because a
config file, a config-version file and an `install(EXPORT)` targets file are
things only CMake writes.

So the fifteen checks that read those files, or ask `find_package` to do
something only `find_package` does, are gated to a CMake delivery. The ten that
build a consumer are asked of both, against whichever package interface was
delivered: `find_package` + the imported target for CMake, the installed `.pc`
for Autotools. Same consumer program, same expected output, same shared/static
distinction -- so what the module measures for a CMake submission is unchanged,
and what it measures for State A is the capability rather than the interface.

Not gated to zero weight. A module whose every check is a licensed skip has an
empty pool and scores zero, which is the scorer's deliberate answer to "deleting
a module is the cheapest way to pass it"; and §1.6 is a real requirement whose
regression should cost something. Keeping the consumption checks live is what
lets the weight stay where it is.
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
import consumers  # noqa: E402
import elfutil  # noqa: E402

with open(os.path.join(SUITE, "data", "baseline.json")) as _fh:
    BASE = json.load(_fh)

VERSION = BASE["package_version"]              # 1.0.20
CONSUMER_OUTPUT = BASE["consumer_output"]      # 1.0.20|26|2|0
WORK = os.environ.get("SRB_WORK", "/tmp/srb-work")

SHARED_TARGET = "libsodium::sodium"
STATIC_TARGET = "libsodium::sodium_static"


@pytest.fixture(scope="session")
def pkgdir(b_default):
    """<libdir>/cmake/libsodium, for the checks that read what CMake wrote there.

    Skips -- not fails -- when the delivery is Autotools, because there is no
    such directory for `install(EXPORT)` to have written and every check that
    asks for this fixture carries `flavour.only(CMAKE)`, which licenses the skip.
    A CMake delivery that installed no package directory still fails here, which
    is the case this fixture exists to catch.
    """
    if not flavour.is_cmake():
        pytest.skip("the delivered build system is %s, which installs no CMake "
                    "package directory" % flavour.FLAVOUR)
    if not b_default.installed:
        pytest.fail("default build did not install:\n%s" % b_default.failure_summary())
    d = b_default.cmake_package_dir()
    if not d:
        pytest.fail("no CMake package directory was installed; §1.6 requires "
                    "<libdir>/cmake/libsodium/libsodiumConfig.cmake")
    return d


def _consume(directory, build, static=False, cmake_target=None):
    """Build the consumer against `build`'s install tree, however it is consumed.

    `find_package` + the imported target for a CMake delivery, the installed
    `libsodium.pc` for an Autotools one.  Returns the same dict either way, so
    the checks below read a result rather than a build system.

    `static` says which *library* to link, which is not the same question as which
    *target name* spells it: §1.6 pins `libsodium::sodium_static` as the archive
    only when both kinds are built, and makes `libsodium::sodium` the archive in a
    static-only install.  `cmake_target` names the target for callers whose
    configuration makes the derived name the wrong one; the Autotools path below
    is unaffected either way, since it selects a library by link line and not by
    target.
    """
    if flavour.is_cmake():
        target = cmake_target or (STATIC_TARGET if static else SHARED_TARGET)
        rc, out, binary = consumers.cmake_consumer(directory, build.prefix,
                                                   target=target)
        how = "find_package(libsodium REQUIRED) + " + target
    else:
        if static:
            rc, out, binary = consumers.archive_consumer(directory, build.libdir())
        else:
            rc, out, binary = consumers.pkgconfig_consumer(directory, build.prefix,
                                                           build.libdir())
        how = _HOW_AUTOTOOLS[static]
    return {"rc": rc, "out": out, "binary": binary, "build": build, "how": how}


#: What the consumer was pointed at, for the failure messages.  A check that
#: fails should name the interface it used and not the one the contract pins --
#: which is why the CMake half is built from the target actually passed to
#: `cmake_consumer` rather than looked up by configuration.
_HOW_AUTOTOOLS = {
    False: "pkg-config --cflags --libs libsodium",
    True: "pkg-config --static --libs libsodium, with libsodium.a named "
          "on the link line",
}


@pytest.fixture(scope="session")
def pkgfiles(pkgdir):
    return sorted(os.listdir(pkgdir))


def _read(pkgdir, name):
    p = os.path.join(pkgdir, name)
    if not os.path.isfile(p):
        return None
    return open(p, errors="replace").read()


@flavour.only(flavour.CMAKE, reason=(
    "libsodiumConfig.cmake is the file `find_package` looks for, and only CMake "
    "writes one. What it is *for* -- a separate project locating the library from "
    "the prefix alone -- is the consumption checks below, which are asked of both "
    "build systems"))
def test_package_config_file_installed(pkgfiles):
    assert any(f in ("libsodiumConfig.cmake", "libsodium-config.cmake")
               for f in pkgfiles), (
        "no libsodiumConfig.cmake in the package directory, found %s (§1.6)" % pkgfiles)


@flavour.only(flavour.CMAKE, reason=(
    "the config-version file is `write_basic_package_version_file`'s output. An "
    "Autotools install carries its version in libsodium.pc's `Version:` field, "
    "which the `pkgconfig` module compares against State A's"))
def test_package_version_file_installed(pkgfiles):
    assert any(f in ("libsodiumConfigVersion.cmake",
                     "libsodium-config-version.cmake") for f in pkgfiles), (
        "no libsodiumConfigVersion.cmake, found %s — find_package(libsodium 1.0.20) "
        "cannot work without it (§1.6)" % pkgfiles)


@flavour.only(flavour.CMAKE, reason=(
    "an exported targets file is what `install(EXPORT)` writes; there is no "
    "Autotools artefact that carries imported targets"))
def test_package_targets_file_installed(pkgfiles):
    assert any(re.search(r"[Tt]argets.*\.cmake$", f) for f in pkgfiles), (
        "no exported targets file in the package directory, found %s (§1.6)" % pkgfiles)


@flavour.only(flavour.CMAKE, reason=(
    "PACKAGE_VERSION is a variable in the config-version file gated above. The same "
    "question for an Autotools install is `pkg-config --modversion`, which the "
    "`pkgconfig` module asks"))
def test_package_version_is_state_a_version(pkgdir, pkgfiles):
    text = None
    for cand in ("libsodiumConfigVersion.cmake", "libsodium-config-version.cmake"):
        text = _read(pkgdir, cand)
        if text:
            break
    if text is None:
        pytest.fail("no config-version file to inspect (§1.6)")
    m = re.search(r'PACKAGE_VERSION\s+"?([0-9][0-9.]*)"?', text)
    assert m, "the config-version file declares no PACKAGE_VERSION"
    assert m.group(1) == VERSION, (
        "the CMake package declares version %s; the library is %s (§1.6)"
        % (m.group(1), VERSION))


@flavour.only(flavour.CMAKE, reason=(
    "there is no Autotools package directory whose location could be right or "
    "wrong; the .pc file's location is §1.7's, in the `install` module"))
def test_package_dir_location_exact(b_default, pkgdir):
    rel = os.path.relpath(pkgdir, b_default.prefix)
    assert rel == os.path.join("lib", "cmake", "libsodium"), (
        "the package was installed to %s; §1.6 pins lib/cmake/libsodium" % rel)


@flavour.only(flavour.CMAKE, reason=(
    "`libsodium::sodium` is a CMake target name. Whether a consumer can link the "
    "shared library at all is the check further down, asked of both"))
def test_targets_file_defines_namespaced_target(pkgdir):
    blob = ""
    for f in os.listdir(pkgdir):
        if f.endswith(".cmake"):
            blob += _read(pkgdir, f) or ""
    assert SHARED_TARGET in blob, (
        "no %s in the installed package files; §1.6 pins that target name"
        % SHARED_TARGET)


@flavour.only(flavour.CMAKE, reason=(
    "reads the .cmake files in the package directory. The same property of the .pc "
    "file is `test_pc_is_relocatable` in the `pkgconfig` module, which State A "
    "passes"))
def test_package_files_are_relocatable(pkgdir, b_default):
    """Exported packages must not bake in the build tree."""
    offenders = []
    for f in os.listdir(pkgdir):
        if not f.endswith(".cmake"):
            continue
        text = _read(pkgdir, f) or ""
        for line in text.splitlines():
            if b_default.bld in line or b_default.src in line:
                offenders.append("%s: %s" % (f, line.strip()[:120]))
    assert offenders == [], (
        "the installed CMake package references the build/source tree, so it "
        "breaks for any downstream user: %s (§1.6)" % offenders[:4])


# --------------------------------------------------------------------------
# End-to-end consumption
# --------------------------------------------------------------------------

@pytest.fixture(scope="session")
def cm_shared(b_default):
    """A separate project linked against the shared library, however that is done.

    Deliberately does not depend on `pkgdir`: a CMake delivery that installed no
    package directory should fail *this* check with the `find_package` output that
    explains why, rather than erroring in setup on a fixture whose own check
    already reported the missing directory.
    """
    if not b_default.installed:
        pytest.fail("default build did not install:\n%s"
                    % b_default.failure_summary())
    return _consume(os.path.join(WORK, "_consumer_pkg_shared"), b_default)


@pytest.fixture(scope="session")
def cm_static(b_default):
    if not b_default.installed:
        pytest.fail("default build did not install:\n%s"
                    % b_default.failure_summary())
    return _consume(os.path.join(WORK, "_consumer_pkg_static"), b_default,
                    static=True)


@pytest.fixture(scope="session")
def cm_versioned(b_default, pkgdir):
    d = os.path.join(WORK, "_consumer_cm_version")
    body = ("cmake_minimum_required(VERSION 3.20)\n"
            "project(srb_consumer C)\n"
            "find_package(libsodium %s REQUIRED)\n"
            "add_executable(consumer consumer.c)\n"
            "target_link_libraries(consumer PRIVATE %s)\n" % (VERSION, SHARED_TARGET))
    os.makedirs(d, exist_ok=True)
    consumers.write_consumer_source(d)
    with open(os.path.join(d, "CMakeLists.txt"), "w") as fh:
        fh.write(body)
    import subprocess
    bld = os.path.join(d, "b")
    p = subprocess.run(["cmake", "-S", d, "-B", bld, "-G", "Ninja",
                        "-DCMAKE_PREFIX_PATH=" + b_default.prefix],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       timeout=600)
    return {"rc": p.returncode, "out": p.stdout.decode("utf-8", "replace")}


@flavour.only(flavour.CMAKE, reason=(
    "that string is CMake's own diagnostic for a missing config file. The Autotools "
    "route's equivalent -- pkg-config cannot find libsodium -- surfaces as a "
    "non-zero rc in the check below, which is asked of both"))
def test_find_package_succeeds(cm_shared):
    assert "Could not find a package configuration file" not in cm_shared["out"], (
        "find_package(libsodium REQUIRED) could not locate the package:\n%s"
        % cm_shared["out"][-1200:])


def test_shared_consumer_builds(cm_shared):
    assert cm_shared["rc"] == 0, (
        "a separate project could not build against the installed shared library "
        "through %s (§1.6):\n%s" % (cm_shared["how"], cm_shared["out"][-1500:]))


def test_shared_consumer_runs_and_matches_state_a(cm_shared, b_default):
    if cm_shared["rc"] != 0:
        pytest.fail("consumer did not build:\n%s" % cm_shared["out"][-800:])
    rc, out = elfutil.run_binary(cm_shared["binary"],
                                 env={"LD_LIBRARY_PATH": b_default.libdir()})
    assert rc == 0, "the consumer exited %d:\n%s" % (rc, out[-600:])
    assert out.strip().startswith(CONSUMER_OUTPUT), (
        "the consumer built through %s printed %r; State A yields %r…"
        % (cm_shared["how"], out.strip()[:60], CONSUMER_OUTPUT))


def test_shared_consumer_links_shared_library(cm_shared):
    if cm_shared["rc"] != 0:
        pytest.fail("consumer did not build:\n%s" % cm_shared["out"][-800:])
    needed = elfutil.needed(cm_shared["binary"])
    assert any(n.startswith("libsodium.so") for n in needed), (
        "%s did not produce a dynamic link against libsodium; NEEDED=%s"
        % (cm_shared["how"], needed))


@flavour.only(flavour.CMAKE, reason=(
    "`libsodium::sodium_static` is a CMake target name, and an Autotools install "
    "distinguishes the two libraries by which file the link line names rather than "
    "by exporting two targets. That a consumer can link the archive is the three "
    "checks below, asked of both"))
def test_static_target_exists(pkgdir):
    blob = ""
    for f in os.listdir(pkgdir):
        if f.endswith(".cmake"):
            blob += _read(pkgdir, f) or ""
    assert STATIC_TARGET in blob, (
        "no %s exported; §1.6 pins both target names when both libraries are "
        "built" % STATIC_TARGET)


def test_static_consumer_builds(cm_static):
    assert cm_static["rc"] == 0, (
        "a separate project could not build against the installed static archive "
        "through %s (§1.6):\n%s" % (cm_static["how"], cm_static["out"][-1500:]))


def test_static_consumer_runs_and_matches_state_a(cm_static):
    if cm_static["rc"] != 0:
        pytest.fail("static consumer did not build:\n%s" % cm_static["out"][-800:])
    rc, out = elfutil.run_binary(cm_static["binary"])
    assert rc == 0, "the static consumer exited %d:\n%s" % (rc, out[-600:])
    assert out.strip().startswith(CONSUMER_OUTPUT), (
        "the static consumer printed %r, expected %r…"
        % (out.strip()[:60], CONSUMER_OUTPUT))


def test_static_consumer_has_no_libsodium_dependency(cm_static):
    """The static route must produce a binary that needs no libsodium at runtime."""
    if cm_static["rc"] != 0:
        pytest.fail("static consumer did not build, so what it linked cannot be "
                    "read:\n%s" % cm_static["out"][-800:])
    needed = elfutil.needed(cm_static["binary"])
    assert not any(n.startswith("libsodium.so") for n in needed), (
        "the static route (%s) pulled in the shared library (NEEDED=%s) — it is "
        "wired to the wrong artifact (§1.6)" % (cm_static["how"], needed))


@flavour.only(flavour.CMAKE, reason=(
    "`find_package(libsodium 1.0.20 REQUIRED)` is a version *negotiation*, which is "
    "what the config-version file exists for. pkg-config's `--atleast-version` is "
    "not the same mechanism and the version it would compare is the .pc field the "
    "`pkgconfig` module already checks against State A's"))
def test_find_package_version_request_accepted(cm_versioned):
    assert cm_versioned["rc"] == 0, (
        "find_package(libsodium %s REQUIRED) failed; the config-version file "
        "does not accept the library's own version (§1.6):\n%s"
        % (VERSION, cm_versioned["out"][-1200:]))


@flavour.only(flavour.CMAKE, reason=(
    "the negative half of the check above, and gated for the same reason: it drives "
    "find_package, which an Autotools install does not answer"))
def test_find_package_rejects_impossible_version(b_default, pkgdir):
    """A version file that accepts anything is not a version file."""
    import subprocess
    d = os.path.join(WORK, "_consumer_cm_badversion")
    os.makedirs(d, exist_ok=True)
    consumers.write_consumer_source(d)
    with open(os.path.join(d, "CMakeLists.txt"), "w") as fh:
        fh.write("cmake_minimum_required(VERSION 3.20)\n"
                 "project(srb_consumer C)\n"
                 "find_package(libsodium 99.0.0 REQUIRED)\n")
    p = subprocess.run(["cmake", "-S", d, "-B", os.path.join(d, "b"),
                        "-DCMAKE_PREFIX_PATH=" + b_default.prefix],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=600)
    assert p.returncode != 0, (
        "find_package(libsodium 99.0.0 REQUIRED) succeeded — the exported "
        "config-version file does not compare versions at all (§1.6)")


def test_consumer_needs_no_manual_include_dirs(cm_shared):
    """The package interface must carry its own usage requirements.

    The consumer project names no include directory anywhere -- it is
    `#include <sodium.h>` and nothing else -- so it compiles only if the interface
    it was pointed at supplied the path itself: INTERFACE_INCLUDE_DIRECTORIES on
    the imported target, or `Cflags:` in the .pc file.
    """
    if cm_shared["rc"] != 0:
        pytest.fail("consumer did not build, so %s did not supply the header "
                    "search path:\n%s" % (cm_shared["how"], cm_shared["out"][-800:]))
    assert "sodium.h" not in cm_shared["out"] or "fatal error" not in cm_shared["out"], (
        "the consumer could not find sodium.h through %s" % cm_shared["how"])


@flavour.only(flavour.CMAKE, reason=(
    "reads the exported targets file for a target name. That the static-only install "
    "is consumable at all is `test_static_only_consumer_builds` below, asked of both"))
def test_static_only_build_exports_the_canonical_target(b_static_only):
    """§1.6: with only one library kind built, `libsodium::sodium` is that kind.

    So what a static-only install must export is the canonical name, not the
    disambiguating one.  `libsodium::sodium_static` is what §1.6 pins for the
    configuration that builds *both* -- there it is the name that picks the archive
    out of two candidates, and `test_static_target_exists` above requires it of the
    default build.  Here there is nothing to disambiguate, and requiring the second
    name would fail a submission that implemented §1.6 exactly as written.  A
    submission that exports it anyway, as an alias, is equally fine and passes.
    """
    if not b_static_only.installed:
        pytest.fail("static-only build did not install:\n%s"
                    % b_static_only.failure_summary())
    d = b_static_only.cmake_package_dir()
    assert d, "the static-only configuration installed no CMake package (§1.6)"
    blob = "".join((_read(d, f) or "") for f in os.listdir(d) if f.endswith(".cmake"))
    assert SHARED_TARGET in blob, (
        "static-only build exports no %s; §1.6 makes that name the static library "
        "when it is the only one built%s"
        % (SHARED_TARGET,
           "" if STATIC_TARGET not in blob else
           " (%s is exported, which is permitted but is not a substitute -- a "
           "consumer written against §1.6 links %s)" % (STATIC_TARGET, SHARED_TARGET)))


@flavour.only(flavour.CMAKE, reason=(
    "same as the check above, for the shared-only configuration"))
def test_shared_only_build_exports_shared_target(b_shared_only):
    if not b_shared_only.installed:
        pytest.fail("shared-only build did not install:\n%s"
                    % b_shared_only.failure_summary())
    d = b_shared_only.cmake_package_dir()
    assert d, "the shared-only configuration installed no CMake package (§1.6)"
    blob = "".join((_read(d, f) or "") for f in os.listdir(d) if f.endswith(".cmake"))
    assert SHARED_TARGET in blob, (
        "shared-only build does not export %s (§1.6)" % SHARED_TARGET)


def test_shared_only_consumer_builds(b_shared_only):
    if not b_shared_only.installed:
        pytest.fail(b_shared_only.failure_summary())
    r = _consume(os.path.join(WORK, "_consumer_pkg_sharedonly"), b_shared_only)
    assert r["rc"] == 0, (
        "a consumer could not build against the shared-only install through "
        "%s:\n%s" % (r["how"], r["out"][-1200:]))
    rc2, sout = elfutil.run_binary(r["binary"],
                                   env={"LD_LIBRARY_PATH": b_shared_only.libdir()})
    assert rc2 == 0 and sout.strip().startswith(CONSUMER_OUTPUT), (
        "shared-only consumer misbehaved: rc=%s out=%r" % (rc2, sout[:80]))


def test_static_only_consumer_builds(b_static_only):
    if not b_static_only.installed:
        pytest.fail(b_static_only.failure_summary())
    # static=True still selects the archive for the Autotools flavour, which names
    # a library file rather than a target.  The CMake target is overridden because
    # §1.6 makes `libsodium::sodium` the archive here; see
    # test_static_only_build_exports_the_canonical_target.
    r = _consume(os.path.join(WORK, "_consumer_pkg_staticonly"), b_static_only,
                 static=True, cmake_target=SHARED_TARGET)
    assert r["rc"] == 0, (
        "a consumer could not build against the static-only install through "
        "%s:\n%s" % (r["how"], r["out"][-1200:]))
    rc2, sout = elfutil.run_binary(r["binary"])
    assert rc2 == 0 and sout.strip().startswith(CONSUMER_OUTPUT), (
        "static-only consumer misbehaved: rc=%s out=%r" % (rc2, sout[:80]))


def test_minimal_install_is_consumable(b_minimal):
    if not b_minimal.installed:
        pytest.fail("minimal build did not install:\n%s" % b_minimal.failure_summary())
    r = _consume(os.path.join(WORK, "_consumer_pkg_minimal"), b_minimal)
    assert r["rc"] == 0, (
        "a consumer could not build against the minimal install through %s "
        "(generichash and version are core API):\n%s" % (r["how"], r["out"][-1200:]))
    rc2, sout = elfutil.run_binary(r["binary"],
                                   env={"LD_LIBRARY_PATH": b_minimal.libdir()})
    assert rc2 == 0 and sout.strip().startswith(CONSUMER_OUTPUT), (
        "minimal consumer misbehaved: rc=%s out=%r" % (rc2, sout[:80]))


@flavour.only(flavour.CMAKE, reason=(
    "CONFIG mode is the distinction between a real package config and a Find module, "
    "and both are CMake mechanisms. pkg-config has one lookup path and the checks "
    "above already used it"))
def test_config_mode_find_package(b_default, pkgdir):
    """find_package(... CONFIG) must work: no Find-module fallback allowed."""
    d = os.path.join(WORK, "_consumer_cm_config")
    rc, out, binary = consumers.cmake_consumer(d, b_default.prefix,
                                               target=SHARED_TARGET, config=True)
    assert rc == 0, (
        "find_package(libsodium REQUIRED CONFIG) failed, so no real package "
        "config was installed (§1.6):\n%s" % out[-1200:])


@flavour.only(flavour.CMAKE, reason=(
    "reads the .cmake files for flags an INTERFACE property should not carry. The "
    "same property of the .pc file is `test_pc_cflags_semantics` in the `pkgconfig` "
    "module, which requires Cflags to be the include path and nothing else"))
def test_package_does_not_leak_private_link_flags(pkgdir):
    blob = "".join((_read(pkgdir, f) or "") for f in os.listdir(pkgdir)
                   if f.endswith(".cmake"))
    for token in ("-Wall", "-Wextra", "-fvisibility=hidden", "-mavx2", "-maes"):
        assert token not in blob, (
            "the exported package pushes the private build flag %s onto every "
            "downstream consumer (§1.6: usage requirements must be interface "
            "requirements)" % token)
