#!/usr/bin/env python3
"""A small, dependency-free class file, jar and module descriptor reader.

The structural and audit checks need precise answers about the delivered
artifact: which classes does this jar contain, what bytecode version were they
compiled for, which module does the descriptor declare and what does it export,
which types does each class actually reference, and does any of it reach for an
implementation it was supposed to replace.

Those questions are answered by reading the bytes directly rather than by
scraping `javap` and `jar --describe-module`.  Parsing keeps the answers stable
across JDK versions, keeps them structured rather than textual, and -- the
reason that matters most here -- makes the constant pool available as data.  The
central anti-cheat gate of this task is that the artifact must not delegate to
`java.util.zip`, and a delegation is a constant pool entry.  Reading the pool is
the only way to ask that question of the shipped bytes rather than of the
submitter's description of them.

One deliberate asymmetry with the ELF reader this replaces: zip decoding is left
to the standard library's `zipfile`.  Hand-rolling it would add a few hundred
lines whose only purpose is to re-derive a format `zipfile` already reads
correctly, and unlike `javap`, `zipfile` is part of the pinned interpreter rather
than a tool whose output format can move.  What is *not* delegated is anything
that decides a grade: the entry list, the class parsing and the module
descriptor are all read here.
"""

from __future__ import annotations

import struct
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

CLASS_MAGIC = 0xCAFEBABE

# Constant pool tags, JVMS 4.4.
CONSTANT_Utf8 = 1
CONSTANT_Integer = 3
CONSTANT_Float = 4
CONSTANT_Long = 5
CONSTANT_Double = 6
CONSTANT_Class = 7
CONSTANT_String = 8
CONSTANT_Fieldref = 9
CONSTANT_Methodref = 10
CONSTANT_InterfaceMethodref = 11
CONSTANT_NameAndType = 12
CONSTANT_MethodHandle = 15
CONSTANT_MethodType = 16
CONSTANT_Dynamic = 17
CONSTANT_InvokeDynamic = 18
CONSTANT_Module = 19
CONSTANT_Package = 20

# How many bytes follow the tag, for the fixed-size entries.  Utf8 is absent
# because it carries its own length; the two eight-byte constants are absent
# because they also consume a second pool slot, which the loop handles.
FIXED_WIDTH = {
    CONSTANT_Integer: 4,
    CONSTANT_Float: 4,
    CONSTANT_Class: 2,
    CONSTANT_String: 2,
    CONSTANT_Fieldref: 4,
    CONSTANT_Methodref: 4,
    CONSTANT_InterfaceMethodref: 4,
    CONSTANT_NameAndType: 4,
    CONSTANT_MethodHandle: 3,
    CONSTANT_MethodType: 2,
    CONSTANT_Dynamic: 4,
    CONSTANT_InvokeDynamic: 4,
    CONSTANT_Module: 2,
    CONSTANT_Package: 2,
}

# Access flags, JVMS 4.1 / 4.5 / 4.6.  Only the ones a check reads.
ACC_PUBLIC = 0x0001
ACC_PRIVATE = 0x0002
ACC_PROTECTED = 0x0004
ACC_STATIC = 0x0008
ACC_FINAL = 0x0010
ACC_SYNCHRONIZED = 0x0020
ACC_VOLATILE = 0x0040
ACC_NATIVE = 0x0100
ACC_INTERFACE = 0x0200
ACC_ABSTRACT = 0x0400
ACC_SYNTHETIC = 0x1000
ACC_ANNOTATION = 0x2000
ACC_ENUM = 0x4000
ACC_MODULE = 0x8000

# Module flags, JVMS 4.7.25.
ACC_OPEN = 0x0020
ACC_TRANSITIVE = 0x0020
ACC_STATIC_PHASE = 0x0040

MODULE_INFO_ENTRY = "module-info.class"

class ClassFileError(Exception):
    """A file that should have been a class file could not be read as one."""


