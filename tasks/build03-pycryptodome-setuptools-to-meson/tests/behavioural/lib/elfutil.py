"""Read the delivered shared objects: symbols, dependencies, instructions.

Everything here inspects a file the build produced. Nothing reads the build's own
account of itself -- no compile_commands.json, no meson-info, no config.h -- so
each measurement means the same thing for State A's setuptools build and for the
submission's, and can therefore be compared between them.

The instruction scan matters more than it looks. pycryptodome's 41 ctypes
libraries are not interchangeable: `_raw_aesni` is compiled with -maes and calls
AESENC, `_ghash_clmul` with -mpclmul and calls PCLMULQDQ, and both are loaded at
runtime only after a CPUID check. A build that applies those flags to every
translation unit produces a wheel that passes every test on the machine that
built it and crashes with SIGILL on any older CPU. The way to see that is to
disassemble what was delivered and ask which objects contain the instructions --
which is what `isa_families` does.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

#: Instruction families, by the mnemonics that only appear when the
#: corresponding flag was in effect. Kept to mnemonics that gcc will not emit
#: from generic code: `aesenc` needs -maes, `pclmul*` needs -mpclmul.
ISA_FAMILIES = {
    "aesni": ("aesenc", "aesenclast", "aesdec", "aesdeclast", "aeskeygenassist", "aesimc"),
    "clmul": ("pclmulqdq", "pclmullqlqdq", "pclmulhqlqdq", "pclmullqhqdq", "pclmulhqhqdq"),
    "ssse3": ("palignr", "pshufb", "phaddw", "phaddd", "pmaddubsw"),
    "avx": ("vpxor", "vmovdqa", "vzeroupper", "vpshufb"),
    "avx512": ("vpternlog", "vpxord", "kmovw", "vpbroadcastq"),
}


def _tool(name: str, *args: str) -> str:
    try:
        p = subprocess.run([name, *args], capture_output=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AssertionError(f"{name} could not be run on {args[-1] if args else '?'}: {exc}")
    if p.returncode != 0:
        raise AssertionError(
            f"{name} {' '.join(args[:-1])} failed on {args[-1] if args else '?'}: "
            f"{p.stderr.decode('utf-8', 'replace')[:300]}"
        )
    return p.stdout.decode("utf-8", "replace")


def dynamic_symbols(path: Path) -> dict:
    """Defined and undefined dynamic symbols, as a sorted pair of lists."""
    out = _tool("readelf", "--wide", "--dyn-syms", str(path))
    defined, undefined = set(), set()
    for line in out.splitlines():
        parts = line.split()
        # `Num:` is the header row and also ends in a colon, so the index has to
        # be a number: otherwise the literal string `Name` is collected as a
        # symbol in every object and the comparison is off by one everywhere.
        if len(parts) < 8 or not parts[0][:-1].isdigit() or not parts[0].endswith(":"):
            continue
        shndx, name = parts[6], parts[7].split("@")[0]
        if not name or name.startswith("$"):
            continue
        (undefined if shndx == "UND" else defined).add(name)
    return {"defined": sorted(defined), "undefined": sorted(undefined)}


def needed(path: Path) -> list[str]:
    """DT_NEEDED entries: what the loader will pull in alongside this object."""
    out = _tool("readelf", "--wide", "-d", str(path))
    return sorted(
        m.group(1)
        for m in (re.search(r"\(NEEDED\).*\[([^\]]+)\]", line) for line in out.splitlines())
        if m
    )


def soname(path: Path) -> str | None:
    out = _tool("readelf", "--wide", "-d", str(path))
    m = re.search(r"\(SONAME\).*\[([^\]]+)\]", out)
    return m.group(1) if m else None


def run_paths(path: Path) -> list[str]:
    """DT_RPATH and DT_RUNPATH, split into individual directories.

    Both tags are read because they are not interchangeable: RUNPATH is consulted
    after LD_LIBRARY_PATH and RPATH before it, so the older one is the more
    dangerous to leave behind. An installed object here should have neither -- every
    library these need is found through the normal search path.
    """
    out = _tool("readelf", "--wide", "-d", str(path))
    paths: list[str] = []
    for line in out.splitlines():
        m = re.search(r"\((RPATH|RUNPATH)\).*\[([^\]]*)\]", line)
        if m:
            paths.extend(p for p in m.group(2).split(":") if p)
    return paths


def elf_type(path: Path) -> str:
    out = _tool("readelf", "-h", str(path))
    m = re.search(r"^\s*Type:\s+(\w+)", out, re.M)
    return m.group(1) if m else "?"


def isa_families(path: Path) -> set[str]:
    """Which instruction families the object's text actually contains.

    Disassembles rather than reading flags: the question is what will execute,
    and an object compiled with -maes that never emits AESENC is not a problem
    while one that emits it without a CPUID guard is.
    """
    out = _tool("objdump", "-d", "--no-show-raw-insn", str(path))
    mnemonics = set()
    for line in out.splitlines():
        if ":\t" not in line:
            continue
        body = line.split(":\t", 1)[1].strip()
        if body:
            mnemonics.add(body.split()[0].lower())
    found = set()
    for family, names in ISA_FAMILIES.items():
        if mnemonics & set(names):
            found.add(family)
    return found


def describe(path: Path) -> dict:
    """The whole record for one object, in the shape data/elf.json uses."""
    syms = dynamic_symbols(path)
    return {
        "defined_symbols": syms["defined"],
        "undefined_symbols": syms["undefined"],
        "n_defined": len(syms["defined"]),
        "needed": needed(path),
        "soname": soname(path),
        "elf_type": elf_type(path),
        "has_pyinit": any(s.startswith("PyInit") for s in syms["defined"]),
        "links_libpython": any("libpython" in s for s in needed(path)),
    }
