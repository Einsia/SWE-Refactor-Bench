#!/usr/bin/env python3
"""List what a wasm module imports, grouped by import module name.

Why this is worth having in front of you while porting: a wasm module's import section
is the complete, machine-checkable list of everything it needs from outside itself.  A
WASI guest cannot open a file, read the clock, or get a random byte without importing
the function that does it -- and it cannot shell out at all, because no such import
exists.  So the import list is the honest answer to "what does this port actually
depend on", in a form that does not rely on reading the source and hoping.

A finished port of this repository imports from exactly one module,
``wasi_snapshot_preview1``.  Anything else means the module expects a host that the
runtime it is scored under will not provide, and it will fail to instantiate.

    wasm-imports.py sqlite3.wasm            # grouped summary
    wasm-imports.py sqlite3.wasm --all      # every import, one per line
"""
from __future__ import annotations

import sys
from collections import defaultdict


def uleb(data: bytes, i: int) -> tuple[int, int]:
    """Read one unsigned LEB128 at ``i``; return (value, next index)."""
    result = shift = 0
    while True:
        byte = data[i]
        i += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, i
        shift += 7


def skip_limits(data: bytes, i: int) -> int:
    flags, i = uleb(data, i)
    _min, i = uleb(data, i)
    if flags & 0x01:
        _max, i = uleb(data, i)
    return i


def imports(blob: bytes) -> list[tuple[str, str, str]]:
    """Return (module, field, kind) for every import, in module order."""
    if blob[:4] != b"\x00asm":
        raise SystemExit("not a wasm module: missing \\0asm header")
    i = 8  # magic + version
    found: list[tuple[str, str, str]] = []
    kinds = {0: "func", 1: "table", 2: "memory", 3: "global"}
    while i < len(blob):
        section_id = blob[i]
        i += 1
        size, i = uleb(blob, i)
        body, i = blob[i : i + size], i + size
        if section_id != 2:  # 2 == import section
            continue
        count, j = uleb(body, 0)
        for _ in range(count):
            mlen, j = uleb(body, j)
            module, j = body[j : j + mlen].decode("utf-8", "replace"), j + mlen
            flen, j = uleb(body, j)
            field, j = body[j : j + flen].decode("utf-8", "replace"), j + flen
            kind = body[j]
            j += 1
            if kind == 0:      # func: type index
                _t, j = uleb(body, j)
            elif kind == 1:    # table: element type, then limits
                j = skip_limits(body, j + 1)
            elif kind == 2:    # memory: limits
                j = skip_limits(body, j)
            elif kind == 3:    # global: value type, mutability
                j += 2
            else:
                raise SystemExit(f"unknown import kind {kind}")
            found.append((module, field, kinds.get(kind, str(kind))))
    return found


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__.strip().splitlines()[-2].strip(), file=sys.stderr)
        return 2
    with open(argv[1], "rb") as fh:
        found = imports(fh.read())

    if not found:
        print("no imports at all -- the module is fully self-contained")
        return 0

    by_module: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for module, field, kind in found:
        by_module[module].append((field, kind))

    if "--all" in argv:
        for module, field, kind in found:
            print(f"{module}\t{field}\t{kind}")
        return 0

    print(f"{len(found)} imports from {len(by_module)} module(s):")
    for module in sorted(by_module):
        entries = sorted(by_module[module])
        print(f"  {module}  ({len(entries)})")
        for field, kind in entries:
            print(f"      {field}  [{kind}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