class Reader:
    """A bounds-checked big-endian cursor.

    Every read goes through here so a truncated or hostile class file produces a
    ClassFileError naming the offset rather than an IndexError from somewhere
    deep in the parse.  That distinction matters: a submission is allowed to ship
    a malformed class, and the report should say which file and where.
    """

    def __init__(self, blob: bytes, name: str = "<class>") -> None:
        self.blob = blob
        self.pos = 0
        self.name = name

    def need(self, count: int) -> None:
        if self.pos + count > len(self.blob):
            raise ClassFileError(
                f"{self.name}: truncated at offset {self.pos}, "
                f"wanted {count} more byte(s) of {len(self.blob)}"
            )

    def u1(self) -> int:
        self.need(1)
        value = self.blob[self.pos]
        self.pos += 1
        return value

    def u2(self) -> int:
        self.need(2)
        value = struct.unpack_from(">H", self.blob, self.pos)[0]
        self.pos += 2
        return value

    def u4(self) -> int:
        self.need(4)
        value = struct.unpack_from(">I", self.blob, self.pos)[0]
        self.pos += 4
        return value

    def take(self, count: int) -> bytes:
        self.need(count)
        chunk = self.blob[self.pos:self.pos + count]
        self.pos += count
        return chunk

    def skip(self, count: int) -> None:
        self.need(count)
        self.pos += count

@dataclass
class Constant:
    tag: int
    # Utf8 text, or the operand indices for everything else.
    text: str = ""
    indices: tuple[int, ...] = ()


class ConstantPool:
    """The constant pool, indexed as the class file indexes it (from 1).

    This is the most load-bearing object in the module.  A class's declared
    dependencies are not in a manifest anywhere; they are here, as Class entries,
    as the descriptors of NameAndType entries, and as Utf8 text.  A check that
    wants to know whether the artifact reaches for java.util.zip asks this.
    """

    def __init__(self, entries: dict[int, Constant]) -> None:
        self.entries = entries

    def __len__(self) -> int:
        return len(self.entries)

    def utf8(self, index: int) -> str:
        entry = self.entries.get(index)
        if entry is None or entry.tag != CONSTANT_Utf8:
            raise ClassFileError(f"constant {index} is not a Utf8")
        return entry.text

    def utf8_or_empty(self, index: int) -> str:
        entry = self.entries.get(index)
        if entry is None or entry.tag != CONSTANT_Utf8:
            return ""
        return entry.text

    def class_name(self, index: int) -> str:
        entry = self.entries.get(index)
        if entry is None or entry.tag != CONSTANT_Class:
            raise ClassFileError(f"constant {index} is not a Class")
        return self.utf8(entry.indices[0])

    def literal(self, index: int) -> int | str | None:
        """The value of an Integer or String constant, for ConstantValue.

        Only the two kinds a `public static final` in this contract can hold.
        Long, Float and Double return None rather than a wrong number: they occupy
        the pool differently and this parser does not keep their bytes, so a
        confident answer here would be a fabricated one.  The caller reports the
        absence, which is the honest outcome and is also a finding -- the contract
        declares 39 ints and one String, so a constant that turns out to be a
        double is a contract violation either way.
        """
        entry = self.entries.get(index)
        if entry is None:
            return None
        if entry.tag == CONSTANT_Integer:
            # The fixed-width branch of the parser stored the four bytes as two
            # big-endian u2 halves, which is exactly what they are.
            if len(entry.indices) < 2:
                return None
            raw = (entry.indices[0] << 16) | entry.indices[1]
            return raw - (1 << 32) if raw >= (1 << 31) else raw
        if entry.tag == CONSTANT_String and entry.indices:
            return self.utf8_or_empty(entry.indices[0])
        return None

    def all_utf8(self) -> list[str]:
        return [e.text for e in self.entries.values() if e.tag == CONSTANT_Utf8]

    def class_names(self) -> set[str]:
        """Every type named by a Class entry, in internal form."""
        return {
            self.utf8_or_empty(e.indices[0])
            for e in self.entries.values()
            if e.tag == CONSTANT_Class and e.indices
        }

    def string_literals(self) -> set[str]:
        """Every string literal, which is where a laundered class name hides.

        A port that wants to call java.util.zip.Deflater without naming it in a
        Class entry can reach it by reflection on a string.  That is a narrower
        hiding place than it sounds -- the string has to be here -- so the
        forbidden-name check reads both this and class_names().
        """
        return {
            self.utf8_or_empty(e.indices[0])
            for e in self.entries.values()
            if e.tag == CONSTANT_String and e.indices
        }

    def descriptors(self) -> set[str]:
        """Every NameAndType descriptor: the types in signatures of references."""
        return {
            self.utf8_or_empty(e.indices[1])
            for e in self.entries.values()
            if e.tag == CONSTANT_NameAndType and len(e.indices) > 1
        }

    def member_refs(self) -> list[tuple[str, str, str]]:
        """Every (owner, name, descriptor) a Fieldref/Methodref names.

        This is what distinguishes "mentions a type" from "calls a method on it".
        `no-library-load` wants the second: a class may legitimately mention
        java.lang.System and must not call System.load.
        """
        refs: list[tuple[str, str, str]] = []
        kinds = (CONSTANT_Fieldref, CONSTANT_Methodref, CONSTANT_InterfaceMethodref)
        for entry in self.entries.values():
            if entry.tag not in kinds or len(entry.indices) < 2:
                continue
            owner_entry = self.entries.get(entry.indices[0])
            nat = self.entries.get(entry.indices[1])
            if owner_entry is None or owner_entry.tag != CONSTANT_Class:
                continue
            if nat is None or nat.tag != CONSTANT_NameAndType:
                continue
            refs.append((
                self.utf8_or_empty(owner_entry.indices[0]),
                self.utf8_or_empty(nat.indices[0]),
                self.utf8_or_empty(nat.indices[1]),
            ))
        return refs

