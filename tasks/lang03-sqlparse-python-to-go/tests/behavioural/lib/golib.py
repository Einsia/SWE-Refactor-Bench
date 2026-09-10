#!/usr/bin/env python3
"""ELF reader and Go build-metadata parser.

Structural claims about a Go port are only worth as much as the evidence behind
them, and the strongest evidence is the shipped binary.  `go build` succeeding
proves the source compiles; it says nothing about whether the artifact is the
static, CGO-free, trimpath-stamped executable the contract promises.  Every
answer to that question is in the file itself.

Two independent readers, on purpose.  This module parses the ELF containers and
the `.go.buildinfo` blob by hand; structure.py separately asks `go version -m`
for the same facts.  A disagreement between them is itself a finding -- one of
the two is being fooled, and the report says which.

64-bit little-endian only.  That is the image's architecture, so anything else
is a verifier defect rather than a submission failure, and raises.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass, field
from pathlib import Path

ELF_MAGIC = b"\x7fELF"

ET_NAMES = {0: "NONE", 1: "REL", 2: "EXEC", 3: "DYN", 4: "CORE"}
EM_NAMES = {3: "i386", 62: "x86-64", 183: "aarch64"}

PT_LOAD = 1
PT_DYNAMIC = 2
PT_INTERP = 3
PT_NOTE = 4
PT_NAMES = {
    0: "NULL", 1: "LOAD", 2: "DYNAMIC", 3: "INTERP", 4: "NOTE", 6: "PHDR",
    7: "TLS", 0x6474E550: "GNU_EH_FRAME", 0x6474E551: "GNU_STACK",
    0x6474E552: "GNU_RELRO", 0x6474E553: "GNU_PROPERTY",
}

SHT_PROGBITS = 1
SHT_SYMTAB = 2
SHT_STRTAB = 3
SHT_DYNAMIC = 6
SHT_NOTE = 7
SHT_NOBITS = 8
SHT_DYNSYM = 11

DT_NULL = 0
DT_NEEDED = 1
DT_STRTAB = 5
DT_STRSZ = 10
DT_SONAME = 14
DT_RPATH = 15
DT_RUNPATH = 29
BIND_NAMES = {0: "LOCAL", 1: "GLOBAL", 2: "WEAK"}
STYPE_NAMES = {0: "NOTYPE", 1: "OBJECT", 2: "FUNC", 3: "SECTION", 4: "FILE",
               6: "TLS"}

# Go stamps two blobs into every binary it links.  Both are looked for by
# section name first and by content scan second, because a submission that
# strips section headers must not thereby "lose" its provenance quietly.
GO_BUILDINFO_SECTION = ".go.buildinfo"
GO_BUILDID_SECTION = ".note.go.buildid"
# 14 bytes: "\xff Go buildinf:" -- the sentinel the runtime and `go version`
# both search for.
BUILDINFO_MAGIC = b"\xff Go buildinf:"

# Traces a Python implementation leaves in a binary that links or embeds it.
# Deliberately narrow: these are C-level symbol and library names, none of which
# appear in Go source, help text or documentation strings.
CPYTHON_MARKERS = (
    b"libpython",
    b"Py_Initialize",
    b"Py_InitializeEx",
    b"PyRun_SimpleString",
    b"PyRun_SimpleFile",
    b"PyImport_ImportModule",
    b"PyEval_EvalCode",
    b"Py_GetVersion",
    b"_PyRuntime",
    b"PyObject_CallObject",
    b"Python/ceval.c",
    b"CPython",
)


class ElfError(Exception):
    """The file is not the 64-bit little-endian ELF this module can read."""


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

    @property
    def type_name(self) -> str:
        return {
            SHT_PROGBITS: "PROGBITS", SHT_SYMTAB: "SYMTAB", SHT_STRTAB: "STRTAB",
            SHT_DYNAMIC: "DYNAMIC", SHT_NOTE: "NOTE", SHT_NOBITS: "NOBITS",
            SHT_DYNSYM: "DYNSYM",
        }.get(self.type, f"0x{self.type:x}")


@dataclass
class Segment:
    type: int
    flags: int
    offset: int
    vaddr: int
    filesz: int
    memsz: int
    align: int

    @property
    def type_name(self) -> str:
        return PT_NAMES.get(self.type, f"0x{self.type:x}")


@dataclass
class Symbol:
    name: str
    value: int
    size: int
    info: int
    other: int
    shndx: int

    @property
    def bind(self) -> str:
        return BIND_NAMES.get(self.info >> 4, str(self.info >> 4))

    @property
    def stype(self) -> str:
        return STYPE_NAMES.get(self.info & 0xF, str(self.info & 0xF))

    @property
    def defined(self) -> bool:
        return self.shndx != 0

    def describe(self) -> str:
        return f"{self.name} [{self.bind} {self.stype} shndx={self.shndx}]"


def _cstr(blob: bytes, offset: int) -> str:
    if offset < 0 or offset >= len(blob):
        return ""
    end = blob.find(b"\x00", offset)
    if end < 0:
        end = len(blob)
    return blob[offset:end].decode("utf-8", "replace")


class ElfFile:
    """A parsed ELF executable.

    Both header tables are read.  Sections are the convenient view and carry
    names; segments are the authoritative one, because sections can be stripped
    while segments cannot -- the kernel needs them to run the program.  Every
    claim that must survive a stripped binary is answered from segments.
    """

    def __init__(self, data: bytes, origin: str = "<memory>") -> None:
        self.data = data
        self.origin = origin
        if len(data) < 64 or data[:4] != ELF_MAGIC:
            raise ElfError(f"{origin}: not an ELF file")
        if data[4] != 2:
            raise ElfError(f"{origin}: not 64-bit ELF (EI_CLASS={data[4]})")
        if data[5] != 1:
            raise ElfError(f"{origin}: not little-endian (EI_DATA={data[5]})")
        self.type, self.machine = struct.unpack_from("<HH", data, 16)
        (self.entry,) = struct.unpack_from("<Q", data, 24)
        (self.phoff,) = struct.unpack_from("<Q", data, 32)
        (self.shoff,) = struct.unpack_from("<Q", data, 40)
        self.phentsize, self.phnum = struct.unpack_from("<HH", data, 54)
        self.shentsize, self.shnum, self.shstrndx = struct.unpack_from(
            "<HHH", data, 58
        )
        self.segments: list[Segment] = []
        self.sections: list[Section] = []
        self._parse_segments()
        self._parse_sections()

    # -- headers ---------------------------------------------------------

    def _parse_segments(self) -> None:
        for index in range(self.phnum):
            base = self.phoff + index * self.phentsize
            if base + 56 > len(self.data):
                raise ElfError(f"{self.origin}: truncated program header {index}")
            p_type, p_flags, p_offset, p_vaddr, _paddr, p_filesz, p_memsz, p_align = (
                struct.unpack_from("<IIQQQQQQ", self.data, base)
            )
            self.segments.append(
                Segment(p_type, p_flags, p_offset, p_vaddr, p_filesz, p_memsz, p_align)
            )

    def _parse_sections(self) -> None:
        if not self.shoff or not self.shnum:
            return
        raw = []
        for index in range(self.shnum):
            base = self.shoff + index * self.shentsize
            if base + 64 > len(self.data):
                raise ElfError(f"{self.origin}: truncated section header {index}")
            raw.append(struct.unpack_from("<IIQQQQIIQQ", self.data, base))
        if self.shstrndx >= len(raw):
            raise ElfError(f"{self.origin}: bad shstrndx {self.shstrndx}")
        shstr = self.data[raw[self.shstrndx][4] :][: raw[self.shstrndx][5]]
        for f in raw:
            self.sections.append(
                Section(_cstr(shstr, f[0]), f[1], f[2], f[3], f[4], f[5],
                        f[6], f[7], f[9])
            )

    # -- lookups ---------------------------------------------------------

    @property
    def type_name(self) -> str:
        return ET_NAMES.get(self.type, f"0x{self.type:x}")

    @property
    def machine_name(self) -> str:
        return EM_NAMES.get(self.machine, f"0x{self.machine:x}")

    @property
    def section_names(self) -> list[str]:
        return [s.name for s in self.sections]

    def section(self, name: str) -> Section | None:
        for entry in self.sections:
            if entry.name == name:
                return entry
        return None

    def section_data(self, name: str) -> bytes:
        entry = self.section(name)
        if entry is None or entry.type == SHT_NOBITS:
            return b""
        return self.data[entry.offset : entry.offset + entry.size]

    def segment(self, p_type: int) -> Segment | None:
        for entry in self.segments:
            if entry.type == p_type:
                return entry
        return None

    def vaddr_to_offset(self, vaddr: int) -> int | None:
        """Translate a virtual address to a file offset via the PT_LOAD map."""
        for seg in self.segments:
            if seg.type != PT_LOAD or not seg.filesz:
                continue
            if seg.vaddr <= vaddr < seg.vaddr + seg.filesz:
                return seg.offset + (vaddr - seg.vaddr)
        return None

    # -- dynamic linking -------------------------------------------------

    def interpreter(self) -> str | None:
        """The ELF interpreter, or None for a static binary.

        Read from PT_INTERP rather than from the `.interp` section: the segment
        is what the kernel consults, so it is the copy that decides whether a
        dynamic loader runs.
        """
        seg = self.segment(PT_INTERP)
        if seg is not None and seg.filesz:
            blob = self.data[seg.offset : seg.offset + seg.filesz]
            return blob.split(b"\x00", 1)[0].decode("utf-8", "replace")
        entry = self.section(".interp")
        if entry is not None and entry.size:
            blob = self.section_data(".interp")
            return blob.split(b"\x00", 1)[0].decode("utf-8", "replace")
        return None

    def dynamic_entries(self) -> list[tuple[int, int]]:
        blob = b""
        seg = self.segment(PT_DYNAMIC)
        if seg is not None and seg.filesz:
            blob = self.data[seg.offset : seg.offset + seg.filesz]
        else:
            blob = self.section_data(".dynamic")
        out: list[tuple[int, int]] = []
        for pos in range(0, len(blob) - 15, 16):
            tag, value = struct.unpack_from("<qQ", blob, pos)
            if tag == DT_NULL:
                break
            out.append((tag, value))
        return out

    def _dynstr(self) -> bytes:
        entry = self.section(".dynstr")
        if entry is not None and entry.size:
            return self.section_data(".dynstr")
        addr = size = 0
        for tag, value in self.dynamic_entries():
            if tag == DT_STRTAB:
                addr = value
            elif tag == DT_STRSZ:
                size = value
        if addr and size:
            offset = self.vaddr_to_offset(addr)
            if offset is not None:
                return self.data[offset : offset + size]
        return b""

    def needed(self) -> list[str]:
        strtab = self._dynstr()
        return [
            _cstr(strtab, value)
            for tag, value in self.dynamic_entries()
            if tag == DT_NEEDED
        ]

    def runpath(self) -> list[str]:
        strtab = self._dynstr()
        out: list[str] = []
        for tag, value in self.dynamic_entries():
            if tag in (DT_RPATH, DT_RUNPATH):
                out.extend(p for p in _cstr(strtab, value).split(":") if p)
        return out

    def soname(self) -> str | None:
        strtab = self._dynstr()
        for tag, value in self.dynamic_entries():
            if tag == DT_SONAME:
                return _cstr(strtab, value)
        return None

    def is_static(self) -> tuple[bool, str]:
        """Static means: nothing is loaded at run time.

        Two conditions, both required.  No PT_INTERP, so no dynamic loader is
        invoked; and no DT_NEEDED, so nothing would be requested if one were.
        A binary with a PT_DYNAMIC segment but no NEEDED entries is still
        static -- Go's external linker emits one -- so the segment's presence
        alone is not the test.
        """
        interp = self.interpreter()
        libs = self.needed()
        if interp:
            return False, f"PT_INTERP={interp}"
        if libs:
            return False, "DT_NEEDED=" + ",".join(libs)
        return True, "no PT_INTERP, no DT_NEEDED"

    # -- symbols ---------------------------------------------------------

    def _symbols_from(self, sh_type: int) -> list[Symbol]:
        out: list[Symbol] = []
        for entry in self.sections:
            if entry.type != sh_type or not entry.entsize:
                continue
            strtab = b""
            if 0 <= entry.link < len(self.sections):
                link = self.sections[entry.link]
                strtab = self.data[link.offset : link.offset + link.size]
            blob = self.data[entry.offset : entry.offset + entry.size]
            for pos in range(0, len(blob) - entry.entsize + 1, entry.entsize):
                name_off, info, other, shndx, value, size = struct.unpack_from(
                    "<IBBHQQ", blob, pos
                )
                name = _cstr(strtab, name_off)
                if name:
                    out.append(Symbol(name, value, size, info, other, shndx))
        return out

    def symtab(self) -> list[Symbol]:
        return self._symbols_from(SHT_SYMTAB)

    def dynsym(self) -> list[Symbol]:
        return self._symbols_from(SHT_DYNSYM)

    def undefined_dynamic(self) -> list[str]:
        return sorted({s.name for s in self.dynsym() if not s.defined})

    def strings_in(self, section: str, minimum: int = 4) -> list[str]:
        blob = self.section_data(section)
        out, current = [], bytearray()
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


# --------------------------------------------------------------------------
# Go build metadata
# --------------------------------------------------------------------------


@dataclass
class GoBuildInfo:
    """What the linker recorded about how this binary was produced."""

    go_version: str = ""
    path: str = ""          # the main package's import path
    module: str = ""        # the main module's path
    module_version: str = ""
    deps: list[tuple[str, str]] = field(default_factory=list)
    settings: dict[str, str] = field(default_factory=dict)
    raw: str = ""
    source: str = ""        # how the blob was located

    def setting(self, key: str, default: str = "") -> str:
        return self.settings.get(key, default)

    def cgo_enabled(self) -> str:
        return self.settings.get("CGO_ENABLED", "")

    def trimpath(self) -> str:
        return self.settings.get("-trimpath", "")

    def summary(self) -> str:
        return (
            f"go={self.go_version or '?'} path={self.path or '?'} "
            f"mod={self.module or '?'} deps={len(self.deps)} "
            f"settings={len(self.settings)}"
        )


def _uvarint(blob: bytes, pos: int) -> tuple[int, int]:
    value = shift = 0
    while pos < len(blob):
        byte = blob[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7
        if shift > 63:
            break
    raise ElfError("buildinfo: malformed uvarint")


def _unquote_go(text: str) -> str:
    """Undo the quoting `go build` applies to a setting that needs it.

    Go quotes with strconv.Quote when a key or value contains a space, an equals
    sign or a quote.  JSON's string grammar accepts the result for every escape
    Go actually emits here; anything it rejects is returned as-is rather than
    guessed at, so a strange value shows up verbatim in the report.
    """
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        try:
            decoded = json.loads(text)
            if isinstance(decoded, str):
                return decoded
        except (ValueError, TypeError):
            return text
    return text


def parse_build_text(text: str) -> GoBuildInfo:
    """Parse the tab-separated BuildInfo record.

    Format (one directive per line, fields tab-separated):
        go    go1.25.12
        path  github.com/owner/mod/cmd/tool
        mod   github.com/owner/mod  (devel)  h1:...
        dep   other/mod             v1.2.3   h1:...
        build -trimpath=true
        build CGO_ENABLED=0
    """
    info = GoBuildInfo(raw=text)
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        directive = parts[0].strip()
        rest = parts[1:]
        if directive == "go" and rest:
            info.go_version = rest[0].strip()
        elif directive == "path" and rest:
            info.path = rest[0].strip()
        elif directive == "mod" and rest:
            info.module = rest[0].strip()
            if len(rest) > 1:
                info.module_version = rest[1].strip()
        elif directive == "dep" and rest:
            version = rest[1].strip() if len(rest) > 1 else ""
            info.deps.append((rest[0].strip(), version))
        elif directive == "build" and rest:
            body = "\t".join(rest)
            key, sep, value = body.partition("=")
            if sep:
                info.settings[_unquote_go(key.strip())] = _unquote_go(value.strip())
            else:
                info.settings[_unquote_go(body.strip())] = ""
    return info


# Header layout after the 14-byte magic:
#   [14] pointer size in bytes
#   [15] flags: bit 0x1 endianness, bit 0x2 set means the two strings follow
#        inline as uvarint-length-prefixed bytes (Go >= 1.18)
_FLAG_ENDIAN_BIG = 0x1
_FLAG_VERSION_INLINE = 0x2


def _strip_module_framing(mod: str) -> str:
    """Remove the 16-byte sentinels the linker wraps the module record in.

    Same test the standard library uses: a real record is at least 33 bytes and
    has a newline 17 from the end.  Anything else is not a module record, and
    reporting it as an empty one is better than slicing garbage.
    """
    if len(mod) >= 33 and mod[len(mod) - 17] == "\n":
        return mod[16 : len(mod) - 16]
    return ""


def decode_buildinfo(blob: bytes, elf: ElfFile | None = None) -> GoBuildInfo:
    """Decode a `.go.buildinfo` blob starting at its magic."""
    if not blob.startswith(BUILDINFO_MAGIC):
        raise ElfError("buildinfo: magic not at start of blob")
    if len(blob) < 32:
        raise ElfError("buildinfo: blob shorter than its header")
    ptr_size = blob[14]
    flags = blob[15]
    if flags & _FLAG_ENDIAN_BIG:
        raise ElfError("buildinfo: big-endian binary, unsupported on this image")

    if flags & _FLAG_VERSION_INLINE:
        # Bytes 16..31 are the pointer slots, left as padding in this form; the
        # two length-prefixed strings begin after the 32-byte header.
        pos = 32
        length, pos = _uvarint(blob, pos)
        version = blob[pos : pos + length].decode("utf-8", "replace")
        pos += length
        length, pos = _uvarint(blob, pos)
        mod_raw = blob[pos : pos + length].decode("utf-8", "replace")
        form = "inline"
    else:
        # Pre-1.18 form: two pointers to Go string headers in the data section.
        if elf is None:
            raise ElfError("buildinfo: pointer form needs the ELF for address mapping")
        if ptr_size not in (4, 8):
            raise ElfError(f"buildinfo: implausible pointer size {ptr_size}")
        fmt = "<I" if ptr_size == 4 else "<Q"

        def read_ptr(at: int) -> int:
            return struct.unpack_from(fmt, blob, at)[0]

        def read_go_string(addr: int) -> str:
            head = elf.vaddr_to_offset(addr)
            if head is None:
                raise ElfError(f"buildinfo: unmapped string header at 0x{addr:x}")
            data_addr = struct.unpack_from(fmt, elf.data, head)[0]
            data_len = struct.unpack_from(fmt, elf.data, head + ptr_size)[0]
            if data_len > 1 << 24:
                raise ElfError(f"buildinfo: implausible string length {data_len}")
            body = elf.vaddr_to_offset(data_addr)
            if body is None:
                raise ElfError(f"buildinfo: unmapped string at 0x{data_addr:x}")
            return elf.data[body : body + data_len].decode("utf-8", "replace")

        version = read_go_string(read_ptr(16))
        mod_raw = read_go_string(read_ptr(16 + ptr_size))
        form = "pointer"

    text = _strip_module_framing(mod_raw)
    info = parse_build_text(text)
    if not info.go_version:
        info.go_version = version
    info.source = f"{form} ptr_size={ptr_size}"
    return info


def find_buildinfo(elf: ElfFile) -> GoBuildInfo | None:
    """Locate and decode the build info, or return None if there is none.

    The named section is tried first.  If it is missing -- a stripped or
    hand-edited binary -- the whole file is scanned for the magic, so that
    "provenance absent" and "section header absent" stay distinguishable.
    """
    entry = elf.section(GO_BUILDINFO_SECTION)
    if entry is not None and entry.size:
        # The section is 16-byte aligned padding around a variable-length blob,
        # so decoding continues past its nominal end.
        blob = elf.data[entry.offset :]
        if blob.startswith(BUILDINFO_MAGIC):
            info = decode_buildinfo(blob, elf)
            info.source = f"{GO_BUILDINFO_SECTION} {info.source}"
            return info
    at = elf.data.find(BUILDINFO_MAGIC)
    if at < 0:
        return None
    info = decode_buildinfo(elf.data[at:], elf)
    info.source = f"content-scan@0x{at:x} {info.source}"
    return info


def read_buildid(elf: ElfFile) -> str:
    """The Go build ID from `.note.go.buildid`.

    Parsed as an ELF note rather than scraped as a string: the note's name must
    be "Go" and its type 4, which a plausible-looking string in the binary would
    not satisfy.
    """
    blob = elf.section_data(GO_BUILDID_SECTION)
    if not blob:
        seg = elf.segment(PT_NOTE)
        if seg is None or not seg.filesz:
            return ""
        blob = elf.data[seg.offset : seg.offset + seg.filesz]
    pos = 0
    while pos + 12 <= len(blob):
        namesz, descsz, ntype = struct.unpack_from("<III", blob, pos)
        pos += 12
        name = blob[pos : pos + namesz].split(b"\x00", 1)[0]
        pos += (namesz + 3) & ~3
        desc = blob[pos : pos + descsz]
        pos += (descsz + 3) & ~3
        if name == b"Go" and ntype == 4:
            return desc.decode("utf-8", "replace").strip()
    return ""


def cpython_markers(data: bytes) -> list[str]:
    """Which CPython-embedding traces appear in these bytes."""
    return [
        marker.decode("ascii")
        for marker in CPYTHON_MARKERS
        if marker in data
    ]


def count_logic_lines(paths) -> tuple[int, int, int, dict[str, int]]:
    """Non-blank, non-comment lines across Go sources, with the breakdown.

    Returns (logic, comments, blanks, per-directory logic).  Two callers want this
    and they want it to agree: the structural case that enforces the contracted
    floor, and the snapshot line in the report that a reader compares against that
    floor.  Two counters would eventually disagree by a few lines and the report
    would contradict the verdict printed above it.

    Block comments are tracked across lines, so a commented-out implementation is
    not counted as logic.  A line that is code followed by a trailing comment counts
    once, as code, which is what it is.
    """
    logic = comments = blanks = 0
    per_dir: dict[str, int] = {}
    for path in paths:
        in_block = False
        count = 0
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for raw in text.splitlines():
            line = raw.strip()
            if in_block:
                comments += 1
                if "*/" in line:
                    in_block = False
                continue
            if not line:
                blanks += 1
                continue
            if line.startswith("//"):
                comments += 1
                continue
            if line.startswith("/*"):
                comments += 1
                in_block = "*/" not in line
                continue
            count += 1
        logic += count
        key = Path(path).parent.as_posix()
        per_dir[key] = per_dir.get(key, 0) + count
    return logic, comments, blanks, per_dir


def load(path: Path) -> ElfFile:
    path = Path(path)
    return ElfFile(path.read_bytes(), origin=str(path))


def is_elf(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(4) == ELF_MAGIC
    except OSError:
        return False


def describe_binary(path: Path) -> dict:
    """One flat record of every binary-level fact, for evidence and logs.

    Never raises: a description is wanted even when the file is not the ELF that
    was expected, and "error" in the result is itself the finding.
    """
    record: dict = {"path": str(path)}
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        record["error"] = f"unreadable: {exc}"
        return record
    record["size"] = len(raw)
    try:
        elf = ElfFile(raw, origin=str(path))
    except ElfError as exc:
        record["error"] = str(exc)
        return record
    static, why = elf.is_static()
    record.update(
        {
            "type": elf.type_name,
            "machine": elf.machine_name,
            "static": static,
            "static_reason": why,
            "interpreter": elf.interpreter(),
            "needed": elf.needed(),
            "runpath": elf.runpath(),
            "segments": sorted({s.type_name for s in elf.segments}),
            "has_buildinfo_section": elf.section(GO_BUILDINFO_SECTION) is not None,
            "has_buildid_section": elf.section(GO_BUILDID_SECTION) is not None,
            "buildid": read_buildid(elf),
            "cpython_markers": cpython_markers(raw),
        }
    )
    try:
        info = find_buildinfo(elf)
    except ElfError as exc:
        record["buildinfo_error"] = str(exc)
        return record
    if info is None:
        record["buildinfo"] = None
        return record
    record["buildinfo"] = {
        "go_version": info.go_version,
        "path": info.path,
        "module": info.module,
        "module_version": info.module_version,
        "deps": info.deps,
        "settings": info.settings,
        "source": info.source,
    }
    return record
