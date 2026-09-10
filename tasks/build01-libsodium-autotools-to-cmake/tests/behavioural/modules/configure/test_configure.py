#!/usr/bin/env python3
"""Behavioural: the build configures, builds and installs across the matrix.

Contract §1.1 and §1.2.
"""
import itertools
import os
import re

import pytest

import flavour

pytestmark = pytest.mark.behaviour

#: The four library shapes §1.2 requires an option for, and the default each must
#: ship.  Named by capability rather than by cache variable: the question is
#: whether the delivered build system lets a packager ask for a minimal library or
#: a static-only one, and `SODIUM_MINIMAL` is one build system's spelling of it.
#: `Build.options_view()` returns the same record whichever spelling was found.
CACHE_OPTIONS = {
    "minimal": "OFF",
    "shared": "ON",
    "static": "ON",
    "tests": "ON",
}

TRUTHY = {"ON", "1", "TRUE", "YES", "Y"}
FALSY = {"OFF", "0", "FALSE", "NO", "N", ""}

MATRIX_NAMES = ["ninja-default", "make-default", "ninja-minimal",
                "ninja-static-only", "ninja-shared-only", "ninja-notests",
                "ninja-debug"]


def norm(v):
    v = (v or "").strip().upper()
    if v in TRUTHY:
        return "ON"
    if v in FALSY:
        return "OFF"
    return v


def options(build):
    """{capability: option record} -- see `Build.options_view()`."""
    return build.options_view()


def option(build, capability):
    """One capability's option record, or a fail naming what is missing.

    A capability with no option under this build system is a *skip*, licensed:
    automake builds the test programs under `make check` and has no
    `--enable-tests`, so "the tests option defaults to ON" is not a question with
    an answer here.  A capability the build system *should* expose and does not is
    the failing case, and that is `test_cache_option_exists`.
    """
    if flavour.switch(capability) is None:
        pytest.skip("%s exposes no option for %s; the capability is reached "
                    "another way" % (flavour.FLAVOUR, capability))
    view = options(build)
    if capability not in view:
        pytest.fail(
            "%s (%s) is not an option of the delivered build system; §1.2 "
            "requires it to be a real, documented one" % (
                capability, flavour.switch(capability)))
    return view[capability]


# ------------------------------------------------------------------- matrix --
@pytest.mark.parametrize("name", MATRIX_NAMES)
def test_configuration_configures(registry, name):
    b = registry.get(name)
    assert b.configured, "cmake configure failed for %s:\n%s" % (
        name, b.failure_summary())


@pytest.mark.parametrize("name", MATRIX_NAMES)
def test_configuration_builds(registry, name):
    b = registry.get(name)
    if not b.configured:
        pytest.fail("configure failed for %s:\n%s" % (name, b.failure_summary()))
    assert b.built, "cmake --build failed for %s:\n%s" % (name, b.failure_summary())


@pytest.mark.parametrize("name", MATRIX_NAMES)
def test_configuration_installs(registry, name):
    b = registry.get(name)
    if not b.built:
        pytest.fail("build failed for %s:\n%s" % (name, b.failure_summary()))
    assert b.installed, "cmake --install failed for %s:\n%s" % (
        name, b.failure_summary())


@pytest.mark.parametrize("name", ["ninja-default", "make-default"])
@flavour.only(flavour.CMAKE, reason=(
    "a generator is a CMake concept: CMake writes build files for a backend you "
    "choose, and `-G Ninja` working as well as `-G 'Unix Makefiles'` is a real "
    "requirement on it. Autotools has one backend -- configure writes makefiles "
    "and make runs them -- so there is no second generator to try, and the two "
    "matrix entries below are the same build twice. Reporting that as a pass "
    "would be claiming to have varied something that was never varied"))
def test_both_generators_supported(registry, name):
    """§1.1: Ninja and Unix Makefiles must both work."""
    b = registry.get(name)
    assert b.installed, "%s did not produce an install tree:\n%s" % (
        name, b.failure_summary())


# ------------------------------------------------------------ build options --
#
# §1.2 asks for four options that are real, boolean, correctly defaulted and
# documented.  `cmake -LAH` is where a CMake project answers that and `configure
# --help` is where an Autotools one does; `options_view()` reads whichever exists
# and returns the same record, so these four checks are one question each rather
# than one question per build system.
@pytest.mark.srb_skip_ok
@pytest.mark.parametrize("capability", sorted(CACHE_OPTIONS))
def test_cache_option_exists(b_default, capability):
    if flavour.switch(capability) is None:
        pytest.skip("%s exposes no option for %s" % (flavour.FLAVOUR, capability))
    view = options(b_default)
    assert capability in view, (
        "%s is not an option of the delivered build system; §1.2 requires it to "
        "be a real one, listed where a packager would look. Found: %s"
        % (flavour.switch(capability), sorted(view) or "none"))