def parse_constant_pool(reader: Reader) -> ConstantPool:
    count = reader.u2()
    entries: dict[int, Constant] = {}
    index = 1
    while index < count:
        tag = reader.u1()
        if tag == CONSTANT_Utf8:
            length = reader.u2()
            raw = reader.take(length)
            # Modified UTF-8 differs from UTF-8 for the NUL byte and for
            # supplementary characters.  Neither appears in a type name, and a
            # strict decode here would reject a class that is legal, so decode
            # leniently and let the checks compare the parts they care about.
            entries[index] = Constant(tag, text=raw.decode("utf-8", "replace"))
        elif tag in (CONSTANT_Long, CONSTANT_Double):
            reader.skip(8)
            entries[index] = Constant(tag)
            # JVMS 4.4.5: these take two slots, and the second is unusable.
            index += 1
        elif tag in FIXED_WIDTH:
            width = FIXED_WIDTH[tag]
            if width == 2:
                entries[index] = Constant(tag, indices=(reader.u2(),))
            elif width == 3:
                entries[index] = Constant(tag, indices=(reader.u1(), reader.u2()))
            else:
                entries[index] = Constant(tag, indices=(reader.u2(), reader.u2()))
        else:
            raise ClassFileError(
                f"{reader.name}: unknown constant pool tag {tag} at entry {index}"
            )
        index += 1
    return ConstantPool(entries)


@dataclass
class Attribute:
    name: str
    data: bytes


def parse_attributes(reader: Reader, pool: ConstantPool) -> list[Attribute]:
    out: list[Attribute] = []
    for _ in range(reader.u2()):
        name = pool.utf8_or_empty(reader.u2())
        length = reader.u4()
        out.append(Attribute(name, reader.take(length)))
    return out

@dataclass
class Member:
    """One declared field or method."""

    name: str
    descriptor: str
    access: int
    attributes: list[Attribute] = field(default_factory=list)

    @property
    def is_public(self) -> bool:
        return bool(self.access & ACC_PUBLIC)

    @property
    def is_static(self) -> bool:
        return bool(self.access & ACC_STATIC)

    @property
    def is_final(self) -> bool:
        return bool(self.access & ACC_FINAL)

    @property
    def is_native(self) -> bool:
        return bool(self.access & ACC_NATIVE)

    @property
    def is_synthetic(self) -> bool:
        # javac marks bridge methods, lambda bodies and switch tables synthetic.
        # A surface check must exclude them: they are public because the compiler
        # needed them to be, not because the author declared them.
        return bool(self.access & ACC_SYNTHETIC) or any(
            a.name == "Synthetic" for a in self.attributes
        )

    def constant_value(self, pool: ConstantPool) -> int | str | None:
        """The ConstantValue attribute, which is the ABI half of a constant.

        This is the number a consumer's class file gets, not the number the
        library's static initializer assigns.  javac inlines a `static final int`
        at every use site from this attribute, so a field whose ConstantValue and
        whose `<clinit>` disagree is compiled one way into the library and the
        other way into everything built against it -- a genuine ABI split with no
        C analogue, and invisible to reflection, which only ever sees the field.
        """
        for attribute in self.attributes:
            if attribute.name != "ConstantValue" or len(attribute.data) < 2:
                continue
            index = struct.unpack_from(">H", attribute.data, 0)[0]
            return pool.literal(index)
        return None

    def thrown(self, pool: ConstantPool) -> list[str]:
        """The Exceptions attribute, which is where `throws` clauses live."""
        for attribute in self.attributes:
            if attribute.name != "Exceptions":
                continue
            sub = Reader(attribute.data, "Exceptions")
            return [pool.class_name(sub.u2()) for _ in range(sub.u2())]
        return []


