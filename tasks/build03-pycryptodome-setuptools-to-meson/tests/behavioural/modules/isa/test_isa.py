"""Which instruction sets ended up in which object.

pycryptodome compiles 41 of its 47 translation units with no ISA flags beyond
-msse2, and exactly two with more: `_raw_aesni` gets -maes, `_ghash_clmul` gets
-mpclmul -mssse3. Both are loaded only after `Crypto.Util._cpuid` has asked the
CPU whether it can run them. That is the entire design: one wheel, portable, with
fast paths taken at runtime.

A build system that applies the ISA flags globally produces a wheel that passes
every test on the machine that built it and dies with SIGILL on anything older --
including, routinely, a CI runner or a cloud instance from a different
generation. It is the single most likely way to arrive at something that builds,
installs, and is wrong, and no behavioural test on the build machine can see it.

So this module disassembles what was delivered and asks which objects contain the
instructions. Not the compile line, and not the build system's description of
itself: the flag is a means, the instruction in the binary is the outcome, and
only the outcome is comparable between a distutils build and whatever the
submission wrote.

The reverse mistake gets its own check. An object with no AES-NI in it at all
means the fast path was dropped, which is not a crash -- it is the migration
quietly throwing away the reason those files exist.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import elfutil


def _ground_truth() -> dict:
    root = Path(os.environ.get("SRB_SUITE_DIR", "/tests/behavioural")) / "data"
    build = json.loads((root / "build.json").read_text(encoding="utf-8"))
    elf = json.loads((root / "elf.json").read_text(encoding="utf-8"))
    return build, elf


BUILD, ELF = _ground_truth()
EXPECTED_OBJECTS = sorted(ELF)

#: State A's own answer, from the frozen build record: the two modules whose
#: compile lines carry more than the global -msse2, and what they carry.
#:   {"Crypto.Cipher._raw_aesni": ["-maes", "-msse2"], ...}
ISA_ELEVATED = BUILD["isa_elevated"]

#: Which instruction family each elevated flag licenses. The mapping is the
#: point of the flag: -maes exists so the compiler may emit AESENC.
FLAG_FAMILY = {"-maes": "aesni", "-mpclmul": "clmul", "-mssse3": "ssse3"}

#: module name -> the families its object is allowed to contain, and the ones it
#: is expected to contain. Derived, not typed: State A's compile lines decide.
ELEVATED_FAMILIES = {
    module: {FLAG_FAMILY[f] for f in flags if f in FLAG_FAMILY}
    for module, flags in ISA_ELEVATED.items()
}


def _module_of(rel: str) -> str:
    """`Crypto/Cipher/_raw_aesni.abi3.so` -> `Crypto.Cipher._raw_aesni`."""
    head, tail = rel.rsplit("/", 1)
    return head.replace("/", ".") + "." + tail.split(".")[0]


@pytest.fixture(scope="module")
def install(built) -> Path:
    return Path(built("default").install)


def test_the_partition_ground_truth_is_present():
    """Two elevated modules, and the families they are for."""
    assert set(ISA_ELEVATED) == {"Crypto.Cipher._raw_aesni", "Crypto.Hash._ghash_clmul"}, (
        f"State A's frozen record elevates {sorted(ISA_ELEVATED)}; this module was written "
        f"for _raw_aesni and _ghash_clmul"
    )
    assert ELEVATED_FAMILIES["Crypto.Cipher._raw_aesni"] == {"aesni"}
    assert ELEVATED_FAMILIES["Crypto.Hash._ghash_clmul"] == {"clmul", "ssse3"}


@pytest.mark.parametrize("rel", EXPECTED_OBJECTS)
def test_object_contains_no_instruction_it_was_not_licensed_for(rel, install):
    """The portability half. An AESENC in the wrong object is a SIGILL waiting for older hardware.

    Reported per object so the failure names the file, because the fix differs:
    an object that should never have had the flag needs the flag removed, and 39
    objects that all grew it need the flag moved off the global argument list.
    """
    path = install / rel
    assert path.is_file(), f"{rel} was not installed"
    module = _module_of(rel)
    allowed = ELEVATED_FAMILIES.get(module, set())
    # avx/avx512 are in the scanner's table but nothing in this project is
    # licensed for them, so they are unexpected everywhere.
    found = elfutil.isa_families(path)
    unlicensed = sorted(found - allowed)
    assert not unlicensed, (
        f"{rel} contains {', '.join(unlicensed)} instructions. State A compiles "
        f"{module} with {ISA_ELEVATED.get(module) or 'no ISA flag beyond the global -msse2'}, "
        f"and this object is loaded without a CPUID check for those families -- so on a CPU "
        f"that lacks them, importing the module that uses it crashes the interpreter."
    )


@pytest.mark.parametrize("module", sorted(ELEVATED_FAMILIES))
def test_the_fast_path_is_still_compiled(module, install):
    """The other half. The two elevated objects must actually contain their instructions.

    A build that never passed -maes still produces a loadable `_raw_aesni` -- the
    C compiles without the intrinsics enabled only if the source guards them, and
    pycryptodome's does not, so more often the object is simply absent. Where it
    is present but generic, every test passes and AES runs at a fraction of the
    speed the file exists to provide.
    """
    rel = next((r for r in EXPECTED_OBJECTS if _module_of(r) == module), None)
    assert rel is not None, f"{module} has no object in State A's artefact list"
    path = install / rel
    assert path.is_file(), (
        f"{rel} was not installed, so the {', '.join(sorted(ELEVATED_FAMILIES[module]))} "
        f"fast path is missing from this wheel"
    )
    want = ELEVATED_FAMILIES[module]
    found = elfutil.isa_families(path)
    missing = sorted(want - found)
    assert not missing, (
        f"{rel} contains no {', '.join(missing)} instructions, so it was compiled without "
        f"{[f for f, fam in FLAG_FAMILY.items() if fam in set(missing)]}. The object loads "
        f"and computes correct answers slowly, which no behavioural test will notice."
    )


def test_the_elevated_objects_are_the_only_elevated_ones(install):
    """Said once over the whole tree: the partition has exactly two members.

    The per-object checks above would also catch this, but they report 41 lines
    where the useful statement is one: the set of objects carrying special
    instructions is `{_raw_aesni, _ghash_clmul}` and nothing else.
    """
    # ssse3 is left out of the summary: -mssse3 rides along with -mpclmul on the
    # one object that has it, and it is the AES-NI and PCLMUL instructions that
    # fault on hardware still in service.
    interesting_families = {"aesni", "clmul", "avx", "avx512"}
    carriers = {}
    for rel in EXPECTED_OBJECTS:
        path = install / rel
        if not path.is_file():
            continue
        interesting = elfutil.isa_families(path) & interesting_families
        if interesting:
            carriers[rel] = sorted(interesting)
    expected = {
        rel: sorted(ELEVATED_FAMILIES[_module_of(rel)] & interesting_families)
        for rel in EXPECTED_OBJECTS if _module_of(rel) in ELEVATED_FAMILIES
    }
    assert carriers == expected, (
        f"the objects carrying CPU-specific instructions are {carriers}; State A's are "
        f"{expected}. One wheel has to run on every x86-64 machine, with the fast paths "
        f"reached through Crypto.Util._cpuid rather than assumed."
    )
