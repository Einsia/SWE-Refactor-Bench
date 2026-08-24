#!/usr/bin/env python3
"""Behavioural: the optimisation and hardening flags survive the port (§1.10).

`configure.ac` probes about fifteen compile and link flags with
AX_CHECK_COMPILE_FLAG / AX_CHECK_LINK_FLAG / AX_ADD_FORTIFY_SOURCE, and every
one of them is part of what State A ships: `-O3` decides whether the reference
implementations are usable at all, `-fno-strict-aliasing` and
`-fno-strict-overflow` keep the type-punning and wrap-around arithmetic in the
reference code defined, `-fvisibility=hidden` decides the exported symbol set,
`-fstack-protector` and `-D_FORTIFY_SOURCE=3` are the reason `libsodium.so.26`
calls `__explicit_bzero_chk` instead of `explicit_bzero`, and
`-Wl,-z,relro,now,noexecstack` are the reason its segments are what they are.

A port that forgets this block still passes every behavioural test — and quietly
ships a slower, less hardened library. So the contract is checked in two places
that a wrapper cannot fake:

  * on the compile line of *every* library translation unit, read from
    `compile_commands.json`, allowing each flag any of its accepted spellings
    (CMake spells some of them through POSITION_INDEPENDENT_CODE and
    C_VISIBILITY_PRESET, which is fine — the flag still has to arrive);
  * in the installed ELF objects: GNU_RELRO, BIND_NOW, a non-executable stack,
    and the fortified `__*_chk` libc entry points State A depends on.

The Debug configuration is checked too, for the opposite property: `--enable-debug`
turned optimisation and fortification *off*, and a port that hard-codes `-O3`
into CMAKE_C_FLAGS instead of probing gets that wrong.
"""
import json
import os
import re
import shlex

import pytest

import flavour

pytestmark = pytest.mark.behaviour

# data/ and lib/ live at the suite root, one level above modules/.  The
# runner exports SRB_SUITE_DIR; the fallback keeps a module runnable by hand.
SUITE = os.environ.get("SRB_SUITE_DIR") or os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import elfutil  # noqa: E402

with open(os.path.join(SUITE, "data", "hardening.json")) as _fh:
    HARD = json.load(_fh)
# The 117 C translation units State A compiles on this host, by their path under
# src/libsodium/. Not every .c in the tree: salsa20_ref.c and
# salsa20_xmm6int-sse2.c are the !HAVE_AMD64_ASM alternatives, and the two .S
# sources go through the assembler, which never saw CFLAGS.
UNITS = HARD["units_c"]
UNITS_ASM = HARD["units_asm"]

SEMANTICS = HARD["compile_semantics"]
ELF = HARD["elf"]
FORTIFY_WITNESSES = ELF["fortify_witnesses"]

# The dimensions whose absence is observable in the delivered binary rather than
# only on the command line; listed separately so the failure message can say so.
SAMPLE_SOURCES = 24          # per-flag spot check across translation units


def _tokens(cmd):
    try:
        return shlex.split(cmd)
    except ValueError:
        return cmd.split()


def _satisfied(cmd, spellings):
    """True when the command line carries any accepted spelling of a flag.

    Compared token-wise, so `-O3` does not match `-DFOO=-O3x` and
    `-D_FORTIFY_SOURCE=3` does not match `-D_FORTIFY_SOURCE=3x`. A multi-token
    spelling ("-U_FORTIFY_SOURCE -D_FORTIFY_SOURCE=2") must appear in order.
    """
    toks = _tokens(cmd)
    for spelling in spellings:
        want = spelling.split()
        if len(want) == 1:
            if want[0] in toks:
                return True
        else:
            n = len(want)
            for i in range(len(toks) - n + 1):
                if toks[i:i + n] == want:
                    return True
    return False


def _c_commands(build):
    """{source_relpath: compile command} for the library's C translation units.

    The .S sources are excluded: automake assembled them through CPPAS, whose
    flags were never CFLAGS, and CMake likewise drives them with the ASM
    compiler. Requiring -fvisibility=hidden on an assembler command line would
    be a demand State A does not meet either.
    """
    return {s: c for s, c in build.all_flags_per_source().items()
            if s.endswith(".c")}


@pytest.fixture(scope="session")
def lib_commands(b_default):
    """{source_relpath: compile command} for the library's translation units."""
    if not b_default.built:
        pytest.fail(b_default.failure_summary())
    cmds = _c_commands(b_default)
    if not cmds:
        pytest.fail("no compile database, so the compile flags cannot be read")
    return cmds


