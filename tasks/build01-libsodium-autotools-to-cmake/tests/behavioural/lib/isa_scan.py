#!/usr/bin/env python3
"""Scan every object file in a build tree and report which x86 ISA extensions
its machine code actually uses.

Keyed by *source basename*, not object path, so the result is comparable across
build systems (automake writes `libavx2_la-foo.o`, CMake writes `foo.c.o`).

Used two ways:
  * capture time  -> record which sources legitimately contain which ISA
  * verify  time  -> prove the agent applied SIMD flags per-file, not globally
"""
import json
import os
import re
import subprocess
import sys

# Instruction / register signatures. Ordered most-specific first.
SIGNATURES = [
    ("avx512", re.compile(r"%zmm\d|\bvpternlog|\bvmovdqa64\b|\bvmovdqu64\b|%k[1-7]\b")),
    ("aesni",  re.compile(r"\baes(enc|dec)(last)?\b|\baesimc\b|\baeskeygenassist\b|\bvaes(enc|dec)")),
    ("pclmul", re.compile(r"\b(v)?pclmul[lh]?qdq\b")),
    ("rdrand", re.compile(r"\brdrand\b")),
    ("avx2",   re.compile(r"%ymm\d")),
    ("sse41",  re.compile(r"\b(v)?p(blendvb|blendw|maxsd|minsd|mulld|test|movzx[bwd]{2}|movsx[bwd]{2}|extrd|insrd)\b|\broundp[sd]\b")),
    ("ssse3",  re.compile(r"\b(v)?p(shufb|alignr|haddd|hsubd|hadds?w|sign[bwd])\b|\bpabs[bwd]\b")),
    # Any VEX-encoded instruction implies at least AVX was enabled.
    ("avx",    re.compile(r"\bv(add|sub|mul|xor|and|or|mov[au]?)p[sd]\b|\bvz?eroupper\b|\bvpxor\b|\bvmovdq[au]\b")),
    ("sse2",   re.compile(r"%xmm\d")),
]

OBJ_PREFIX = re.compile(r"^lib[a-z0-9_]+_la-")


def source_key(path: str) -> str:
    """Normalise an object filename to the basename of its source file."""
    base = os.path.basename(path)
    base = OBJ_PREFIX.sub("", base)
    for suf in (".c.o", ".S.o", ".c.obj", ".o", ".obj", ".lo"):
        if base.endswith(suf):
            base = base[: -len(suf)]
            break
    return base


def scan(obj: str):
    try:
        out = subprocess.run(
            ["objdump", "-d", "--no-show-raw-insn", obj],
            capture_output=True, text=True, timeout=180,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    found = sorted({name for name, rx in SIGNATURES if rx.search(out)})
    return found


def main():
    root, dest = sys.argv[1], sys.argv[2]
    result = {}
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            if not fn.endswith((".o", ".obj")):
                continue
            full = os.path.join(dirpath, fn)
            # skip libtool's PIC/non-PIC duplicate: .libs/foo.o mirrors foo.o
            feats = scan(full)
            if feats is None:
                continue
            key = source_key(fn)
            prev = result.setdefault(key, {"isa": [], "objects": []})
            prev["isa"] = sorted(set(prev["isa"]) | set(feats))
            rel = os.path.relpath(full, root)
            if rel not in prev["objects"]:
                prev["objects"].append(rel)
    for v in result.values():
        v["objects"].sort()
    with open(dest, "w") as fh:
        json.dump(result, fh, indent=1, sort_keys=True)
    print("scanned %d distinct sources" % len(result))


if __name__ == "__main__":
    main()
