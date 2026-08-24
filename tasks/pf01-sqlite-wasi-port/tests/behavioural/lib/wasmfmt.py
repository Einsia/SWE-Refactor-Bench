"""Read a wasm module's own declarations, so absence can be proved rather than argued.

Running the port shows what it *does*.  It cannot show what it *cannot do*, and for
a platform rewrite that is the more important half: the claim is not "sqlite works
here" but "sqlite works here using only what this platform offers".  A module's
import section is the exhaustive list of host functions it is able to call.  Nothing
outside that list is reachable -- not by dlopen, not by an inline syscall, not by a
fallback path that only triggers on some input the suite did not think of -- because
the function is simply not present in the instance.  That makes the import section a
proof, checkable in milliseconds, of every capability the port does *not* use.

What that proof buys, concretely.  WASI preview1 has no ``mmap``, no ``dlopen``, no
``fork``/``exec``, and no thread spawn.  So a module importing only
``wasi_snapshot_preview1`` cannot memory-map a database, cannot load a shared
library, and -- the one that matters most for a benchmark -- cannot shell out to a
native ``sqlite3`` and pass the behavioural suite by proxy.  The suite's 2653
behavioural cases are all satisfiable by a good enough proxy; this file is what makes
the proxy impossible instead of merely unlikely.

It is deliberately a parser and not a linker.  Stock wasmtime already refuses a
module importing a function that WASI does not define, so a forged host module could
never instantiate; the value here is in *reporting* the capability set as evidence a
reviewer can read, and in catching the cases wasmtime is happy with -- an extra
import module smuggled alongside the WASI one, a memory declared shared, a reactor
module with no ``_start``.

Format reference: WebAssembly core 1.0, section 5.5 (binary format, modules).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

#: Magic and version of a wasm binary: ``\0asm`` then version 1, little endian.
MAGIC = b"\x00asm"
VERSION = b"\x01\x00\x00\x00"

#: Import module names a WASI command may legitimately use.  ``wasi_unstable`` is
#: the older spelling; some toolchains still emit it and it is equally constrained,
#: so it is allowed and reported rather than rejected.
WASI_MODULES = frozenset({"wasi_snapshot_preview1", "wasi_unstable"})

#: Every function WASI preview1 defines.  Written out rather than derived so that
#: the list a reviewer checks against is in the repository, not in a dependency.
#: Note what is absent, since that absence is the whole point: no mmap, no dlopen,
#: no process spawn, no thread creation, no chmod.
PREVIEW1 = frozenset({
    "args_get", "args_sizes_get",
    "environ_get", "environ_sizes_get",
    "clock_res_get", "clock_time_get",
    "fd_advise", "fd_allocate", "fd_close", "fd_datasync",
    "fd_fdstat_get", "fd_fdstat_set_flags", "fd_fdstat_set_rights",
    "fd_filestat_get", "fd_filestat_set_size", "fd_filestat_set_times",
    "fd_pread", "fd_prestat_get", "fd_prestat_dir_name", "fd_pwrite",
    "fd_read", "fd_readdir", "fd_renumber", "fd_seek", "fd_sync", "fd_tell",
    "fd_write",
    "path_create_directory", "path_filestat_get", "path_filestat_set_times",
    "path_link", "path_open", "path_readlink", "path_remove_directory",
    "path_rename", "path_symlink", "path_unlink_file",
    "poll_oneoff", "proc_exit", "proc_raise", "sched_yield", "random_get",
    "sock_accept", "sock_recv", "sock_send", "sock_shutdown",
})

#: The four preview1 calls that touch a socket.  A file-backed database engine has
#: no use for them, but wasi-libc's socket stubs can pull them in without the port
#: asking, so their presence is reported and not treated as a failure on its own --
#: the network is closed off by the runner's lack of ``--allow-tcp`` and by the
#: task's no-network container, not by their absence here.
NETWORK = frozenset({"sock_accept", "sock_recv", "sock_send", "sock_shutdown"})

#: What a real file-backed database engine must ask the platform for, as
#: capability -> acceptable imports (any one of them satisfies it).
#:
#: This is the positive half of the proof, and it exists because the negative half
#: has a hole: a module that imports nothing at all trivially imports nothing
#: forbidden.  An implementation keeping every database in linear memory and
#: never touching a descriptor would pass an imports-only allow-list while failing
#: to be a port at all.  The behavioural suite would catch it -- the on-disk format
#: family reads bytes back off the filesystem -- but catching it here names the
#: actual defect instead of scattering it across a hundred file cases.
#:
#: Alternatives are listed where the choice is the port's to make: ``fd_read`` and
#: ``fd_pread`` are both ways to read, and requiring the positional one would be
#: this file dictating an implementation rather than checking a capability.
REQUIRED_CAPABILITIES: dict[str, tuple[str, ...]] = {
    "open a file": ("path_open",),
    "read a file": ("fd_read", "fd_pread"),
    "write a file": ("fd_write", "fd_pwrite"),
    "flush writes to disk": ("fd_sync", "fd_datasync"),
    "close a descriptor": ("fd_close",),
    "stat a file": ("fd_filestat_get", "path_filestat_get"),
    "truncate a file": ("fd_filestat_set_size",),
    "delete a file": ("path_unlink_file",),
    "read its argument vector": ("args_get",),
    "exit with a status": ("proc_exit",),
}

#: Section ids, core 1.0 table in 5.5.2.
SECTION_NAMES = {
    0: "custom", 1: "type", 2: "import", 3: "function", 4: "table",
    5: "memory", 6: "global", 7: "export", 8: "start", 9: "element",
    10: "code", 11: "data", 12: "datacount",
}

#: Import/export description tags.
KIND_FUNC, KIND_TABLE, KIND_MEMORY, KIND_GLOBAL = 0x00, 0x01, 0x02, 0x03
KIND_NAMES = {
    KIND_FUNC: "func", KIND_TABLE: "table",
    KIND_MEMORY: "memory", KIND_GLOBAL: "global",
}

#: Limits flags, core 1.0 5.3.7 plus the threads and memory64 proposals.  Bit 0 is
#: "has maximum"; bit 1 is shared (threads); bit 2 is a 64-bit index space.
LIMIT_HAS_MAX, LIMIT_SHARED, LIMIT_MEM64 = 0x01, 0x02, 0x04


class WasmError(RuntimeError):
    """Raised when a file is not a wasm module this reader can make sense of."""


class _Reader:
    """A bounds-checked cursor over the module bytes.

    Every read is checked against the end of the buffer.  A module is an
    untrusted artifact -- it is whatever the submission built -- so a malformed
    length must produce a clear ``WasmError`` and not a ``struct.error`` from
    somewhere deep in a section walk, or a silent short read that makes a
    truncated file look like a module with fewer imports than it has.
    """

    __slots__ = ("data", "pos")

    def __init__(self, data: bytes, pos: int = 0) -> None:
        self.data = data
        self.pos = pos

    def byte(self) -> int:
        if self.pos >= len(self.data):
            raise WasmError(f"read past end of module at {self.pos}")
        out = self.data[self.pos]
        self.pos += 1
        return out

    def take(self, count: int) -> bytes:
        if count < 0 or self.pos + count > len(self.data):
            raise WasmError(
                f"section claims {count} bytes at {self.pos}, module has "
                f"{len(self.data) - self.pos} left"
            )
        out = self.data[self.pos:self.pos + count]
        self.pos += count
        return out

    def uleb(self) -> int:
        """Unsigned LEB128.  Capped at five groups, the most a u32 can need."""
        result = shift = 0
        for _ in range(5):
            byte = self.byte()
            result |= (byte & 0x7F) << shift
            if not byte & 0x80:
                return result
            shift += 7
        raise WasmError(f"LEB128 value at {self.pos} does not fit in u32")

    def name(self) -> str:
        """A wasm name: byte length then UTF-8.

        Decoded strictly.  The spec requires valid UTF-8 here, and a replacing
        decode would map two distinct import names onto the same string, which is
        exactly the kind of collision an allow-list check must not have.
        """
        raw = self.take(self.uleb())
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WasmError(f"name at {self.pos} is not valid UTF-8: {exc}") from exc


@dataclass(frozen=True)
class Import:
    module: str
    field: str
    kind: int

    @property
    def kind_name(self) -> str:
        return KIND_NAMES.get(self.kind, f"0x{self.kind:02x}")

    def __str__(self) -> str:
        return f"{self.module}.{self.field} ({self.kind_name})"


@dataclass(frozen=True)
class Export:
    name: str
    kind: int
    index: int


@dataclass(frozen=True)
class Memory:
    minimum: int
    maximum: int | None
    shared: bool
    mem64: bool


@dataclass(frozen=True)
class Module:
    path: str
    size: int
    imports: tuple[Import, ...]
    exports: tuple[Export, ...]
    memories: tuple[Memory, ...]
    imported_memories: tuple[Memory, ...]
    sections: tuple[tuple[str, int], ...]
    custom: tuple[str, ...]

    # -- capability views -------------------------------------------------
    def host_functions(self) -> tuple[str, ...]:
        """Every host function the module can call, as ``module.field``, sorted."""
        return tuple(sorted(
            f"{i.module}.{i.field}" for i in self.imports if i.kind == KIND_FUNC
        ))

    def wasi_functions(self) -> tuple[str, ...]:
        return tuple(sorted(
            i.field for i in self.imports
            if i.kind == KIND_FUNC and i.module in WASI_MODULES
        ))

    def foreign_imports(self) -> tuple[Import, ...]:
        """Imports from anywhere other than WASI.

        Non-empty means the module needs a host function the platform does not
        define, which for this task means it needs something the platform does not
        have.  That is the escape hatch a proxy implementation would be built on.
        """
        return tuple(i for i in self.imports if i.module not in WASI_MODULES)

    def unknown_wasi(self) -> tuple[Import, ...]:
        """WASI-named imports that preview1 does not define."""
        return tuple(
            i for i in self.imports
            if i.module in WASI_MODULES
            and (i.kind != KIND_FUNC or i.field not in PREVIEW1)
        )

    def network_imports(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.wasi_functions()) & NETWORK))

    def missing_capabilities(self) -> tuple[str, ...]:
        """Capabilities in ``REQUIRED_CAPABILITIES`` that no import satisfies."""
        have = set(self.wasi_functions())
        return tuple(
            name for name, options in REQUIRED_CAPABILITIES.items()
            if not have.intersection(options)
        )

    def exported(self, name: str) -> bool:
        return any(e.name == name for e in self.exports)

    @property
    def is_command(self) -> bool:
        """A command module: wasmtime runs it by calling ``_start``.

        The alternative shape is a reactor, which exports ``_initialize`` and
        expects a host to drive it.  ``wasmtime run`` of a reactor does nothing
        useful, so this distinguishes a module that is actually the CLI from one
        that is a library the submission intended to be driven some other way.
        """
        return self.exported("_start")

    @property
    def is_wasm32(self) -> bool:
        """No memory uses the 64-bit index space."""
        return not any(m.mem64 for m in self.all_memories)

    @property
    def is_threaded(self) -> bool:
        """Any memory is shared, which is how a wasm module gets threads."""
        return any(m.shared for m in self.all_memories)

    @property
    def all_memories(self) -> tuple[Memory, ...]:
        return self.memories + self.imported_memories


def _limits(reader: _Reader) -> Memory:
    flags = reader.byte()
    minimum = reader.uleb()
    maximum = reader.uleb() if flags & LIMIT_HAS_MAX else None
    return Memory(
        minimum=minimum,
        maximum=maximum,
        shared=bool(flags & LIMIT_SHARED),
        mem64=bool(flags & LIMIT_MEM64),
    )


def _skip_global_type(reader: _Reader) -> None:
    reader.byte()  # value type
    reader.byte()  # mutability


def parse(path: str) -> Module:
    """Read the declarations of the module at ``path``.

    Only the sections whose contents this file makes claims about are decoded:
    imports, exports, memory, and the names of custom sections.  The rest are
    stepped over by their declared length.  Walking into the code section to
    inspect instructions would be a different tool with a much larger surface,
    and it is not needed -- an instruction cannot reach a host function that the
    import section did not bring in.
    """
    with open(path, "rb") as fh:
        data = fh.read()
    if len(data) < 8:
        raise WasmError(f"{path}: {len(data)} bytes, too short to be a module")
    if data[:4] != MAGIC:
        raise WasmError(f"{path}: does not start with the wasm magic bytes")
    if data[4:8] != VERSION:
        raise WasmError(f"{path}: wasm version {data[4:8]!r}, expected 1")

    reader = _Reader(data, 8)
    imports: list[Import] = []
    exports: list[Export] = []
    memories: list[Memory] = []
    imported_memories: list[Memory] = []
    sections: list[tuple[str, int]] = []
    custom: list[str] = []

    while reader.pos < len(data):
        section_id = reader.byte()
        size = reader.uleb()
        body = _Reader(reader.take(size))
        sections.append((SECTION_NAMES.get(section_id, str(section_id)), size))

        if section_id == 0:
            custom.append(body.name())
        elif section_id == 2:
            for _ in range(body.uleb()):
                module_name, field = body.name(), body.name()
                kind = body.byte()
                if kind == KIND_FUNC:
                    body.uleb()
                elif kind == KIND_TABLE:
                    body.byte()
                    _limits(body)
                elif kind == KIND_MEMORY:
                    imported_memories.append(_limits(body))
                elif kind == KIND_GLOBAL:
                    _skip_global_type(body)
                else:
                    raise WasmError(
                        f"{path}: import {module_name}.{field} has unknown "
                        f"descriptor 0x{kind:02x}"
                    )
                imports.append(Import(module_name, field, kind))
        elif section_id == 5:
            for _ in range(body.uleb()):
                memories.append(_limits(body))
        elif section_id == 7:
            for _ in range(body.uleb()):
                name = body.name()
                exports.append(Export(name, body.byte(), body.uleb()))

    return Module(
        path=path,
        size=len(data),
        imports=tuple(imports),
        exports=tuple(exports),
        memories=tuple(memories),
        imported_memories=tuple(imported_memories),
        sections=tuple(sections),
        custom=tuple(custom),
    )


def describe(module: Module) -> str:
    """A human-readable capability report.

    Printed by the audit test on failure, and by hand while porting.  The
    import list is the part worth reading: it is short, and it says exactly what
    the port asks of the platform.
    """
    lines = [
        f"{os.path.basename(module.path)}: {module.size} bytes",
        f"  sections: " + ", ".join(f"{n}={s}" for n, s in module.sections),
        f"  custom:   " + (", ".join(module.custom) or "(none)"),
        f"  command:  {'yes (_start)' if module.is_command else 'NO _start'}",
        f"  wasm32:   {module.is_wasm32}",
        f"  threaded: {module.is_threaded}",
    ]
    for mem in module.all_memories:
        lines.append(
            f"  memory:   min={mem.minimum} max={mem.maximum} "
            f"shared={mem.shared} mem64={mem.mem64}"
        )
    wasi = module.wasi_functions()
    lines.append(f"  wasi imports ({len(wasi)}):")
    lines.extend(f"    {name}" for name in wasi)
    foreign = module.foreign_imports()
    lines.append(f"  non-wasi imports ({len(foreign)}):")
    lines.extend(f"    {imp}" for imp in foreign)
    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    for _path in sys.argv[1:]:
        print(describe(parse(_path)))
        print()