def parse_members(reader: Reader, pool: ConstantPool) -> list[Member]:
    out: list[Member] = []
    for _ in range(reader.u2()):
        access = reader.u2()
        name = pool.utf8_or_empty(reader.u2())
        descriptor = pool.utf8_or_empty(reader.u2())
        out.append(Member(name, descriptor, access, parse_attributes(reader, pool)))
    return out

TYPE_IN_DESCRIPTOR_START = "L"


def types_in_descriptor(descriptor: str) -> set[str]:
    """Every object type named in a field or method descriptor.

    `(Ljava/lang/String;[I)Ljava/util/zip/Deflater;` names two.  Array and
    primitive markers are skipped; what is wanted is the set of classes, because
    that is what the forbidden-reference checks compare against.
    """
    found: set[str] = set()
    position = 0
    while True:
        start = descriptor.find(TYPE_IN_DESCRIPTOR_START, position)
        if start < 0:
            return found
        end = descriptor.find(";", start)
        if end < 0:
            return found
        found.add(descriptor[start + 1:end])
        position = end + 1


class ClassFile:
    """One parsed class file."""

    def __init__(self, blob: bytes, name: str = "<class>") -> None:
        self.origin = name
        reader = Reader(blob, name)
        if reader.u4() != CLASS_MAGIC:
            raise ClassFileError(f"{name}: not a class file (bad magic)")
        self.minor_version = reader.u2()
        self.major_version = reader.u2()
        self.pool = parse_constant_pool(reader)
        self.access = reader.u2()
        this_index = reader.u2()
        self.this_class = (
            self.pool.class_name(this_index) if this_index else ""
        )
        super_index = reader.u2()
        # java.lang.Object and module-info both carry super_class 0.
        self.super_class = self.pool.class_name(super_index) if super_index else ""
        self.interfaces = [self.pool.class_name(reader.u2()) for _ in range(reader.u2())]
        self.fields = parse_members(reader, self.pool)
        self.methods = parse_members(reader, self.pool)
        self.attributes = parse_attributes(reader, self.pool)

    # -- shape ------------------------------------------------------------

    @property
    def is_interface(self) -> bool:
        return bool(self.access & ACC_INTERFACE)

    @property
    def is_public(self) -> bool:
        return bool(self.access & ACC_PUBLIC)

    @property
    def is_final(self) -> bool:
        return bool(self.access & ACC_FINAL)

    @property
    def is_abstract(self) -> bool:
        return bool(self.access & ACC_ABSTRACT)

    @property
    def is_module(self) -> bool:
        return bool(self.access & ACC_MODULE)

    @property
    def binary_name(self) -> str:
        """The dotted name, which is what a contract and a human both use."""
        return self.this_class.replace("/", ".")

    @property
    def package(self) -> str:
        if "/" not in self.this_class:
            return ""
        return self.this_class.rsplit("/", 1)[0].replace("/", ".")

    def attribute(self, name: str) -> Attribute | None:
        for attribute in self.attributes:
            if attribute.name == name:
                return attribute
        return None

    # -- provenance -------------------------------------------------------

    @property
    def source_file(self) -> str:
        """The SourceFile attribute: what the compiler says it compiled.

        This is the class-file counterpart of the ELF reader's translation-unit
        names, and it answers the same question.  `Deflater.java` is what a Java
        port looks like; `deflate.c` is what a class file transliterated by a
        C-to-bytecode tool looks like, and it is evidence the migration did not
        happen the way it claims.
        """
        attribute = self.attribute("SourceFile")
        if attribute is None or len(attribute.data) < 2:
            return ""
        index = struct.unpack_from(">H", attribute.data, 0)[0]
        return self.pool.utf8_or_empty(index)

    @property
    def has_native_method(self) -> bool:
        return any(method.is_native for method in self.methods)

    def native_methods(self) -> list[str]:
        return [m.name for m in self.methods if m.is_native]

    # -- dependencies -----------------------------------------------------

    def referenced_types(self) -> set[str]:
        """Every type this class names, in internal form, from every angle.

        Deliberately generous.  A type can enter a class file through a Class
        entry, through the descriptor of a member reference, through the
        descriptor of a declared member, or through a generic signature.  A check
        that reads only the first is one refactor away from missing the thing it
        exists to catch, so all four are unioned here and the cost is a slightly
        larger set.
        """
        found = set(self.pool.class_names())
        for descriptor in self.pool.descriptors():
            found |= types_in_descriptor(descriptor)
        for member in list(self.fields) + list(self.methods):
            found |= types_in_descriptor(member.descriptor)
            for attribute in member.attributes:
                if attribute.name == "Signature" and len(attribute.data) >= 2:
                    index = struct.unpack_from(">H", attribute.data, 0)[0]
                    found |= types_in_descriptor(self.pool.utf8_or_empty(index))
        # Array types appear as descriptors in Class entries: `[Ljava/x/Y;`.
        expanded: set[str] = set()
        for name in found:
            expanded.add(name)
            if name.startswith("["):
                expanded |= types_in_descriptor(name)
        return {n for n in expanded if n and not n.startswith("[")}

    def referenced_packages(self) -> set[str]:
        out: set[str] = set()
        for name in self.referenced_types():
            if "/" in name:
                out.add(name.rsplit("/", 1)[0].replace("/", "."))
        return out

    def calls(self) -> list[tuple[str, str, str]]:
        """Member references, as dotted (owner, name, descriptor)."""
        return [
            (owner.replace("/", "."), name, descriptor)
            for owner, name, descriptor in self.pool.member_refs()
        ]

    def string_literals(self) -> set[str]:
        return self.pool.string_literals()

    def all_utf8(self) -> list[str]:
        return self.pool.all_utf8()

