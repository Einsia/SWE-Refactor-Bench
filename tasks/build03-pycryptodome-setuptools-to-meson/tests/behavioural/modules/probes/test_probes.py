"""Does the build system actually ask the compiler anything.

State A's `compiler_opt.py` compiles a small program for each capability it wants
-- AES-NI intrinsics, PCLMUL intrinsics, `__uint128_t`, `posix_memalign`,
`cpuid.h` -- and lets the answers decide which extension modules get built at
all. That is the difference between a build system and a transcript of one
successful build.

The way to measure it is to take the capability away and see whether the build
notices. Three configurations, built by the `build` module, whose compiler rejects
`-maes`, rejects `-mpclmul -mssse3`, or rejects all three. A build system that
asks produces a wheel with 39 objects instead of 41 and still passes the test
suite, because pycryptodome has portable implementations behind both fast paths.
A build system that hardcoded State A's answers fails to compile.

Both outcomes are informative, and this module reports which one happened. What it
never does is read the build files: "does this Meson file call
`cc.has_argument`" is a question about text, answerable by writing the string in
a comment, and it does not distinguish a build that calls it from one that calls
it and ignores the result. The wheel's member list does.

The reverse failure is also here. A build whose artefact set does not change when
the compiler loses -maes has either hardcoded the answer or applied the flag
globally, and the second means the 39 remaining objects now contain AES-NI
instructions -- which the isa module measures on the default build and this one
measures again on the crippled ones, because that is where the mistake shows.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import elfutil
import wheelutil


def _ground_truth() -> dict:
    root = Path(os.environ.get("SRB_SUITE_DIR", "/tests/behavioural")) / "data"
    return json.loads((root / "probe_honesty.json").read_text(encoding="utf-8"))


HONESTY = _ground_truth()

#: configuration name -> the objects it must stop producing, by basename.
#: From the frozen record: crippling all three drops both, and each flag drops
#: the one object that needed it.
DROPS = {
    "no-aesni": ("_raw_aesni.abi3.so",),
    "no-clmul": ("_ghash_clmul.abi3.so",),
    "no-isa": tuple(HONESTY["crippled_compiler"]["absent"]),
}

#: What must survive every crippling: the portable implementations the fast paths
#: fall back to. If these go missing the build did not degrade, it broke.
SURVIVES = tuple(HONESTY["crippled_compiler"]["still_present"])


@pytest.fixture(scope="module")
def full(built):
    return Path(built("default").wheel)


def _payload_basenames(wheel_path: Path) -> set:
    wheel = wheelutil.Wheel(wheel_path)
    try:
        return {n.rsplit("/", 1)[-1] for n in wheel.by_suffix(".so")}
    finally:
        wheel.close()


def test_the_honesty_ground_truth_is_present():
    """41 objects with the compiler, 39 without. Measured on both states."""
    assert HONESTY["full_compiler"]["n_so"] == 41
    assert HONESTY["crippled_compiler"]["n_so"] == 39
    assert sorted(HONESTY["crippled_compiler"]["rejects_flags"]) == ["-maes", "-mpclmul", "-mssse3"]


def test_the_full_build_has_both_fast_paths(full):
    """The baseline this module compares against. 41 objects, both present."""
    names = _payload_basenames(full)
    assert len(names) == HONESTY["full_compiler"]["n_so"], (
        f"the default configuration produced {len(names)} shared objects, "
        f"State A produces {HONESTY['full_compiler']['n_so']}"
    )
    missing = [n for n in HONESTY["full_compiler"]["present"] if n not in names]
    assert not missing, (
        f"the default configuration did not produce {', '.join(missing)}, so there is no "
        f"fast path for the crippled configurations to drop"
    )


@pytest.mark.migration
@pytest.mark.parametrize("configuration", sorted(DROPS))
def test_the_build_notices_a_compiler_without_the_instruction(configuration, ledger, full):
    """The measurement. Take the flag away; the artefact set has to respond.

    A configuration that failed to build is reported as exactly that, with its
    log, rather than being read as "it noticed". Hardcoding State A's answers
    usually *does* fail here -- the compile of AESNI.c dies on the intrinsic --
    and that failure is a truthful, distinct result from a build that dropped the
    object deliberately.
    """
    rec = ledger.get(configuration)
    assert rec is not None, f"the build module recorded nothing for {configuration!r}"
    assert rec.ok, (
        f"the build failed outright when the compiler rejected "
        f"{', '.join(rec.rejects)}: {rec.note or 'no note'} (log: {rec.log}). A build system "
        f"that probes drops the affected object and carries on; one that assumes the "
        f"instruction is available cannot compile without it."
    )

    names = _payload_basenames(Path(rec.wheel))
    still_there = [n for n in DROPS[configuration] if n in names]
    assert not still_there, (
        f"with the compiler rejecting {', '.join(rec.rejects)}, the build still produced "
        f"{', '.join(still_there)}. Either the flag was never passed -- in which case the "
        f"object contains no fast path in any configuration -- or the build ignored what "
        f"the compiler said."
    )

    gone = [n for n in SURVIVES if n not in names]
    assert not gone, (
        f"with the compiler rejecting {', '.join(rec.rejects)}, the build also lost "
        f"{', '.join(gone)}. Those are the portable implementations; without them the "
        f"library has no fallback and this is a broken build rather than a degraded one."
    )


@pytest.mark.migration
def test_the_fully_crippled_build_matches_state_a_exactly(ledger):
    """39 objects, and the same 39. State A's response to the same compiler.

    The per-configuration checks above say the two ISA objects went away; this one
    says nothing *else* went away with them, which is what distinguishes a
    build that degraded from a build that gave up on whatever else it could not
    figure out.
    """
    rec = ledger.get("no-isa")
    assert rec is not None and rec.ok, (
        f"the fully crippled configuration did not build: "
        f"{(rec.note if rec else 'not attempted')}"
    )
    names = _payload_basenames(Path(rec.wheel))
    assert len(names) == HONESTY["crippled_compiler"]["n_so"], (
        f"a compiler without -maes, -mpclmul or -mssse3 produced {len(names)} shared objects; "
        f"State A produces {HONESTY['crippled_compiler']['n_so']} under the same compiler"
    )


@pytest.mark.parametrize("configuration", sorted(DROPS))
def test_the_crippled_build_has_no_instruction_it_could_not_compile(configuration, ledger, built):
    """The flattening check, on the configuration where flattening is fatal.

    If the ISA flags were applied globally and the probe was skipped, the build
    with -maes rejected either fails or produces 39 objects that are generic --
    fine. But a build that dropped the *probe* while keeping a hardcoded global
    -maes on everything else produces objects full of AESENC on a compiler that
    was supposed not to support it. Disassembling the crippled wheel's install is
    how that shows up.
    """
    rec = ledger.get(configuration)
    if rec is None or not rec.ok or not rec.install:
        pytest.fail(
            f"{configuration} produced no install tree to inspect: "
            f"{(rec.note if rec else 'not attempted')}"
        )
    rejected_families = {
        "-maes": "aesni", "-mpclmul": "clmul", "-mssse3": "ssse3",
    }
    forbidden = {rejected_families[f] for f in rec.rejects if f in rejected_families}
    offenders = {}
    for path in sorted(Path(rec.install).rglob("*.so")):
        found = elfutil.isa_families(path) & forbidden
        if found:
            offenders[path.name] = sorted(found)
    assert not offenders, (
        f"the compiler rejected {', '.join(rec.rejects)}, yet these objects contain the "
        f"corresponding instructions: {offenders}. The instructions cannot have come from "
        f"a compiler that refused the flag, so they came from an assembly file or an "
        f"unconditional intrinsic -- and they will fault on a CPU without the feature."
    )
