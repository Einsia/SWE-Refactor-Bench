#!/usr/bin/env python3
"""Audit: feature detection is real detection, not a copied macro list.

Contract §1.4. The verifier re-configures the submission with compilers that
genuinely lack a capability and requires the conclusion to change. Every
expectation here was *observed* by running State A's own `configure` under the
same wrappers, so a submission that probes the way Autotools probes reacts the
same way, and one that hardcodes cannot.

  * `probe-nosysrandom` — a compiler that cannot see <sys/random.h>.
    State A drops HAVE_SYS_RANDOM_H, HAVE_GETRANDOM (and HAVE_GETENTROPY).
  * `probe-noavx512`   — a compiler that rejects -mavx512f.
    State A stops passing -mavx512f to the AVX-512 unit but still builds.
  * `probe-noaes`      — a compiler that rejects -maes and -mpclmul.
    State A stops passing them to the three AES-NI units.

Note what is *not* required: rejecting -mavx512f does not drop
HAVE_AVX512FINTRIN_H, because that probe compiles with `#pragma GCC target`
and succeeds regardless. The verifier demands exactly the reaction the reference
build system has, no more.
"""
import json
import os

import pytest

pytestmark = pytest.mark.migration

# data/ and lib/ live at the suite root, one level above modules/.  The
# runner exports SRB_SUITE_DIR; the fallback keeps a module runnable by hand.
SUITE = os.environ.get("SRB_SUITE_DIR") or os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
with open(os.path.join(SUITE, "data", "probes.json")) as _fh:
    PROBES = json.load(_fh)

NOSYS = PROBES["nosysrandom"]
NOAVX = PROBES["noavx512"]
NOAES = PROBES["noaes"]


def _macros(build, label):
    if not build.configured:
        pytest.fail(
            "configuring under the %s probe compiler failed. §1.4 requires the "
            "build to *probe* the toolchain; it must still configure when a "
            "capability is absent.\n%s" % (label, build.failure_summary()))
    m = build.macro_map()
    if not m:
        pytest.fail(
            "no compile database after configuring under the %s probe, so the "
            "macro set cannot be observed" % label)
    return m


def _flags(build, label):
    if not build.configured:
        pytest.fail("configuring under the %s probe failed:\n%s"
                    % (label, build.failure_summary()))
    f = build.flags_per_source()
    if not f:
        pytest.fail("no compile database after configuring under %s" % label)
    out = {}
    for rel, flags in f.items():
        stem = os.path.basename(rel)
        for suf in (".c", ".S"):
            if stem.endswith(suf):
                stem = stem[: -len(suf)]
        out.setdefault(stem, set()).update(flags)
    return out


# ------------------------------------------------------- missing header probe --
@pytest.mark.parametrize("macro", NOSYS["macros_must_drop"])
def test_missing_sys_random_drops_macro(p_nosysrandom, macro):
    macros = _macros(p_nosysrandom, "nosysrandom")
    assert macro not in macros, (
        "%s is still defined when the compiler cannot see <sys/random.h>. "
        "State A's configure drops it. A hardcoded macro list looks exactly like "
        "this (§1.4)." % macro)


@pytest.mark.parametrize("macro", NOSYS["macros_must_keep"])
def test_missing_sys_random_keeps_unrelated_macro(p_nosysrandom, macro):
    """The probe must be surgical: unrelated conclusions must not collapse."""
    macros = _macros(p_nosysrandom, "nosysrandom")
    assert macro in macros, (
        "%s disappeared when only <sys/random.h> was hidden; detection is "
        "reacting to the wrong thing" % macro)


def test_missing_sys_random_changes_the_macro_set(p_nosysrandom, b_default):
    if not b_default.configured:
        pytest.fail(b_default.failure_summary())
    base = set(b_default.macro_map())
    probed = set(_macros(p_nosysrandom, "nosysrandom"))
    assert probed != base, (
        "the macro set is byte-identical whether or not <sys/random.h> exists — "
        "feature detection is not happening (§1.4)")
    assert base - probed, (
        "hiding <sys/random.h> removed nothing from the macro set (§1.4)")


# ------------------------------------------------------- rejected flag probes --
@pytest.mark.parametrize("source", sorted(NOAVX["sources_must_lose"]))
def test_rejected_avx512_flag_is_not_passed(p_noavx512, source):
    lost = NOAVX["sources_must_lose"][source]
    flags = _flags(p_noavx512, "noavx512").get(source, set())
    still = sorted(set(lost) & flags)
    assert still == [], (
        "%s is still compiled with %s even though the compiler rejects it; "
        "§1.4 requires check_c_compiler_flag-style probing before a flag is used"
        % (source, still))


@pytest.mark.parametrize("source", sorted(NOAES["sources_must_lose"]))
def test_rejected_aes_flag_is_not_passed(p_noaes, source):
    lost = NOAES["sources_must_lose"][source]
    flags = _flags(p_noaes, "noaes").get(source, set())
    still = sorted(set(lost) & flags)
    assert still == [], (
        "%s is still compiled with %s even though the compiler rejects it (§1.4)"
        % (source, still))


def test_flag_probe_changes_something(p_noavx512, b_default):
    if not b_default.built:
        pytest.fail(b_default.failure_summary())
    base = _flags(b_default, "default")
    probed = _flags(p_noavx512, "noavx512")
    assert base != probed, (
        "the per-file flag assignment is identical whether or not the compiler "
        "accepts -mavx512f — the flags are hardcoded (§1.4)")


def test_probe_configs_still_configure(p_nosysrandom, p_noavx512, p_noaes):
    """A build that only works on a perfect toolchain has not been ported."""
    for b, label in ((p_nosysrandom, "nosysrandom"), (p_noavx512, "noavx512"),
                     (p_noaes, "noaes")):
        assert b.configured, (
            "configure failed under the %s probe compiler:\n%s"
            % (label, b.failure_summary()))


# Whether the submission "uses CMake's feature-check modules" was a check here,
# and it was a scan for eight `Check*` module names -- a proxy that a copied macro
# list passes by adding one unused `include(CheckIncludeFile)`, and that a build
# probing correctly through its own helper fails. The question it was standing in
# for is stage 1's. What is left in this file is the evidence stage 1 cannot get:
# three configures under a crippled compiler, and the macros and flags that came
# out of them.