@dataclass
class Requires:
    name: str
    flags: int
    version: str = ""

    @property
    def is_transitive(self) -> bool:
        return bool(self.flags & ACC_TRANSITIVE)

    @property
    def is_static(self) -> bool:
        return bool(self.flags & ACC_STATIC_PHASE)


@dataclass
class Exports:
    package: str
    flags: int
    to: tuple[str, ...] = ()

    @property
    def is_qualified(self) -> bool:
        """An export `to` a named set of modules, which is not a public export.

        Worth naming rather than inferring at each call site: a descriptor that
        exports org.zlib only to a friend module has not published its API, and a
        check that counted it as an export would pass a jar no consumer can use.
        """
        return bool(self.to)


@dataclass
class ModuleDescriptor:
    """A parsed module-info.class.

    The direct successor to zlib.map.  Both are a machine-readable declaration of
    exactly what the outside world may reach, and in both cases the declaration is
    the release contract rather than a build detail: a package left unexported is
    invisible on the module path and fatal to any consumer that resolves the jar
    as a module, while behaving perfectly on the class path.
    """

    name: str
    flags: int
    version: str
    requires: list[Requires]
    exports: list[Exports]
    opens: list[Exports]
    uses: list[str]
    provides: list[tuple[str, tuple[str, ...]]]

    @property
    def is_open(self) -> bool:
        return bool(self.flags & ACC_OPEN)

    def exported_packages(self, include_qualified: bool = False) -> set[str]:
        return {
            e.package for e in self.exports
            if include_qualified or not e.is_qualified
        }

    def required_modules(self) -> set[str]:
        return {r.name for r in self.requires}

def _module_name(pool: ConstantPool, index: int) -> str:
    """Resolve a CONSTANT_Module or CONSTANT_Package index to a dotted name.

    The Module attribute refers to modules and packages through their own tag
    kinds rather than through CONSTANT_Class, and both wrap a Utf8 that is stored
    in internal form with slashes.  Callers want the dotted name.
    """
    entry = pool.entries.get(index)
    if entry is None or not entry.indices:
        return ""
    if entry.tag not in (CONSTANT_Module, CONSTANT_Package):
        return ""
    return pool.utf8_or_empty(entry.indices[0]).replace("/", ".")