# ------------------------------------------------- flags reach every compile --

@pytest.mark.parametrize("dim", sorted(SEMANTICS))
def test_flag_reaches_the_library_compile_line(lib_commands, dim):
    """§1.10: each probed flag must arrive on the library's compile line."""
    spec = SEMANTICS[dim]
    ok = [src for src, cmd in lib_commands.items()
          if _satisfied(cmd, spec["any_of"])]
    assert ok, (
        "no library translation unit is compiled with any of %s (%s). State A "
        "compiles everything with %r; the CMake port must probe for it the same "
        "way, not drop it."
        % (spec["any_of"], spec["why"], HARD["state_a_cflags"]))


@pytest.mark.parametrize("dim", sorted(SEMANTICS))
def test_flag_reaches_every_translation_unit(lib_commands, dim):
    """The old CFLAGS applied to all 119 units, not to a favoured few."""
    spec = SEMANTICS[dim]
    missing = sorted(src for src, cmd in lib_commands.items()
                     if not _satisfied(cmd, spec["any_of"]))
    assert missing == [], (
        "%d of %d translation units are compiled without %s (%s), e.g. %s"
        % (len(missing), len(lib_commands), spec["any_of"], spec["why"],
           missing[:5]))


@pytest.mark.parametrize("nth", range(SAMPLE_SOURCES))
def test_sampled_translation_unit_carries_every_dimension(lib_commands, nth):
    """Per-source view of the same contract: one test per sampled unit."""
    names = sorted(lib_commands)
    if not names:
        pytest.fail("no library translation unit was compiled")
    # Spread the sample across the whole set instead of taking the first 24
    # alphabetically, which would only ever look at one corner of the tree.
    name = names[(nth * len(names)) // SAMPLE_SOURCES]
    cmd = lib_commands[name]
    missing = sorted(d for d, spec in SEMANTICS.items()
                     if not _satisfied(cmd, spec["any_of"]))
    assert missing == [], (
        "%s is compiled without %s" % (name, missing))


@pytest.mark.parametrize("unit", UNITS)
def test_named_translation_unit_is_fully_hardened(lib_commands, unit):
    """One check per translation unit State A compiles, by name.

    The aggregate tests above say "some unit lost a flag"; these say which. They
    also pin the unit list itself: a port that quietly stops compiling
    crypto_stream/salsa20/xmm6int/salsa20_xmm6int-avx2.c fails here rather than
    passing a check that only looks at whatever it did build.
    """
    cmd = lib_commands.get(unit)
    if cmd is None:
        # Fall back to a basename match: the compile database keys on the path
        # the port chose, which may differ in casing of intermediate dirs.
        base = os.path.basename(unit)
        cand = [c for s, c in lib_commands.items() if os.path.basename(s) == base]
        if not cand:
            pytest.fail("%s is not in the compile database; State A compiles it"
                        % unit)
        cmd = cand[0]
    missing = sorted(d for d, spec in SEMANTICS.items()
                     if not _satisfied(cmd, spec["any_of"]))
    assert missing == [], ("%s is compiled without %s" % (unit, missing))


@pytest.mark.parametrize("unit", UNITS_ASM)
def test_assembler_source_is_still_assembled(b_default, unit):
    """The .S units are exempt from CFLAGS, not from being built.

    Excluding them from the flag checks would be a blind spot if a port could
    silently drop them, so they are pinned here instead: State A assembles both
    through CPPAS, and the resulting objects are in the library.
    """
    if not b_default.built:
        pytest.fail(b_default.failure_summary())
    seen = set(b_default.all_flags_per_source())
    assert unit in seen, (
        "%s is not in the compile database; State A assembles it into "
        "libsodium (its symbols are part of the ABI)" % unit)


@pytest.mark.parametrize("dim", sorted(SEMANTICS))
def test_flag_also_reaches_the_makefiles_generator(b_make, dim):
    """§1.1: the two generators must configure identically, flags included."""
    if not b_make.built:
        pytest.fail("Unix Makefiles build failed:\n%s" % b_make.failure_summary())
    cmds = _c_commands(b_make)
    if not cmds:
        pytest.fail("the Unix Makefiles build produced no compile database")
    spec = SEMANTICS[dim]
    missing = sorted(src for src, cmd in cmds.items()
                     if not _satisfied(cmd, spec["any_of"]))
    assert missing == [], (
        "with -G 'Unix Makefiles', %d translation units lose %s, e.g. %s — the "
        "flags are attached to a generator instead of to the project"
        % (len(missing), spec["any_of"], missing[:4]))


@pytest.mark.parametrize("dim", sorted(SEMANTICS))
def test_flag_also_reaches_the_minimal_build(b_minimal, dim):
    """--enable-minimal changed the API surface, not the hardening."""
    if not b_minimal.built:
        pytest.fail(b_minimal.failure_summary())
    cmds = _c_commands(b_minimal)
    if not cmds:
        pytest.fail("the minimal build produced no compile database")
    spec = SEMANTICS[dim]
    missing = sorted(src for src, cmd in cmds.items()
                     if not _satisfied(cmd, spec["any_of"]))
    assert missing == [], (
        "SODIUM_MINIMAL=ON drops %s from %d translation units, e.g. %s"
        % (spec["any_of"], len(missing), missing[:4]))


#: The one dimension State A deliberately drops when the user supplies CFLAGS.
#: `configure.ac:44` records whether CFLAGS came from the environment
#: (`sodium_CFLAGS=${CFLAGS+set}`) and `:53` gates the whole -O3/-O2/-O1/-O probe
#: chain on it being unset. That is the Autotools convention -- the user's
#: optimisation choice wins over the package's -- and it is deliberate, so
#: `./configure CFLAGS=-DSRB_USER_FLAG=1` yields no -O at all. Every other
#: dimension is appended after that block and does survive.
USER_CFLAGS_SUPPRESSES = {"optimisation"}


@pytest.mark.srb_skip_ok
@pytest.mark.parametrize("dim", sorted(SEMANTICS))
def test_flag_survives_user_supplied_cflags(registry, dim):
    """CMAKE_C_FLAGS from the user must add to the project's flags, not replace them.

    `./configure CFLAGS=-DFOO` appended to the probed set; a port that assigns
    to CMAKE_C_FLAGS instead of using add_compile_options loses everything the
    moment a user passes a flag of their own.

    One dimension is licensed away for an Autotools delivery and only there --
    see `USER_CFLAGS_SUPPRESSES`. The requirement on State B is the stronger one
    and stays scored for all seven dimensions: CMake has no equivalent of that
    conditional, `add_compile_options` composes with CMAKE_C_FLAGS rather than
    being replaced by it, and a port that loses -O3 the moment a packager passes
    a flag has lost the capability whatever State A's convention was.
    """
    if dim in USER_CFLAGS_SUPPRESSES and not flavour.is_cmake():
        pytest.skip(
            "%s: State A suppresses this on purpose when CFLAGS is supplied "
            "(configure.ac:44 records that it was, :53 gates the -O probe chain "
            "on it having been unset), so there is no flag here to survive" % dim)
    b = registry.dynamic("harden-userflags", generator="Ninja",
                         options={"CMAKE_C_FLAGS": "-DSRB_USER_FLAG=1"})
    if not b.built:
        pytest.fail(b.failure_summary())
    cmds = _c_commands(b)
    if not cmds:
        pytest.fail("no compile database")
    spec = SEMANTICS[dim]
    missing = sorted(src for src, cmd in cmds.items()
                     if not _satisfied(cmd, spec["any_of"]))
    assert missing == [], (
        "passing CMAKE_C_FLAGS=-DSRB_USER_FLAG=1 removed %s from %d translation "
        "units, e.g. %s" % (spec["any_of"], len(missing), missing[:4]))


def test_user_supplied_cflags_are_honoured(registry):
    b = registry.dynamic("harden-userflags", generator="Ninja",
                         options={"CMAKE_C_FLAGS": "-DSRB_USER_FLAG=1"})
    if not b.built:
        pytest.fail(b.failure_summary())
    cmds = _c_commands(b)
    if not cmds:
        pytest.fail("no compile database")
    bad = sorted(s for s, c in cmds.items() if "SRB_USER_FLAG" not in c)
    assert bad == [], (
        "CMAKE_C_FLAGS did not reach %d translation units, e.g. %s"
        % (len(bad), bad[:4]))


# --------------------------------------------- the probes are probes, not text --

# §1.10: the hardening flags have to be discovered rather than assumed, because a
# hardcoded `-fstack-protector` breaks on any compiler that does not have it --
# which is the breakage AX_CHECK_COMPILE_FLAG existed to prevent.
#
# A string cannot check that.  Requiring `check_c_compiler_flag` to appear in some
# CMake file fails a tree that probes through a helper of its own, or through
# `try_compile` directly, or through a module it wrote, and passes one that wrote
# `# TODO: use check_c_compiler_flag`.  Requiring each flag in CMakeCache.txt as
# part of an INTERNAL variable's *name* asks how that variable was named, not
# whether anything was probed.
#
# So it is measured as a behaviour, below, by configuring under a compiler that
# rejects the flag.  Whether the *approach* to detection is real detection rather
# than a copied conclusion is stage 1's `detection_is_real` gate, which reads both
# build systems.


def _tokens(command):
    """A compile command's arguments, so a flag can be matched exactly.

    Substring matching would make `-fstack-protector-strong` an occurrence of
    `-fstack-protector`, and a submission that probed, found the plain flag
    missing and fell back to the strong one has done nothing wrong.
    """
    return set(command.replace("\t", " ").split())


@pytest.mark.parametrize("source", [
    "crypto_box/crypto_box.c",
    "crypto_generichash/blake2b/ref/blake2b-ref.c",
    "sodium/core.c",
])
def test_rejected_hardening_flag_is_not_passed(p_nohardening, source):
    """§1.10: a probed flag disappears when the compiler will not take it.

    The wrapper this configuration runs under exits 1 the moment
    `-fstack-protector` appears in its argv. So:

      * a build system that probes runs its test compile, sees it fail, concludes
        the flag is unavailable, and leaves it out -- every real compile then
        succeeds, and the flag is absent from the compile line;
      * a build system that writes the flag into CMAKE_C_FLAGS cannot get this
        far. CMake compiles its own ABI-detection program with CMAKE_C_FLAGS
        before it reaches any of the project's targets, so configure fails
        outright.

    Both outcomes are forced by what a probe *is*, which is why this needs no
    recorded State-A reaction the way the ISA probes do.
    """
    if not p_nohardening.configured:
        pytest.fail(
            "configuring under a compiler that rejects -fstack-protector "
            "failed. §1.10 requires the hardening flags to be probed for: a "
            "probe finds the flag unavailable and moves on, so this "
            "configuration must succeed. Failing here is the signature of the "
            "flag being written into CMAKE_C_FLAGS unconditionally.\n"
            + p_nohardening.failure_summary())
    cmds = _c_commands(p_nohardening)
    if not cmds:
        pytest.fail(
            "no compile database after configuring under the no-hardening "
            "probe, so what reached the compiler cannot be read")
    match = [c for s, c in cmds.items() if s.endswith(source)]
    if not match:
        pytest.skip(f"{source} is not a translation unit in this configuration")
    assert "-fstack-protector" not in _tokens(match[0]), (
        f"{source} is still compiled with -fstack-protector under a compiler "
        f"that rejects it, so the flag is asserted rather than probed (§1.10). "
        f"Command: {match[0][:400]}")


def test_hardening_probe_configuration_still_builds_something(p_nohardening):
    """The probe must remove the flag, not the build.

    A submission could pass the check above by refusing to configure anything at
    all under an unfamiliar compiler. This asks that the configuration produced a
    real compile database with roughly libsodium's number of translation units,
    so "it dropped the flag" means the build survived without it.
    """
    if not p_nohardening.configured:
        pytest.fail(p_nohardening.failure_summary())
    cmds = _c_commands(p_nohardening)
    assert len(cmds) >= 100, (
        f"configuring without -fstack-protector produced {len(cmds)} "
        f"translation units; libsodium has 119, so this build lost most of "
        f"itself rather than one flag")


def test_hardening_flags_still_present_when_the_compiler_takes_them(b_default):
    """The other half: with a normal compiler the flag is there.

    Together with the probe above this is the whole of "probed, not hardcoded" as
    a behaviour -- present when available, absent when not -- and neither half
    says anything about how the build system spells its probe.
    """
    cmds = _c_commands(b_default)
    if not cmds:
        pytest.fail("no compile database")
    without = sorted(s for s, c in cmds.items()
                     if "-fstack-protector" not in _tokens(c))
    assert without == [], (
        f"{len(without)} translation units compile without -fstack-protector on "
        f"a compiler that supports it, e.g. {without[:4]}; State A hardens every "
        f"unit")


# --------------------------------------------- evidence in the shipped binary --

@pytest.fixture(scope="session")
def installed_so(b_default):
    if not b_default.installed:
        pytest.fail(b_default.failure_summary())
    lib = b_default.shared_lib()
    if lib is None:
        pytest.fail("no shared library in the install tree")
    return lib


def test_installed_library_has_gnu_relro(installed_so):
    assert elfutil.has_gnu_relro(installed_so), (
        "libsodium.so has no GNU_RELRO segment; State A links with "
        "%s" % HARD["state_a_ldflags"])


def test_installed_library_has_bind_now(installed_so):
    """Full RELRO: -Wl,-z,now, so the GOT is read-only after startup."""
    assert elfutil.has_bind_now(installed_so), (
        "libsodium.so is not linked with -Wl,-z,now (no BIND_NOW / DF_BIND_NOW "
        "in the dynamic section); State A links with %s"
        % HARD["state_a_ldflags"])


def test_installed_library_stack_is_not_executable(installed_so):
    flags = elfutil.stack_flags(installed_so)
    assert not elfutil.stack_is_executable(installed_so), (
        "libsodium.so's GNU_STACK segment is %s — executable; State A links "
        "with -Wl,-z,noexecstack" % flags)


def test_installed_library_has_no_text_relocations(installed_so):
    assert not elfutil.has_textrel(installed_so), (
        "libsodium.so has text relocations, so it was not built as position "
        "independent code")


@pytest.mark.parametrize("sym", FORTIFY_WITNESSES)
def test_fortified_libc_entry_point_is_used(installed_so, sym):
    """_FORTIFY_SOURCE rewrites memcpy/memset/explicit_bzero into __*_chk.

    This is the end-to-end consequence of AX_ADD_FORTIFY_SOURCE: State A's
    libsodium.so.26 imports %s. A port that drops the macro imports the
    unchecked variant instead, and the difference is visible in `nm -D`.
    """
    undef = elfutil.dynamic_undefined(installed_so)
    assert sym in undef, (
        "libsodium.so does not import %s. State A does, because it is compiled "
        "with %r — the fortification level was lost in the port"
        % (sym, HARD["state_a_cppflags"]))


@pytest.mark.srb_skip_ok
@pytest.mark.parametrize("sym", FORTIFY_WITNESSES)
def test_unfortified_variant_is_not_imported_instead(installed_so, sym):
    """The unchecked twin must not be what the library asks for."""
    plain = sym[2:-4] if sym.startswith("__") and sym.endswith("_chk") else None
    if plain is None:
        pytest.skip("%s has no unchecked twin" % sym)
    undef = elfutil.dynamic_undefined(installed_so)
    if sym in undef:
        return          # fortified call present; a stray plain call is harmless
    assert plain not in undef, (
        "libsodium.so imports the unchecked %s where State A imports %s; "
        "_FORTIFY_SOURCE is not in effect" % (plain, sym))


def test_test_binaries_are_hardened_too(b_default):
    """The old LDFLAGS covered the test programs as well."""
    if not b_default.built:
        pytest.fail(b_default.failure_summary())
    bins = []
    for dirpath, _d, files in os.walk(b_default.bld):
        for f in files:
            p = os.path.join(dirpath, f)
            if os.access(p, os.X_OK) and not os.path.isdir(p) and \
                    elfutil.file_type(p).startswith("ELF") and \
                    "executable" in elfutil.file_type(p):
                bins.append(p)
        if len(bins) >= 5:
            break
    if not bins:
        pytest.fail("no test executable found in the build tree")
    bad = [b for b in bins[:5] if not elfutil.has_gnu_relro(b)]
    assert bad == [], (
        "%d test executable(s) are linked without RELRO, e.g. %s — the link "
        "flags are attached to the library target only, not to the project"
        % (len(bad), os.path.basename(bad[0])))


@pytest.mark.srb_skip_ok
@pytest.mark.parametrize("fixture_name", ["b_shared_only", "b_minimal"])
def test_other_configurations_are_hardened(request, fixture_name):
    b = request.getfixturevalue(fixture_name)
    if not b.installed:
        pytest.fail(b.failure_summary())
    lib = b.shared_lib()
    if lib is None:
        pytest.skip("configuration %s installs no shared library" % fixture_name)
    problems = []
    if not elfutil.has_gnu_relro(lib):
        problems.append("no GNU_RELRO")
    if not elfutil.has_bind_now(lib):
        problems.append("no BIND_NOW")
    if elfutil.stack_is_executable(lib):
        problems.append("executable stack")
    assert problems == [], (
        "the %s configuration ships a less hardened library: %s"
        % (fixture_name, ", ".join(problems)))


# ------------------------------------------ the debug configuration backs off --

def test_debug_build_is_not_fortified(b_debug):
    """`--enable-debug` set -DDEBUG=1 -U_FORTIFY_SOURCE (configure.ac:215).

    A port that hard-codes -D_FORTIFY_SOURCE=3 unconditionally cannot express
    this, and fortification without optimisation is exactly the configuration
    glibc warns about.
    """
    if not b_debug.built:
        pytest.fail(b_debug.failure_summary())
    cmds = _c_commands(b_debug)
    if not cmds:
        pytest.fail("the Debug build produced no compile database")
    fortified = sorted(s for s, c in cmds.items()
                       if _satisfied(c, ["-D_FORTIFY_SOURCE=2",
                                         "-D_FORTIFY_SOURCE=3"]))
    # Plain `-O` counts. State A's own debug line is `-O -g3` (configure.ac:215
    # sets CFLAGS="$nxflags -O -g3"), and `-O` is the last rung of the probe chain
    # AX_CHECK_COMPILE_FLAG walks in the default configuration. Leaving it out
    # made this check read "State A fortifies without optimising", which is the
    # opposite of what its own compile line says.
    optimised = sorted(s for s, c in cmds.items()
                       if _satisfied(c, ["-O", "-O1", "-O2", "-O3", "-Ofast",
                                         "-Os", "-Og", "-Oz"]))
    assert not (fortified and not optimised), (
        "the Debug build fortifies (%d units) without optimising; "
        "_FORTIFY_SOURCE requires -O and glibc warns about the combination"
        % len(fortified))


def test_debug_build_still_hides_symbols(b_debug):
    """Visibility is not a hardening flag; it decides the ABI in every config."""
    if not b_debug.built:
        pytest.fail(b_debug.failure_summary())
    cmds = _c_commands(b_debug)
    if not cmds:
        pytest.fail("the Debug build produced no compile database")
    missing = sorted(s for s, c in cmds.items()
                     if not _satisfied(c, ["-fvisibility=hidden"]))
    assert missing == [], (
        "%d units in the Debug build lose -fvisibility=hidden, e.g. %s; the "
        "exported symbol set must not depend on CMAKE_BUILD_TYPE"
        % (len(missing), missing[:4]))


def test_debug_build_is_debuggable(b_debug):
    if not b_debug.built:
        pytest.fail(b_debug.failure_summary())
    cmds = _c_commands(b_debug)
    if not cmds:
        pytest.fail("the Debug build produced no compile database")
    missing = sorted(s for s, c in cmds.items()
                     if not _satisfied(c, ["-g", "-g3", "-ggdb", "-g2"]))
    assert missing == [], (
        "-DCMAKE_BUILD_TYPE=Debug produced %d units without debug info, e.g. %s"
        % (len(missing), missing[:4]))


@pytest.mark.srb_skip_ok
def test_ssp_can_be_turned_off(registry):
    """`./configure --disable-ssp` had to keep working; so must its CMake twin.

    §1.10 requires the capability -- a build that does not want the stack
    protector can say so -- without saying what the port must call its
    replacement, so the switch is looked up rather than assumed:
    `Build.feature_switch()` reads the cache for a CMake tree and
    `configure --help` for an Autotools one, and answers with the name and the
    value that turns it off. A build system with no such control simply has
    nothing to turn off, which this records as a skip.
    """
    base = registry.get("ninja-default")
    found = base.feature_switch(r"SSP", r"STACK")
    if found is None:
        pytest.skip("the delivered build system exposes no stack-protector "
                    "switch (permitted)")
    name, off = found
    b = registry.dynamic("harden-nossp", generator="Ninja",
                         options={name: off})
    if not b.built:
        pytest.fail("%s=%s does not build:\n%s" % (name, off, b.failure_summary()))
    cmds = _c_commands(b)
    if not cmds:
        pytest.fail("no compile database")
    still = sorted(s for s, c in cmds.items()
                   if _satisfied(c, ["-fstack-protector",
                                     "-fstack-protector-strong",
                                     "-fstack-protector-all"]))
    assert still == [], (
        "%s=%s still compiles %d units with -fstack-protector, e.g. %s"
        % (name, off, len(still), still[:4]))