@pytest.mark.srb_skip_ok
@pytest.mark.parametrize("capability", sorted(CACHE_OPTIONS))
def test_cache_option_is_bool(b_default, capability):
    rec = option(b_default, capability)
    assert rec["kind"] == "BOOL", (
        "%s has type %s; §1.2 describes a boolean option"
        % (rec["spelling"], rec["kind"]))


@pytest.mark.srb_skip_ok
@pytest.mark.parametrize("capability", sorted(CACHE_OPTIONS))
def test_cache_option_default(b_default, capability):
    rec = option(b_default, capability)
    got, want = norm(rec["default"]), CACHE_OPTIONS[capability]
    assert got == want, (
        "%s defaults to %s but §1.2 specifies %s"
        % (rec["spelling"], got, want))


@pytest.mark.srb_skip_ok
@pytest.mark.parametrize("capability", sorted(CACHE_OPTIONS))
def test_cache_option_documented(b_default, capability):
    """An option nobody can find is not an option.

    `cmake -LAH` prints the help string from `option()`; `configure --help` prints
    the one from `AC_ARG_ENABLE`. Either way the requirement is that the option
    describes itself where a packager would go looking for it.
    """
    rec = option(b_default, capability)
    assert rec["help"].strip(), (
        "%s carries no help text, so it does not appear in the option listing "
        "(§1.2)" % rec["spelling"])


# ------------------------------------------- every option combination builds --
CAPABILITIES = ("minimal", "shared", "static", "tests")
COMBOS = [c for c in itertools.product(["ON", "OFF"], repeat=4)
          if not (c[1] == "OFF" and c[2] == "OFF")]  # 12 of 16; §1.2


def _requested(combo):
    """{capability: value} for the capabilities this build system can be asked.

    A capability with no switch is dropped rather than passed and ignored, so two
    combinations that differ only in it resolve to the same configure -- one build
    instead of two identical ones, and no check claiming to have varied something
    that was never varied.
    """
    return {cap: val for cap, val in zip(CAPABILITIES, combo)
            if flavour.switch(cap) is not None}


def _combo_build(registry, combo):
    req = _requested(combo)
    name = "combo-" + "-".join("%s%s" % (c[:2], v.lower())
                               for c, v in sorted(req.items()))
    cmake_names = flavour.SWITCHES[flavour.CMAKE]
    return registry.dynamic(
        name, generator="Ninja", configure_only=True,
        options={cmake_names[c]: v for c, v in req.items()})


def _combo_label(combo):
    return " ".join("%s=%s" % (c.upper(), v)
                    for c, v in zip(CAPABILITIES, combo))


@pytest.mark.parametrize("combo", COMBOS, ids=lambda c: "".join(
    v[0] for v in c))
def test_option_combination_configures(registry, combo):
    """§1.2: every combination must configure (at least one library kind on)."""
    b = _combo_build(registry, combo)
    assert b.configured, (
        "configure failed for %s (§1.2):\n%s"
        % (_combo_label(combo), b.failure_summary()))


@pytest.mark.parametrize("combo", COMBOS, ids=lambda c: "".join(
    v[0] for v in c))
def test_option_combination_is_honoured(registry, combo):
    """The configured tree must hold what was asked for, not just accept it."""
    b = _combo_build(registry, combo)
    if not b.configured:
        pytest.fail("configure failed:\n%s" % b.failure_summary())
    view = b.options_view()
    for cap, want in sorted(_requested(combo).items()):
        assert cap in view, "%s vanished from the option table" % cap
        assert norm(view[cap]["value"]) == want, (
            "%s was requested as %s but the configured tree holds %s"
            % (view[cap]["spelling"], want, view[cap]["value"]))


# ------------------------------------------------------- toolchain entry points --
def test_honours_cmake_c_flags(registry):
    b = registry.dynamic("flags-cflags", generator="Ninja",
                         options={"CMAKE_C_FLAGS": "-DSRB_PROBE_FLAG=1"},
                         configure_only=True)
    if not b.configured:
        pytest.fail("configure with CMAKE_C_FLAGS failed:\n%s" % b.failure_summary())
    cmds = b.compile_commands()
    assert cmds, "no compile database to inspect"
    lib = [c for _f, c in cmds if "SRB_PROBE_FLAG" in c]
    assert lib, (
        "CMAKE_C_FLAGS did not reach the compile lines; §1.1 requires it to be "
        "honoured (the build probably overwrites CMAKE_C_FLAGS instead of "
        "appending to it)")