def parse_module_attribute(cls: ClassFile) -> ModuleDescriptor:
    """Read the Module attribute of a module-info class, JVMS 4.7.25."""
    attribute = cls.attribute("Module")
    if attribute is None:
        raise ClassFileError(f"{cls.origin}: no Module attribute")
    pool = cls.pool
    reader = Reader(attribute.data, f"{cls.origin}:Module")

    name = _module_name(pool, reader.u2())
    flags = reader.u2()
    version_index = reader.u2()
    version = pool.utf8_or_empty(version_index) if version_index else ""

    requires: list[Requires] = []
    for _ in range(reader.u2()):
        req_name = _module_name(pool, reader.u2())
        req_flags = reader.u2()
        req_version_index = reader.u2()
        requires.append(Requires(
            req_name, req_flags,
            pool.utf8_or_empty(req_version_index) if req_version_index else "",
        ))

    def read_exports() -> list[Exports]:
        out: list[Exports] = []
        for _ in range(reader.u2()):
            package = _module_name(pool, reader.u2())
            exp_flags = reader.u2()
            to = tuple(_module_name(pool, reader.u2()) for _ in range(reader.u2()))
            out.append(Exports(package, exp_flags, to))
        return out

    exports = read_exports()
    opens = read_exports()
    uses = [pool.class_name(reader.u2()).replace("/", ".") for _ in range(reader.u2())]

    provides: list[tuple[str, tuple[str, ...]]] = []
    for _ in range(reader.u2()):
        service = pool.class_name(reader.u2()).replace("/", ".")
        impls = tuple(
            pool.class_name(reader.u2()).replace("/", ".") for _ in range(reader.u2())
        )
        provides.append((service, impls))

    return ModuleDescriptor(
        name=name, flags=flags, version=version, requires=requires,
        exports=exports, opens=opens, uses=uses, provides=provides,
    )

@dataclass
class JarEntry:
    name: str
    size: int
    compress_type: int
    is_dir: bool


