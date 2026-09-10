"""The exported symbol table of every delivered object.

These libraries are opened with ctypes and called by name, so their dynamic
symbol table is their entire interface. Nothing else about them is public: there
is no header, no import, no `PyInit_`. If `AES_start_operation` is not exported,
`Crypto.Cipher.AES` raises; if a hundred internal helpers are exported alongside
it, the interface is a hundred names wider than the project intended.

Meson's `py.extension_module()` defaults to hidden visibility, which is the
right default and the one most likely to remove a symbol the loader needs. The
opposite mistake is a build that exports everything, which passes every
behavioural test. Both are visible here and only here.

Three properties per object, compared against State A object by object:

  * the defined symbol set, exactly -- 277 names across 41 files;
  * no `PyInit_*`, because these are not Python extension modules and a build
    that made them into some would have changed how they load;
  * DT_NEEDED, because a library that pulled in libpython is no longer loadable
    from a different interpreter, and one that pulled in libgcc or libstdc++ has
    acquired a runtime dependency the wheel does not declare.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import elfutil


def _ground_truth() -> dict:
    path = Path(os.environ.get("SRB_SUITE_DIR", "/tests/behavioural")) / "data/elf.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


ELF = _ground_truth()
EXPECTED_OBJECTS = sorted(ELF)

#: Symbols every gcc-linked object carries that say nothing about the project.
#: Subtracted from both sides, so the comparison is over the project's own names.
TOOLCHAIN_SYMBOLS = frozenset({
    "_init", "_fini", "__bss_start", "_edata", "_end",
    "_ITM_deregisterTMCloneTable", "_ITM_registerTMCloneTable",
    "__gmon_start__", "__cxa_finalize", "__stack_chk_fail",
})


@pytest.fixture(scope="module")
def install(built) -> Path:
    return Path(built("default").install)


def _project_symbols(names) -> set:
    return {n for n in names if n not in TOOLCHAIN_SYMBOLS}


def test_the_symbol_ground_truth_is_present():
    total = sum(len(_project_symbols(rec["defined_symbols"])) for rec in ELF.values())
    assert len(ELF) == 41, f"data/elf.json describes {len(ELF)} objects, expected 41"
    assert total == 277, (
        f"State A's frozen tables hold {total} project symbols across 41 objects, expected 277"
    )


@pytest.mark.parametrize("rel", EXPECTED_OBJECTS)
def test_exported_symbols_match(rel, install):
    """The defined set, exactly. Missing names break the loader; extra ones widen the ABI."""
    path = install / rel
    assert path.is_file(), f"{rel} was not installed, so it exports nothing"
    want = _project_symbols(ELF[rel]["defined_symbols"])
    got = _project_symbols(elfutil.dynamic_symbols(path)["defined"])
    missing, extra = sorted(want - got), sorted(got - want)
    assert not missing, (
        f"{rel} does not export {len(missing)} of State A's {len(want)} symbols: "
        f"{', '.join(missing[:8])}. ctypes resolves these by name, so each one is a "
        f"function the installed library cannot call."
    )
    assert not extra, (
        f"{rel} exports {len(extra)} symbols State A keeps internal: "
        f"{', '.join(extra[:8])}. Default visibility instead of hidden widens the "
        f"library's interface to everything it happens to define."
    )


@pytest.mark.parametrize("rel", EXPECTED_OBJECTS)
def test_object_is_not_a_python_extension_module(rel, install):
    """No `PyInit_*`. These are ctypes libraries, and the difference is how they load.

    A build that produced real extension modules would have them imported rather
    than opened, which changes the failure mode from `OSError` to `ImportError`
    and makes the abi3 tag on the wheel a claim about the Python C API instead of
    a statement that there is none.
    """
    path = install / rel
    assert path.is_file(), f"{rel} was not installed"
    defined = elfutil.dynamic_symbols(path)["defined"]
    pyinit = sorted(n for n in defined if n.startswith("PyInit"))
    assert not pyinit, (
        f"{rel} exports {', '.join(pyinit)}; State A's objects export no PyInit_ symbol "
        f"because nothing here uses the Python C API"
    )


#: What an object here may depend on. libc because everything does; libm and
#: libgcc_s because a compiler is entitled to emit a call into either from
#: perfectly ordinary C. Anything else is a dependency the wheel does not
#: declare and that a minimal container may not have.
ALLOWED_NEEDED = frozenset({"libc.so.6", "libm.so.6", "libgcc_s.so.1", "ld-linux-x86-64.so.2"})


@pytest.mark.parametrize("rel", EXPECTED_OBJECTS)
def test_runtime_dependencies_are_ones_the_wheel_can_assume(rel, install):
    """DT_NEEDED against an allowed set, not against State A's list entry for entry.

    State A leaves three of these objects with no DT_NEEDED at all -- `_raw_ecb`,
    `_cpuid_c` and `_strxor` use nothing from libc that survives
    `--as-needed`. A build that is otherwise identical can easily produce
    `libc.so.6` there instead, and does: these are opened with ctypes inside a
    process that already has libc mapped, so neither form changes what loads or
    what runs.

    So requiring State A's exact list would fail a correct migration on one
    object out of 41 for a difference with no behavioural content. What does have
    behavioural content is a dependency on something that might not be there,
    which is what this checks.
    """
    path = install / rel
    assert path.is_file(), f"{rel} was not installed"
    got = elfutil.needed(path)
    unexpected = sorted(set(got) - ALLOWED_NEEDED)
    assert not unexpected, (
        f"{rel} depends on {', '.join(unexpected)}, which the wheel does not declare "
        f"and a minimal install may not have. State A's objects need "
        f"{sorted(ALLOWED_NEEDED & set(ELF[rel]['needed'])) or 'nothing'}."
    )


def test_no_object_links_against_libpython(install):
    """Said once, across all of them, because it is the one that voids the abi3 tag."""
    linked = sorted(
        rel for rel in EXPECTED_OBJECTS
        if (install / rel).is_file()
        and any("libpython" in n for n in elfutil.needed(install / rel))
    )
    assert not linked, (
        f"{len(linked)} objects link against libpython: {', '.join(linked[:6])}. "
        f"A wheel tagged for the stable ABI whose objects need a specific libpython "
        f"installs where it cannot run."
    )


def test_no_object_is_an_executable_or_static_archive(install):
    """ELF DYN, all 41. A PIE or a relocatable object is not something dlopen accepts."""
    wrong = {}
    for rel in EXPECTED_OBJECTS:
        path = install / rel
        if not path.is_file():
            continue
        kind = elfutil.elf_type(path)
        if kind != ELF[rel]["elf_type"]:
            wrong[rel] = f"{kind} (State A: {ELF[rel]['elf_type']})"
    assert not wrong, (
        "these objects are not the ELF type State A produces: "
        + ", ".join(f"{k} is {v}" for k, v in sorted(wrong.items())[:6])
    )
