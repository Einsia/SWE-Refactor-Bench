#!/usr/bin/env python3
"""A small, dependency-free ELF and ar reader.

The structural and audit checks need precise answers about the delivered
binaries: what SONAME does this shared object carry, exactly which symbols does
it export and with what visibility, which libraries does it need, and which
toolchain produced each translation unit inside it.

Those questions are answered by reading the files directly rather than by
scraping `readelf` output.  Parsing bytes keeps the answers stable across
binutils versions, keeps them structured rather than textual, and lets one code
path serve both a shared object and the members of a static archive.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

ELF_MAGIC = b"\x7fELF"

DT_NEEDED = 1
DT_STRTAB = 5
DT_STRSZ = 10
DT_SONAME = 14
DT_RPATH = 15
DT_TEXTREL = 22
DT_RUNPATH = 29
DT_FLAGS = 30

SHT_SYMTAB = 2
SHT_STRTAB = 3
SHT_DYNAMIC = 6
SHT_DYNSYM = 11

STB_LOCAL, STB_GLOBAL, STB_WEAK = 0, 1, 2
STT_NOTYPE, STT_OBJECT, STT_FUNC = 0, 1, 2
STV_DEFAULT, STV_INTERNAL, STV_HIDDEN, STV_PROTECTED = 0, 1, 2, 3

BIND_NAMES = {0: "LOCAL", 1: "GLOBAL", 2: "WEAK"}
TYPE_NAMES = {0: "NOTYPE", 1: "OBJECT", 2: "FUNC", 3: "SECTION", 4: "FILE",
              5: "COMMON", 6: "TLS", 10: "GNU_IFUNC"}
VIS_NAMES = {0: "DEFAULT", 1: "INTERNAL", 2: "HIDDEN", 3: "PROTECTED"}

ET_NAMES = {0: "NONE", 1: "REL", 2: "EXEC", 3: "DYN", 4: "CORE"}

class ElfError(Exception):
    """The file is not an ELF object this reader can interpret."""


@dataclass
class Section:
    name: str
    type: int
    flags: int
    addr: int
    offset: int
    size: int
    link: int
    info: int
    entsize: int


@dataclass
class Symbol:
    name: str
    value: int
    size: int
    bind: int
    type: int
    visibility: int
    shndx: int

    @property
    def defined(self) -> bool:
        return self.shndx != 0

    @property
    def exported(self) -> bool:
        """Globally visible to a dynamic linker resolving another object."""
        return (
            self.defined
            and self.bind in (STB_GLOBAL, STB_WEAK)
            and self.visibility in (STV_DEFAULT, STV_PROTECTED)
        )

    def describe(self) -> str:
        return (
            f"{self.name} {BIND_NAMES.get(self.bind, self.bind)}/"
            f"{TYPE_NAMES.get(self.type, self.type)}/"
            f"{VIS_NAMES.get(self.visibility, self.visibility)}"
        )


class ElfFile:
    """A parsed ELF object.  64-bit little-endian only, which is the target."""

    def __init__(self, data: bytes, origin: str = "<memory>") -> None:
        self.data = data
        self.origin = origin
        if len(data) < 64 or data[:4] != ELF_MAGIC:
            raise ElfError(f"{origin}: not an ELF file")
        if data[4] != 2:
            raise ElfError(f"{origin}: not a 64-bit ELF (EI_CLASS={data[4]})")
        if data[5] != 1:
            raise ElfError(f"{origin}: not little-endian (EI_DATA={data[5]})")
        self.elf_class = data[4]
        self.type, self.machine = struct.unpack_from("<HH", data, 16)
        (self.entry,) = struct.unpack_from("<Q", data, 24)
        (self.shoff,) = struct.unpack_from("<Q", data, 40)
        self.shentsize, self.shnum, self.shstrndx = struct.unpack_from(
            "<HHH", data, 58
        )
        self.sections: list[Section] = []
        self._parse_sections()

    # -- sections --------------------------------------------------------

    def _parse_sections(self) -> None:
        if not self.shoff or not self.shnum:
            return
        raw = []
        for index in range(self.shnum):
            base = self.shoff + index * self.shentsize
            if base + 64 > len(self.data):
                raise ElfError(f"{self.origin}: truncated section header {index}")
            fields = struct.unpack_from("<IIQQQQIIQQ", self.data, base)
            raw.append(fields)
        if self.shstrndx >= len(raw):
            raise ElfError(f"{self.origin}: bad shstrndx {self.shstrndx}")
        str_off, str_size = raw[self.shstrndx][4], raw[self.shstrndx][5]
        shstr = self.data[str_off : str_off + str_size]
        for fields in raw:
            self.sections.append(
                Section(
                    name=_cstr(shstr, fields[0]),
                    type=fields[1],
                    flags=fields[2],
                    addr=fields[3],
                    offset=fields[4],
                    size=fields[5],
                    link=fields[6],
                    info=fields[7],
                    entsize=fields[9],
                )
            )

    def section(self, name: str) -> Section | None:
        for entry in self.sections:
            if entry.name == name:
                return entry
        return None

    def section_data(self, name: str) -> bytes:
        entry = self.section(name)
        if entry is None or entry.type == 8:  # SHT_NOBITS carries no bytes
            return b""
        return self.data[entry.offset : entry.offset + entry.size]

    @property
    def section_names(self) -> list[str]:
        return [s.name for s in self.sections]

    # -- symbols ---------------------------------------------------------

    def _symbols_from(self, sh_type: int) -> list[Symbol]:
        out: list[Symbol] = []
        for entry in self.sections:
            if entry.type != sh_type:
                continue
            if entry.link >= len(self.sections):
                continue
            strtab_sec = self.sections[entry.link]
            strtab = self.data[
                strtab_sec.offset : strtab_sec.offset + strtab_sec.size
            ]
            count = entry.size // 24 if entry.size else 0
            for index in range(count):
                base = entry.offset + index * 24
                name_off, info, other, shndx, value, size = struct.unpack_from(
                    "<IBBHQQ", self.data, base
                )
                name = _cstr(strtab, name_off)
                if not name:
                    continue
                out.append(
                    Symbol(
                        name=name,
                        value=value,
                        size=size,
                        bind=info >> 4,
                        type=info & 0xF,
                        visibility=other & 0x3,
                        shndx=shndx,
                    )
                )
        return out

    @property
    def dynsym(self) -> list[Symbol]:
        return self._symbols_from(SHT_DYNSYM)

    @property
    def symtab(self) -> list[Symbol]:
        return self._symbols_from(SHT_SYMTAB)

    def exported_symbols(self) -> list[Symbol]:
        """What another object can link against, deduplicated by name."""
        seen: dict[str, Symbol] = {}
        for sym in self.dynsym:
            if sym.exported and sym.name not in seen:
                seen[sym.name] = sym
        return [seen[name] for name in sorted(seen)]

    def undefined_symbols(self) -> list[str]:
        names = {
            sym.name
            for sym in self.dynsym
            if not sym.defined and sym.bind != STB_LOCAL
        }
        return sorted(names)

    # -- dynamic segment -------------------------------------------------

    def _dynamic(self) -> list[tuple[int, int]]:
        entry = self.section(".dynamic")
        if entry is None:
            return []
        out: list[tuple[int, int]] = []
        count = entry.size // 16
        for index in range(count):
            tag, value = struct.unpack_from(
                "<qQ", self.data, entry.offset + index * 16
            )
            if tag == 0:
                break
            out.append((tag, value))
        return out

    def _dynstr(self) -> bytes:
        entry = self.section(".dynstr")
        if entry is None:
            return b""
        return self.data[entry.offset : entry.offset + entry.size]

    @property
    def soname(self) -> str | None:
        strtab = self._dynstr()
        for tag, value in self._dynamic():
            if tag == DT_SONAME:
                return _cstr(strtab, value)
        return None

    @property
    def needed(self) -> list[str]:
        strtab = self._dynstr()
        return [
            _cstr(strtab, value)
            for tag, value in self._dynamic()
            if tag == DT_NEEDED
        ]

    @property
    def runpath(self) -> list[str]:
        strtab = self._dynstr()
        out: list[str] = []
        for tag, value in self._dynamic():
            if tag in (DT_RPATH, DT_RUNPATH):
                out.extend(p for p in _cstr(strtab, value).split(":") if p)
        return out

    @property
    def is_shared_object(self) -> bool:
        return self.type == 3

    def strings_in(self, section: str, minimum: int = 4) -> list[str]:
        """Printable runs inside one section, for producer/provenance reads."""
        blob = self.section_data(section)
        out: list[str] = []
        current = bytearray()
        for byte in blob:
            if 32 <= byte < 127:
                current.append(byte)
                continue
            if len(current) >= minimum:
                out.append(current.decode("ascii"))
            current.clear()
        if len(current) >= minimum:
            out.append(current.decode("ascii"))
        return out

def _cstr(blob: bytes, offset: int) -> str:
    if offset >= len(blob):
        return ""
    end = blob.find(b"\0", offset)
    if end < 0:
        end = len(blob)
    return blob[offset:end].decode("utf-8", "replace")


AR_MAGIC = b"!<arch>\n"


def _long_name(table: bytes, offset: int) -> str:
    """Resolve a GNU extended name.

    Entries in the `//` table are terminated by "/\\n", not by NUL, so a plain
    C-string read would run into the following names.
    """
    if offset >= len(table):
        return ""
    end = table.find(b"/\n", offset)
    if end < 0:
        end = table.find(b"\n", offset)
    if end < 0:
        end = len(table)
    return table[offset:end].decode("utf-8", "replace")


@dataclass
class ArMember:
    name: str
    data: bytes


def read_archive(path: Path) -> list[ArMember]:
    """Iterate the members of a System V / GNU `ar` archive.

    Only enough of the format to walk members and resolve GNU long names, which
    is all a static library produced by rustc or cmake will use.
    """
    blob = path.read_bytes()
    if blob[: len(AR_MAGIC)] != AR_MAGIC:
        raise ElfError(f"{path}: not an ar archive")
    pos = len(AR_MAGIC)
    long_names = b""
    members: list[ArMember] = []
    while pos + 60 <= len(blob):
        header = blob[pos : pos + 60]
        if header[58:60] != b"`\n":
            break
        raw_name = header[0:16].decode("ascii", "replace").rstrip()
        size_field = header[48:58].decode("ascii", "replace").strip()
        if not size_field.isdigit():
            break
        size = int(size_field)
        body = blob[pos + 60 : pos + 60 + size]
        pos += 60 + size + (size & 1)
        if raw_name == "//":
            long_names = body
            continue
        if raw_name in ("/", "/SYM64/", "__.SYMDEF"):
            continue
        name = raw_name
        if name.startswith("/") and name[1:].isdigit():
            name = _long_name(long_names, int(name[1:]))
        members.append(ArMember(name=name.rstrip("/"), data=body))
    return members

def load(path: Path) -> ElfFile:
    return ElfFile(path.read_bytes(), origin=str(path))


def archive_objects(path: Path) -> list[tuple[str, ElfFile]]:
    """Every ELF member of an archive, paired with its member name."""
    out: list[tuple[str, ElfFile]] = []
    for member in read_archive(path):
        if member.data[:4] != ELF_MAGIC:
            continue
        out.append((member.name, ElfFile(member.data, origin=f"{path}({member.name})")))
    return out


def archive_defined_symbols(path: Path) -> set[str]:
    """Global symbols an archive can satisfy for a linker."""
    names: set[str] = set()
    for _, obj in archive_objects(path):
        for sym in obj.symtab:
            if sym.defined and sym.bind in (STB_GLOBAL, STB_WEAK):
                names.add(sym.name)
    return names


def is_elf(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(4) == ELF_MAGIC
    except OSError:
        return False


def is_archive(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(len(AR_MAGIC)) == AR_MAGIC
    except OSError:
        return False