def test_honours_cmake_install_libdir(registry):
    b = registry.dynamic("libdir-lib64", generator="Ninja",
                         options={"CMAKE_INSTALL_LIBDIR": "lib64"})
    if not b.installed:
        pytest.fail("build/install with CMAKE_INSTALL_LIBDIR=lib64 failed:\n%s"
                    % b.failure_summary())
    p = os.path.join(b.prefix, "lib64")
    assert os.path.isdir(p), (
        "CMAKE_INSTALL_LIBDIR=lib64 was ignored — nothing installed to %s; §1.1 "
        "requires the GNUInstallDirs variables to be honoured" % p)
    libs = [f for f in os.listdir(p) if f.startswith("libsodium")]
    assert libs, "no libsodium artifact in lib64/: %s" % os.listdir(p)[:10]


def test_honours_destdir(b_default):
    """§1.1: DESTDIR must prefix the whole install tree."""
    if not b_default.built:
        pytest.fail(b_default.failure_summary())
    step = b_default.install_to_destdir()
    assert step.ok, "DESTDIR install failed:\n%s" % step.tail(25)
    staged = os.path.join(b_default.destdir, b_default.prefix.lstrip("/"))
    assert os.path.isdir(staged), (
        "DESTDIR was ignored: expected the install tree under %s" % staged)
    header = os.path.join(staged, "include", "sodium.h")
    assert os.path.isfile(header), (
        "DESTDIR install is incomplete: %s missing" % header)


def test_honours_cc_environment_variable(registry):
    """CC must select the compiler (§1.1)."""
    import shutil
    real = shutil.which("gcc") or shutil.which("cc")
    if real is None:
        pytest.fail("no C compiler found in the verifier image")
    b = registry.dynamic("cc-env", generator="Ninja", configure_only=True,
                         extra_env={"CC": real})
    if not b.configured:
        pytest.fail("configure with CC=%s failed:\n%s" % (real, b.failure_summary()))
    got = b.selected_compiler()
    assert got, "the configured tree records no C compiler"
    assert os.path.realpath(got) == os.path.realpath(real) or \
        os.path.basename(got) == os.path.basename(real), (
        "CC=%s was ignored; the cache selected %s" % (real, got))


def test_build_is_parallel_safe(b_default):
    """The matrix builds with -j; a broken dependency graph shows up as a failure."""
    assert b_default.built, (
        "the parallel build failed, which usually means the CMake dependency "
        "graph is incomplete:\n%s" % b_default.failure_summary())


def test_out_of_source_build_leaves_sources_clean(b_default, pre_build_snapshot):
    """§1.1: nothing may be written into the source tree by a build.

    The build copy started as a byte-for-byte copy of the snapshot, so any file
    that appeared, vanished or changed content is something the build wrote into
    the sources.

    This is the one check in the stage that legitimately looks at a source tree,
    and it is why the fixture is named for a snapshot rather than for the
    repository: the claim is about the difference between two states of it, and
    the second state is a build output. A check that only looked at the first
    state would be reading the repository, which is stage 1's job.
    """
    if not b_default.built:
        pytest.fail(b_default.failure_summary())

    def snapshot(root):
        out = {}
        for dirpath, dirnames, files in os.walk(root):
            dirnames[:] = [d for d in dirnames if d != ".git"]
            for f in files:
                p = os.path.join(dirpath, f)
                rel = os.path.relpath(p, root)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                out[rel] = st.st_size
        return out

    before, after = snapshot(pre_build_snapshot), snapshot(b_default.src)
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    resized = sorted(k for k in set(before) & set(after) if before[k] != after[k])
    assert (added, removed, resized) == ([], [], []), (
        "the build wrote into the source tree (§1.1) — added=%s removed=%s "
        "changed=%s" % (added[:8], removed[:8], resized[:8]))


def test_debug_build_type_supported(b_debug):
    assert b_debug.installed, (
        "CMAKE_BUILD_TYPE=Debug did not build and install (§1.1):\n%s"
        % b_debug.failure_summary())


def test_empty_build_type_supported(registry):
    b = registry.dynamic("no-buildtype", generator="Ninja", build_type=None)
    assert b.installed, (
        "a build with no CMAKE_BUILD_TYPE failed (§1.1):\n%s" % b.failure_summary())
