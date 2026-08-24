"""What the delivered module can and cannot reach, read off the module itself.

Every other module in this suite runs the port and compares what came out.  That
answers "does it behave correctly" and it cannot answer "does it behave correctly
*using only this platform*" -- because a module that shelled out to a native
sqlite3, or memory-mapped a database through a host function of its own, would
produce identical output and pass all 2,653 comparisons.

A wasm module's import section is the complete list of host functions it is able to
call.  Not the list it does call on the inputs the suite chose: the list it *can*
call, ever, on any input, because a function that is not imported is not present in
the instance.  That makes absence provable in milliseconds, and it is the one
question in the whole stage that a behavioural test genuinely cannot reach.

This is not a source check.  Nothing here opens a ``.c`` file, greps for a macro or
cares what the VFS is called.  It reads the binary the build produced, which is the
same thing the other modules run.

The positive half matters as much as the negative half.  A module that imports
nothing at all imports nothing forbidden, and would sail through an allow-list
while keeping every database in linear memory and never touching the filesystem.
So ``wasmfmt.REQUIRED_CAPABILITIES`` asserts that the port asks the platform for
the things a file-backed database engine has to ask for -- open, read, write, sync,
stat, truncate, unlink -- with alternatives listed wherever the choice is the
port's (``fd_read`` or ``fd_pread``; both are ways to read).

On a pre-migration submission
-----------------------------
Five of the seven checks below ask a wasm module a question a native binary has no
answer to -- whether it exports ``_start``, whether a memory is 64-bit -- and those
are recorded as skips with the reason attached, not as failures.  That is a
distinction in the report and not in the arithmetic: the scorer keeps a skip in the
module's denominator and scores it 0, so a native binary rates 2 of 7 here.  What
the skip buys is a reader who can tell a question this artefact could not be asked
from one it answered wrongly; the line this module prints is over the checks it
could ask, which is why it reads ``2/2`` beside a rate of 0.2857.

Two of them survive the change of platform, because the *claim* was never about
wasm; it was about reading a binary's import table instead of trusting its output.
An ELF's dynamic symbol table is the same instrument as a wasm import section: the
list of host entry points the binary is able to call, ever, on any input.  So
``filesystem-capabilities`` keeps its id and its meaning and reads ``.dynsym``
instead of the import section, and ``instantiates`` keeps its id and runs the
binary instead of the module.  Two checks are added for what the native platform
makes visible and wasm does not (that the artefact is an executable for this
machine, and that everything it links against is a base-system library).

The capability list is deliberately keyed by the same ten names on both paths, so
the two readings make the same claims in the same words.  Nine of the ten map;
``read its argument vector`` does not, because native argv arrives as a parameter
to ``main`` rather than through a call, so there is no symbol to find.  It is
dropped from the native list rather than matched against something that would
always be there -- a capability check that cannot fail is a free mark.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import builds
import execute
import report
import wasmfmt

#: The POSIX reading of ``wasmfmt.REQUIRED_CAPABILITIES``: same capability names,
#: same "any one of these satisfies it" rule, libc entry points instead of WASI
#: functions.  The 64-bit variants are listed because glibc's headers redirect to
#: them under _FILE_OFFSET_BITS=64, which is what SQLite's unix VFS asks for, so
#: the symbol that actually lands in .dynsym is `open64` rather than `open`.
#:
#: `read its argument vector` is absent on purpose -- see the module docstring.
POSIX_CAPABILITIES: dict[str, tuple[str, ...]] = {
    "open a file": ("open", "open64", "openat", "openat64"),
    "read a file": ("read", "pread", "pread64"),
    "write a file": ("write", "pwrite", "pwrite64"),
    "flush writes to disk": ("fsync", "fdatasync"),
    "close a descriptor": ("close",),
    "stat a file": ("stat", "stat64", "fstat", "fstat64", "lstat", "lstat64",
                    "__fxstat", "__fxstat64"),
    "truncate a file": ("ftruncate", "ftruncate64"),
    "delete a file": ("unlink", "unlinkat", "remove"),
    "exit with a status": ("exit", "_exit", "_Exit"),
}

#: Shared objects a base-system build of 3.31.1 may need: libc and the four the
#: recipe passes (-lm -lpthread -ldl -lz).  Nothing here is a judgement about the
#: port's quality; the check exists so a binary that dlopens a vendored helper or
#: links a second sqlite is visible, which is the native form of the question
#: `wasi-only-imports` asks.
BASE_LIBRARIES = (
    "libc.so", "libm.so", "libpthread.so", "libdl.so", "librt.so",
    "libz.so", "ld-linux", "libgcc_s.so",
)


def dyn_syms(binary: str) -> tuple[set[str], str]:
    """Undefined function symbols in an ELF's dynamic symbol table.

    The complete list of libc entry points the binary can call, which is what
    makes absence provable here rather than merely unobserved.  Version suffixes
    are stripped: `fsync@GLIBC_2.2.5` and `fsync` are the same capability.
    """
    proc = subprocess.run(
        ["readelf", "-W", "--dyn-syms", binary],
        capture_output=True, timeout=120,
    )
    if proc.returncode != 0:
        raise OSError(execute.decode(proc.stderr).strip()[:400] or "readelf failed")
    names: set[str] = set()
    for line in execute.decode(proc.stdout).splitlines():
        parts = line.split()
        # Num: Value Size Type Bind Vis Ndx Name
        if len(parts) < 8 or parts[3] != "FUNC" or parts[6] != "UND":
            continue
        names.add(parts[7].split("@", 1)[0])
    return names, execute.decode(proc.stdout)


def needed_libraries(binary: str) -> list[str]:
    """The DT_NEEDED entries: which shared objects the loader will pull in."""
    proc = subprocess.run(
        ["readelf", "-W", "-d", binary], capture_output=True, timeout=120,
    )
    out: list[str] = []
    for line in execute.decode(proc.stdout).splitlines():
        if "(NEEDED)" not in line:
            continue
        _, _, tail = line.partition("Shared library: [")
        name = tail.split("]", 1)[0]
        if name:
            out.append(name)
    return out


def main_native(ledger: builds.Ledger) -> int:
    """Read the pre-migration binary with the native form of the same instrument."""
    checks: list[dict[str, object]] = []
    notes: list[str] = []
    binary = ledger.wasm

    def check(cid: str, title: str, ok: bool, detail: str, *,
              required: bool = False, weight: int = 1) -> None:
        checks.append({"id": cid, "title": title, "ok": ok, "weight": weight,
                       "required": required, "detail": detail})

    def skip(cid: str, title: str, detail: str) -> None:
        # Recorded per check with its own reason rather than dropped, so the
        # report says which questions were not asked and why.  A skip stays in the
        # denominator and scores 0 -- see `wasm_only` below; it is not credit.
        checks.append({"id": cid, "title": title, "verdict": "skip", "ok": False,
                       "weight": 1, "required": False, "detail": detail})

    header = execute.decode(subprocess.run(
        ["readelf", "-h", binary], capture_output=True, timeout=120,
    ).stdout)
    is_exec = "EXEC (" in header or "DYN (" in header
    machine = next((l.split(":", 1)[1].strip() for l in header.splitlines()
                    if l.strip().startswith("Machine:")), "?")
    check(
        "native-executable",
        "the artefact is an executable image for this machine",
        is_exec and bool(machine),
        f"ELF executable, machine {machine}" if is_exec else
        "not an ELF executable.  The pre-migration recipe produces one with gcc; "
        "a shell wrapper or an object file would run under the cases only by "
        "accident",
        required=True,
    )

    try:
        symbols, _ = dyn_syms(binary)
    except (OSError, subprocess.SubprocessError) as exc:
        symbols = set()
        notes.append(f"the dynamic symbol table could not be read: {exc}")
    missing = [name for name, options in POSIX_CAPABILITIES.items()
               if not symbols.intersection(options)]
    check(
        "filesystem-capabilities",
        "the binary calls what a file-backed engine needs from the platform",
        bool(symbols) and not missing,
        f"all {len(POSIX_CAPABILITIES)} capabilities are imported from libc "
        f"({len(symbols)} undefined function symbols)"
        if symbols and not missing else
        "no symbol satisfies: " + "; ".join(missing)
        + ".  An engine that never asks the platform to open, read or sync a file "
        "is not storing databases in files, whatever its output looks like"
        if symbols else
        "not attempted: the symbol table could not be read",
        required=True,
        weight=2,
    )

    libraries = needed_libraries(binary)
    foreign = [lib for lib in libraries
               if not any(lib.startswith(base.split(".so")[0]) for base in BASE_LIBRARIES)]
    check(
        "base-libraries-only",
        "every shared library it links is part of the base system",
        bool(libraries) and not foreign,
        "links " + ", ".join(libraries) if libraries and not foreign else
        "links outside the base system: " + ", ".join(foreign) if foreign else
        "no DT_NEEDED entries at all, which a dynamically linked build of this "
        "recipe cannot produce",
        required=True,
    )

    proc = subprocess.run(
        [binary, "-version", ":memory:"],
        capture_output=True, timeout=180, env=dict(execute.BASE_ENV),
    )
    out = execute.decode(proc.stdout).strip()
    reference_version = ledger.reference_provenance.get("version", "")
    check(
        "instantiates",
        "the binary runs and reports a version",
        proc.returncode == 0 and out.startswith(reference_version + " "),
        f"`<binary> -version` printed {out!r}" if proc.returncode == 0 else
        f"exit {proc.returncode}; stderr: {execute.decode(proc.stderr)[:800]!r}",
        required=True,
    )

    wasm_only = (
        "this asks a wasm module about its own format; the submission is a "
        "pre-migration native binary, which has no {}.  Recorded as a skip to say "
        "why the answer is missing, and charged as one: a skip stays in the "
        "denominator and scores 0, because producing the wasm module this reads "
        "was the task and the submissions that produced one are asked the same "
        "checks"
    )
    skip("command-module", "the artefact is a wasm command module (exports _start)",
         wasm_only.format("export section"))
    skip("wasm32", "the module uses the 32-bit index space",
         wasm_only.format("index space to declare"))
    skip("wasi-only-imports", "every host function comes from WASI preview1",
         wasm_only.format("WASI imports")
         + ".  `base-libraries-only` above is the native form of the same question")
    skip("known-preview1", "every WASI import is a function preview1 actually defines",
         wasm_only.format("WASI imports"))
    skip("not-threaded", "no memory is shared",
         wasm_only.format("shared memory")
         + ".  The pre-migration build is THREADSAFE=1 and links pthread, which is "
         "the platform difference the corpus already knows about")

    notes.append(
        "read as a pre-migration native binary: the ledger records kind=native, "
        "so the two checks whose subject is the import table were read off .dynsym "
        "and the five that are about the wasm container were skipped with a reason"
    )

    report.write({
        "checks": checks,
        "notes": notes,
        "metadata": {
            "module": "artifact",
            "path": "native",
            "bytes": Path(binary).stat().st_size,
            "dynamic_symbols": len(symbols),
            "needed_libraries": libraries,
        },
    })
    passed = report.passed(checks)
    scored = report.scored(checks)
    print(f"artifact: {passed}/{scored} checks passed, "
          f"{len(checks) - scored} skipped (native binary, "
          f"{len(symbols)} dynamic symbols)")
    return 0 if passed == scored else 1


def main() -> int:
    checks: list[dict[str, object]] = []
    notes: list[str] = []

    try:
        ledger = builds.Ledger.load()
    except builds.LedgerError as exc:
        report.write({"status": "error", "summary": str(exc), "checks": []})
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2

    # Before parsing anything: wasmfmt.parse on an ELF raises, and the honest
    # reading of a native submission is not "the wasm module is malformed".
    if ledger.is_native:
        return main_native(ledger)

    module = wasmfmt.parse(ledger.wasm)

    def check(cid: str, title: str, ok: bool, detail: str, *,
              required: bool = False, weight: int = 1) -> None:
        checks.append({"id": cid, "title": title, "ok": ok, "weight": weight,
                       "required": required, "detail": detail})

    # -- what shape of module is it ------------------------------------------
    check(
        "command-module",
        "the artefact is a wasm command module (exports _start)",
        module.is_command,
        "exports _start" if module.is_command else
        "no _start export.  `wasmtime run` invokes _start; a reactor module "
        "(exporting _initialize) needs a host to drive it and does nothing "
        "useful when run, so the scored invocation could never work.  Exports "
        f"seen: {', '.join(e.name for e in module.exports[:12]) or '(none)'}",
        required=True,
    )
    check(
        "wasm32",
        "the module uses the 32-bit index space",
        module.is_wasm32,
        "no memory declares the 64-bit index space" if module.is_wasm32 else
        "a memory uses memory64.  The target is wasm32-wasi; a 64-bit module is "
        "a different platform with different pointer width, and the on-disk "
        "format cases would be measuring something else",
    )

    # -- what it can reach ---------------------------------------------------
    foreign = module.foreign_imports()
    check(
        "wasi-only-imports",
        "every host function comes from WASI preview1",
        not foreign,
        "imports only from " + ", ".join(sorted({i.module for i in module.imports})
                                         or ["(nothing)"])
        if not foreign else
        "imports from outside WASI: "
        + ", ".join(sorted(f"{i.module}.{i.field}" for i in foreign)[:20])
        + ".  A host function the runtime does not define is a capability this "
        "platform does not have, and the module would not instantiate under the "
        "scored invocation",
        required=True,
    )
    unknown = module.unknown_wasi()
    check(
        "known-preview1",
        "every WASI import is a function preview1 actually defines",
        not unknown,
        "all imports are in the preview1 set" if not unknown else
        "WASI-named imports preview1 does not define: "
        + ", ".join(sorted(f"{i.module}.{i.field}" for i in unknown)[:20]),
    )
    check(
        "not-threaded",
        "no memory is shared",
        not module.is_threaded,
        "no shared memory" if not module.is_threaded else
        "a memory is declared shared, which is how a wasm module gets threads.  "
        "This build is single-threaded (THREADSAFE=0) and the runtime is invoked "
        "without a thread-capable host",
    )

    # -- what it must reach --------------------------------------------------
    missing = module.missing_capabilities()
    check(
        "filesystem-capabilities",
        "the module imports what a file-backed engine needs from the platform",
        not missing,
        f"all {len(wasmfmt.REQUIRED_CAPABILITIES)} capabilities are imported"
        if not missing else
        "no import satisfies: " + "; ".join(missing)
        + ".  An engine that never asks the platform to open, read or sync a file "
        "is not storing databases in files, whatever its output looks like",
        required=True,
        weight=2,
    )
    network = module.network_imports()
    if network:
        notes.append(
            "the module imports the preview1 socket calls ("
            + ", ".join(network)
            + ").  Reported, not scored: wasi-libc's stubs pull these in without "
            "the port asking, and the runtime is invoked with no network access"
        )

    # -- does it run at all --------------------------------------------------
    # The cheapest possible end-to-end: instantiate under the scored invocation
    # and ask for the version.  Every case module does this 2,653 times; doing it
    # once here means a module that cannot instantiate reports *that*, rather
    # than reporting 2,653 identical comparison failures.
    proc = subprocess.run(
        [ledger.wasmtime, "run", ledger.wasm, "-version", ":memory:"],
        capture_output=True, timeout=180, env=dict(execute.BASE_ENV),
    )
    out = execute.decode(proc.stdout).strip()
    reference_version = ledger.reference_provenance.get("version", "")
    check(
        "instantiates",
        "the module instantiates under wasmtime and reports a version",
        proc.returncode == 0 and out.startswith(reference_version + " "),
        f"`wasmtime run <module> -version` printed {out!r}"
        if proc.returncode == 0 else
        f"exit {proc.returncode}; stderr: "
        f"{execute.decode(proc.stderr)[:800]!r}",
        required=True,
    )

    report.write({
        "checks": checks,
        "notes": notes,
        "metadata": {
            "module": "artifact",
            "path": "wasm",
            "bytes": module.size,
            "imports": len(module.imports),
            "wasi_functions": len(module.wasi_functions()),
            "sections": [name for name, _ in module.sections],
        },
    })

    passed = sum(1 for c in checks if c["ok"])
    print(f"artifact: {passed}/{len(checks)} checks passed "
          f"({module.size} bytes, {len(module.wasi_functions())} WASI imports)")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