class JarFile:
    """An installed jar, read as the container it is.

    The counterpart of the ELF reader's archive support, and used the same way:
    open it once, ask it what it contains, parse the members that matter.  Entry
    names are kept exactly as stored, because a duplicate entry or an entry with a
    leading `/` or a `..` component is itself a finding.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        if not self.path.exists():
            raise ClassFileError(f"{self.path}: no such file")
        try:
            self.zip = zipfile.ZipFile(self.path)
        except (zipfile.BadZipFile, OSError) as exc:
            raise ClassFileError(f"{self.path}: not a readable zip: {exc}") from exc
        # infolist rather than namelist: duplicates collapse in namelist, and a
        # duplicate entry is a finding rather than a detail.  Which of two
        # same-named entries a JVM loads is not something to leave unstated.
        self.infos = list(self.zip.infolist())

    def close(self) -> None:
        self.zip.close()

    def __enter__(self) -> JarFile:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- entries ----------------------------------------------------------

    def entries(self) -> list[JarEntry]:
        return [
            JarEntry(i.filename, i.file_size, i.compress_type, i.is_dir())
            for i in self.infos
        ]

    def names(self) -> list[str]:
        """Every stored name, duplicates included and in stored order."""
        return [i.filename for i in self.infos]

    def duplicate_names(self) -> list[str]:
        seen: set[str] = set()
        duplicates: set[str] = set()
        for name in self.names():
            if name in seen:
                duplicates.add(name)
            seen.add(name)
        return sorted(duplicates)

    def unsafe_names(self) -> list[str]:
        """Entries that would escape the extraction directory, or are absolute."""
        bad: list[str] = []
        for name in self.names():
            if name.startswith("/") or name.startswith("\\"):
                bad.append(name)
            elif ".." in Path(name.replace("\\", "/")).parts:
                bad.append(name)
        return bad

    def read(self, name: str) -> bytes:
        try:
            return self.zip.read(name)
        except KeyError as exc:
            raise ClassFileError(f"{self.path}: no entry {name}") from exc

    def has(self, name: str) -> bool:
        return name in set(self.names())

    # -- contents ---------------------------------------------------------

    def class_entry_names(self) -> list[str]:
        return sorted(
            n for n in set(self.names())
            if n.endswith(".class") and not n.endswith("/")
        )

    def classes(self, skip_module_info: bool = True) -> list[ClassFile]:
        """Parse every class in the jar.

        Raises on the first unparseable one, naming it.  A jar with a class the
        JVM cannot load is broken in a way that matters more than whatever the
        caller was about to measure.
        """
        out: list[ClassFile] = []
        for name in self.class_entry_names():
            if skip_module_info and name.endswith(MODULE_INFO_ENTRY):
                continue
            out.append(ClassFile(self.read(name), f"{self.path.name}:{name}"))
        return out

    def packages(self) -> set[str]:
        """Packages that actually contain a class, not directory entries.

        Directory entries are optional in a jar and a build may or may not write
        them, so counting them would make the answer depend on how the jar was
        produced rather than on what it holds.
        """
        found: set[str] = set()
        for name in self.class_entry_names():
            if name.endswith(MODULE_INFO_ENTRY):
                continue
            if "/" in name:
                found.add(name.rsplit("/", 1)[0].replace("/", "."))
            else:
                found.add("")
        return found

    def module_descriptor(self) -> ModuleDescriptor | None:
        """The module descriptor, or None for a jar that declares no module."""
        if not self.has(MODULE_INFO_ENTRY):
            return None
        cls = ClassFile(self.read(MODULE_INFO_ENTRY), MODULE_INFO_ENTRY)
        if not cls.is_module:
            raise ClassFileError(f"{self.path}: {MODULE_INFO_ENTRY} is not a module")
        return parse_module_attribute(cls)

    def manifest(self) -> dict[str, str]:
        """The main section of META-INF/MANIFEST.MF.

        Only the main section: per-entry sections describe individual entries and
        nothing in the contract reads them.  Continuation lines are joined per the
        jar specification, which folds at 72 bytes and continues with a single
        leading space -- a manifest written by hand rather than by `jar` often
        gets this wrong, and silently dropping the tail of a long value would
        misreport an attribute as absent.
        """
        if not self.has("META-INF/MANIFEST.MF"):
            return {}
        text = self.read("META-INF/MANIFEST.MF").decode("utf-8", "replace")
        attributes: dict[str, str] = {}
        current: str | None = None
        for raw in text.split("\n"):
            line = raw.rstrip("\r")
            if not line:
                # A blank line ends the main section.
                break
            if line.startswith(" ") and current is not None:
                attributes[current] += line[1:]
                continue
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            current = key.strip()
            attributes[current] = value.strip()
        return attributes

    def signature_entries(self) -> list[str]:
        """META-INF signature files, which pin the jar to one signer."""
        out: list[str] = []
        for name in set(self.names()):
            upper = name.upper()
            if not upper.startswith("META-INF/"):
                continue
            if upper.endswith((".SF", ".DSA", ".RSA", ".EC")):
                out.append(name)
        return sorted(out)

def internal(dotted: str) -> str:
    """`java.util.zip.Deflater` -> `java/util/zip/Deflater`."""
    return dotted.replace(".", "/")


def matches_prefix(name: str, prefixes: list[str] | tuple[str, ...]) -> str | None:
    """The first forbidden prefix a name falls under, comparing dotted forms.

    Both sides are normalized to dotted form with any trailing separator removed,
    and that normalization is load-bearing rather than cosmetic.  The contract
    writes package prefixes the way the class file format does -- `java/util/zip/`,
    with a trailing slash -- so a comparison that only swapped slashes for dots
    would be testing against `java.util.zip.`, whose `startswith(wanted + ".")`
    can never match anything.  That failure mode is silent and it disarms the most
    important gate in the task, so the trailing separator is stripped here and the
    self-test drives the contract's own strings rather than a convenient copy.

    Prefix rather than equality: forbidding `java.util.zip` has to catch
    `java.util.zip.Deflater` and `java.util.zip.DataFormatException` without
    enumerating the package, while still not catching `java.util.zipfile.Nope`.
    """
    dotted = name.replace("/", ".")
    for prefix in prefixes:
        wanted = prefix.replace("/", ".").rstrip(".")
        if not wanted:
            continue
        if dotted == wanted or dotted.startswith(wanted + "."):
            return prefix
        # A nested type: java.util.zip.Deflater$1 under prefix ...Deflater.
        if dotted.startswith(wanted + "$"):
            return prefix
    return None


def is_class_file(path: Path) -> bool:
    try:
        with open(path, "rb") as handle:
            return struct.unpack(">I", handle.read(4))[0] == CLASS_MAGIC
    except (OSError, struct.error):
        return False


def is_jar(path: Path) -> bool:
    return zipfile.is_zipfile(path)


def load_class(path: Path) -> ClassFile:
    path = Path(path)
    return ClassFile(path.read_bytes(), path.name)


def load_jar(path: Path) -> JarFile:
    return JarFile(Path(path))

