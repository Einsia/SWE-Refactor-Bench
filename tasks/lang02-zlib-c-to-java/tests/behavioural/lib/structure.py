#!/usr/bin/env python3
"""Structural verification: the build, the install tree, the artifact, the API.

A compatibility rewrite is not finished when the new code produces the right
bytes.  It is finished when what a distributor ships is still something a
downstream can consume.  For the C original that meant a SONAME, 88 exported
symbols under 14 version nodes, a generated header and a pkg-config file.  After
a port to the JVM it means a different list of the same kind of thing: a jar at a
published path, a module descriptor that exports exactly one package, 115 public
members with unchanged signatures, 40 constants holding unchanged values, and
class files that reference nothing outside java.base.

Every check is scored on its own, so a submission that gets the library right but
drops the stable-name symlink loses only that case.  Checks read the *installed*
tree rather than the build directory, because the install tree is the release.

Four things are specific to this task.

**The expectations are the contract, not a reference build.**  There is nothing to
diff against: the reference publishes a shared object and a header, and State B
publishes a jar.  So the expectations come from source-contract.json, which is a
deliverable of the task rather than a description of the reference -- and the
contract's own internal consistency is asserted at startup by Expectations.load(),
so a contract that drifted from itself fails loudly instead of grading everyone
against a stale number.

**The API is read twice, from two independent readings of the same jar.**
classfile.py parses the class files as bytes; surface/Surface.java loads them in a
JVM and reflects.  Neither subsumes the other.  The parse cannot tell whether a
class loads at all; reflection cannot see a ConstantValue attribute that disagrees
with what the static initializer assigns -- and since javac inlines the former at
every use site, those two numbers disagreeing is a real ABI split with no C
analogue.  Both are graded, and where they overlap they must agree.

**The two linkage modes are graded separately because they fail apart.**  On the
module path a descriptor that forgot to export org.zlib fails at compile time with
"package org.zlib is not visible"; on the class path the same jar works perfectly,
because module-info.class is inert there.  A submission that only ever ran one way
usually cannot run the other.

**The install inventory is mostly absences.**  Two entries are required and eleven
categories of file are forbidden.  That is not padding: an install tree that still
ships a `.so`, a `.a`, a header or a `.pc` file is advertising a C ABI that no
longer exists, and a consumer that believes the advertisement fails at link time
rather than here.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import classfile
import vlib
from build import (
    MODE_CLASSPATH,
    MODE_MODULE,
    BuildOutcome,
    Builder,
    compile_java_probe,
    jar_describe_module,
    java_command,
    java_compile,
    java_tool,
    probe_class_dir,
)
from vlib import CaseOutcome, Log

RUN_TIMEOUT = 120.0
JAVAC_TIMEOUT = 600.0

# Files permitted in the install tree beyond the contract's inventory.  Empty by
# design: zlib's own install rules are explicit about every file, so there is no
# upstream quirk to accommodate, and after the port the whole tree is two entries.
INSTALL_ALLOWED_EXTRA: set[str] = set()

# Suffixes that mean implementation detail escaped into the release.  `.java` and
# `.class` are here for the same reason `.c` is: a shipped source or a loose class
# file is a second copy of the implementation, and the install inventory names
# neither.  `.class` inside the jar is of course the artifact; this list is applied
# to the install *tree*.
SOURCE_LEAK_SUFFIXES = (
    ".c", ".cc", ".cpp", ".h", ".hpp", ".o", ".obj", ".a", ".java", ".class",
    ".jmod", ".d", ".cmakein", ".in", ".py", ".sh",
)

# The classes Surface.java is asked to reflect over.  Derived from the contract at
# load time rather than listed here; this is only the ordering seed, so the dump is
# stable across runs and a diff between two runs is a real difference.
SURFACE_ARGS_NOTE = "derived from api_contract.classes and their nested types"

# Exceptions a method may name in `throws` without changing how a caller is
# written.  The contract declares no checked exception anywhere -- C signals
# failure by return code and every code is a pinned constant, so a port that
# throws instead has replaced the interface rather than translated it -- but an
# unchecked exception in a throws clause is documentation: javac does not require
# the caller to handle it, and it can be thrown with or without the clause.
# Listing it is often the more informative choice, so it is not penalized.
UNCHECKED_EXCEPTIONS = frozenset({
    "java.lang.RuntimeException",
    "java.lang.IllegalArgumentException",
    "java.lang.IllegalStateException",
    "java.lang.NullPointerException",
    "java.lang.IndexOutOfBoundsException",
    "java.lang.ArrayIndexOutOfBoundsException",
    "java.lang.UnsupportedOperationException",
    "java.lang.ArithmeticException",
    "java.lang.OutOfMemoryError",
    "java.lang.Error",
})

# A downstream consumer, written the way a downstream would write it: against the
# published API, with the jar as its only input.
#
# It is deliberately small.  The exhaustive API differential lives in Probe.java;
# what this exists to prove is that a naive consumer, compiled the naive way,
# builds and runs -- and it is compiled in both linkage modes, because that is the
# distinction module-info.class actually controls.
#
# It prints its results, and structure.py checks the values it prints rather than
# only its exit status.  A consumer that compiles, links, runs and computes the
# wrong CRC has not demonstrated a usable library, and the numbers are the pinned
# ones from the contract's behavioral section rather than a reference run: there is
# no C consumer to compare against here, since the C consumer cannot link a jar.
CONSUMER_SRC = r"""
import org.zlib.Zlib;

public final class Consumer {
    public static void main(String[] args) {
        String text = "zlib is a general purpose data compression library. "
                + "zlib is a general purpose data compression library.";
        byte[] source = text.getBytes(java.nio.charset.StandardCharsets.US_ASCII);
        byte[] packed = new byte[512];
        byte[] back = new byte[512];
        int[] packedLen = { packed.length };
        int[] backLen = { back.length };

        System.out.println("version=" + Zlib.version());
        System.out.println("bound=" + Zlib.compressBound(source.length));
        int rc = Zlib.compress(packed, packedLen, source, source.length);
        if (rc != Zlib.Z_OK) {
            System.out.println("compress=" + rc);
            System.exit(2);
        }
        System.out.println("packed_len=" + packedLen[0]);
        System.out.println("packed_crc="
                + Long.toHexString(Zlib.crc32(0L, packed, 0, packedLen[0])));
        rc = Zlib.uncompress(back, backLen, packed, packedLen[0]);
        if (rc != Zlib.Z_OK) {
            System.out.println("uncompress=" + rc);
            System.exit(3);
        }
        System.out.println("back_len=" + backLen[0]);
        boolean same = backLen[0] == source.length;
        for (int i = 0; same && i < source.length; i++) {
            same = back[i] == source[i];
        }
        System.out.println("roundtrip=" + (same ? 1 : 0));
        System.out.println("adler="
                + Long.toHexString(Zlib.adler32(
                        Zlib.adler32(0L, null, 0, 0), source, 0, source.length)));
    }
}
"""

# What the consumer must print, independent of the submission.  These are facts
# about zlib's format rather than about an implementation: the uncompressed length
# round-trips, the Adler-32 of a known string is a known number, and compressBound
# of 101 bytes is what the documented formula yields.  Only the fields whose value
# is fixed by the format are listed -- `packed_len` and `packed_crc` are not, since
# they depend on the level's exact output and are graded to the byte by the
# thousands of probe cases that exist for it.
CONSUMER_EXPECTED = {
    "version": "1.3.1",
    "back_len": "103",
    "roundtrip": "1",
    # adler32 of the 103-byte sentence, from the pinned reference.
    "adler": "c07e2673",
    # compressBound(103), by compress.c's formula:
    #   103 + (103 >> 12) + (103 >> 14) + (103 >> 25) + 13 = 116
    "bound": "116",
}


@dataclass
class Expectations:
    """What the contract says the release looks like, checked against itself.

    This replaces the RefInfo of the C-to-C form.  There, expectations were read
    out of a reference install and cross-checked against the contract, so a drift
    in either was caught by the other.  Here there is no reference to read -- the
    reference publishes a `.so` and State B publishes a jar -- so the cross-check
    has to be internal: the contract states the same facts in several places, and
    load() asserts that those statements agree.

    Concretely, the jar's required entries and the API's class list are two
    independent spellings of "which classes exist", `symbol_map` is a third
    spelling of "which members exist", and the module contract's exports and the
    jar contract's allowed packages are two spellings of "which packages exist".
    A contract in which those disagree cannot grade anything correctly, so it fails
    at startup rather than at case 3,000.
    """

    contract: dict
    pristine: Path | None
    # The 88 C entry points, in the contract's order, and where each one went.
    symbol_map: dict[str, str]
    # Fully qualified names of every class the API declares, nested types included,
    # in the order Surface.java should be asked about them.
    surface_classes: tuple[str, ...]
    # name -> declared member records, keyed by the spelling Surface.java emits.
    api_types: dict[str, dict]
    int_constants: dict[str, int]
    string_constants: dict[str, str]
    jar_relpath: str
    module_name: str
    major_version: int

    @property
    def api(self) -> dict:
        return self.contract["api_contract"]

    @property
    def jar_contract(self) -> dict:
        return self.contract["jar_contract"]

    @property
    def module_contract(self) -> dict:
        return self.contract["module_contract"]

    @property
    def classfile_contract(self) -> dict:
        return self.contract["classfile_contract"]

    @property
    def inventory(self) -> dict:
        return self.contract["install_inventory"]

    @property
    def constants_holder(self) -> str:
        return self.api["constants"]["holder"]

    def member_count(self) -> int:
        """Every declared member, nested types included."""
        total = 0
        for spec in self.api["classes"]:
            total += len(spec.get("methods", []))
            total += len(spec.get("fields", []))
            total += len(spec.get("constructors", []))
            for nested in spec.get("nested", []):
                total += len(nested.get("methods", []))
        return total

    @classmethod
    def load(cls, contract: dict, pristine: Path | None = None) -> Expectations:
        api = contract["api_contract"]
        jar = contract["jar_contract"]
        module = contract["module_contract"]
        classfile_c = contract["classfile_contract"]

        # -- flatten the class list, nested types included -----------------
        api_types: dict[str, dict] = {}
        order: list[str] = []
        for spec in api["classes"]:
            api_types[spec["name"]] = spec
            order.append(spec["name"])
            for nested in spec.get("nested", []):
                api_types[nested["name"]] = nested
                order.append(nested["name"])

        # -- assertion 1: the jar's entries and the API's classes agree ----
        # Two independent spellings of "which classes exist".  A class in the API
        # that the jar does not carry cannot be reached; an entry in the jar that
        # the API does not declare is either dead weight or an undeclared surface.
        entry_paths = {item["path"] for item in jar["required_entries"]}
        for name in api_types:
            # Nested types are Outer$Inner as a file name and Outer.Inner in Java
            # source.  The contract writes them with the `$` already, so no guessing
            # is needed here: internal() only has to swap dots for slashes.
            want = classfile.internal(name) + ".class"
            if want not in entry_paths:
                raise SystemExit(
                    f"contract is inconsistent: api_contract declares {name} but "
                    f"jar_contract.required_entries has no {want}"
                )
        if module["descriptor_path"] not in entry_paths:
            raise SystemExit(
                "contract is inconsistent: module_contract.descriptor_path "
                f"({module['descriptor_path']}) is not a required jar entry"
            )

        # -- assertion 2: every C entry point lands on a declared member ---
        # symbol_map is the migration's own account of where each of the 88 C
        # entry points went.  It is written by hand, and a typo in it would be
        # invisible: nothing else reads it, so the wrong answer would simply
        # never be checked.  Resolving all 88 against the class list makes it
        # checkable.
        declared: set[str] = set()
        for name, spec in api_types.items():
            short = name.split(".")[-1]
            for method in spec.get("methods", []):
                declared.add(f"{short}.{method['name']}({','.join(method['params'])})")
            for fld in spec.get("fields", []):
                declared.add(f"{short}.{fld['name']}")
            for ctor in spec.get("constructors", []):
                declared.add(f"{short}({','.join(ctor['params'])})")
        unresolved = sorted(
            f"{c}->{j}" for c, j in api["symbol_map"].items() if j not in declared
        )
        if unresolved:
            raise SystemExit(
                "contract is inconsistent: symbol_map entries name members the "
                f"api_contract does not declare: {unresolved[:6]}"
            )
        if len(api["symbol_map"]) != int(api["state_a_symbol_count"]):
            raise SystemExit(
                f"contract is inconsistent: symbol_map has "
                f"{len(api['symbol_map'])} entries, state_a_symbol_count says "
                f"{api['state_a_symbol_count']}"
            )

        # -- assertion 3: exported packages are packages the jar may carry --
        allowed = tuple(jar["packages_allowed"])
        for package in module["exports"]:
            if classfile.matches_prefix(package, allowed) is None:
                raise SystemExit(
                    f"contract is inconsistent: module exports {package}, which "
                    f"jar_contract.packages_allowed ({allowed}) does not permit"
                )

        # -- assertion 3b: nested types are spelled one way throughout ------
        # A nested class is Outer.Inner in Java source and Outer$Inner from
        # Class.getName(), and the contract has to pick one because Surface.java
        # emits the second.  It picks `$`, and the risk is that a member's type or
        # parameter list is written in the other spelling by hand -- which is what
        # happened: two ZStream fields said ZStream.Allocator while the class list
        # and every parameter said ZStream$Allocator.
        #
        # The cost of that typo was two cases failing on every correct submission,
        # reported as "want org.zlib.ZStream.Allocator" -- a message that reads like
        # the submission is wrong.  A contract defect that accuses submissions is
        # the worst kind, so the spelling is checked here instead.
        dollar_names = {name for name in api_types if "$" in name}
        dotted = {name.replace("$", "."): name for name in dollar_names}
        for name, spec in api_types.items():
            references: list[tuple[str, str]] = []
            for method in spec.get("methods", []):
                references.append((f"{name}.{method['name']} return", method["returns"]))
                for param in method["params"]:
                    references.append((f"{name}.{method['name']} param", param))
            for fld in spec.get("fields", []):
                references.append((f"{name}.{fld['name']} type", fld["type"]))
            for ctor in spec.get("constructors", []):
                for param in ctor["params"]:
                    references.append((f"{name} ctor param", param))
            for where, ref in references:
                bare = ref.removesuffix("[]")
                if bare in dotted:
                    raise SystemExit(
                        f"contract is inconsistent: {where} is written {ref!r}, but "
                        f"the class list spells that type {dotted[bare]!r}; nested "
                        "types must use $ throughout because that is what "
                        "Class.getName() returns"
                    )

        # -- assertion 4: the constants holder is a declared class ---------
        if api["constants"]["holder"] not in api_types:
            raise SystemExit(
                "contract is inconsistent: constants holder "
                f"{api['constants']['holder']} is not a declared class"
            )

        # -- assertion 5: the artifact path is one path, said once ----------
        artifact = jar["artifact"]
        inventory_files = {
            item["path"] for item in contract["install_inventory"]["common"]
        }
        if artifact not in inventory_files:
            raise SystemExit(
                f"contract is inconsistent: jar_contract.artifact ({artifact}) is "
                f"not in install_inventory.common ({sorted(inventory_files)})"
            )

        # -- assertion 6: the prefix matcher understands the contract's spelling
        #
        # The two forbidden lists answer at different granularities on purpose: the
        # prefixes ban whole packages, the type list bans specific classes that sit
        # in packages nobody could ban wholesale -- java.lang.Runtime is forbidden,
        # and java/lang/ obviously is not.  So they are not nested, and asserting
        # that they were would be asserting something false.
        #
        # What is worth asserting is that the matcher agrees with how the contract
        # spells a prefix.  These are written in internal form with a trailing
        # slash ("java/util/zip/"), and a matcher that mishandled that form would
        # fail *open*: it would find nothing forbidden and pass every cheating
        # submission silently.  That happened once during development, and it was
        # caught by a test that had hardcoded the prefixes without their trailing
        # slashes -- which is to say, nearly not caught at all.  So each prefix is
        # probed here, in the contract's own spelling, before anything is graded.
        prefixes = tuple(classfile_c["forbidden_prefixes"])
        for prefix in prefixes:
            probe = prefix.rstrip("/") + "/Probe"
            if classfile.matches_prefix(probe, prefixes) is None:
                raise SystemExit(
                    f"forbidden-prefix matching is broken: {probe!r} is not "
                    f"matched by {prefix!r}; the anti-cheat scan would fail open"
                )
        if classfile.matches_prefix("org/zlib/Deflater", prefixes) is not None:
            raise SystemExit(
                "forbidden-prefix matching is broken: org/zlib/Deflater matches a "
                "forbidden prefix, so every correct submission would be rejected"
            )

        return cls(
            contract=contract,
            pristine=pristine,
            symbol_map=dict(api["symbol_map"]),
            surface_classes=tuple(order),
            api_types=api_types,
            int_constants=dict(api["constants"]["int_values"]),
            string_constants=dict(api["constants"]["string_values"]),
            jar_relpath=artifact,
            module_name=module["name"],
            major_version=int(classfile_c["major_version"]),
        )


@dataclass
class SurfaceDump:
    """One reflective reading of one install tree's jar."""

    ok: bool
    mode: str
    detail: str
    records: list[str] = field(default_factory=list)

    def of_kind(self, kind: str) -> list[str]:
        head = kind + " "
        return [r[len(head):] for r in self.records if r.startswith(head)]

    def behavior(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for record in self.of_kind("behavior"):
            key, _, value = record.partition(" = ")
            out[key.strip()] = value.strip()
        return out


# Lines that name what went wrong, most specific first.  A Python traceback puts
# its exception on the *last* line; make puts its own summary after that.
_DIAGNOSTIC = re.compile(
    r"^(?:\w+(?:Error|Exception)\b.*"          # FileNotFoundError: ...
    r"|.*\berror:.*"                            # javac / gcc / cmake
    r"|.*\bCMake Error\b.*"
    r"|.*\bNo such file or directory\b.*"
    r"|.*\bPermission denied\b.*"
    r"|.*\bfatal error\b.*)$",
    re.IGNORECASE)


def _diagnosis(result: vlib.Result, lines: int = 12, limit: int = 1200) -> str:
    """The tail, but led by the line that names the failure.

    `Result.tail()` keeps the *last* lines and joins them with " | "; the report
    renderer keeps the *front* of a summary and only the first six lines of
    detail.  The two truncations point in opposite directions, so a summary built
    from a bare tail opens mid-traceback: the graded run of this suite reported a
    jar that failed to build as `... File ".../jarfix.py", line 28, in main`, and
    the `FileNotFoundError` naming the cause survived in neither the summary nor
    the first rendered line.

    So the deepest diagnostic line is hoisted to the front, and the tail follows
    it for context.  When nothing matches -- a tool that failed silently, a
    timeout -- this is exactly `tail()`, which is what it already was.
    """
    blob = (result.stderr or result.stdout).decode("utf-8", "replace").rstrip()
    if not blob:
        blob = (result.stdout or b"").decode("utf-8", "replace").rstrip()
    tail = " | ".join(blob.splitlines()[-lines:])[-limit:]
    hits = [ln.strip() for ln in blob.splitlines()
            if ln.strip() and _DIAGNOSTIC.match(ln.strip())]
    if not hits:
        return tail
    # The last match, not the first: a traceback's exception is at the bottom, and
    # a chained one ends with the error that actually stopped the build.
    lead = hits[-1][:limit]
    if tail.startswith(lead):
        return tail
    return f"{lead} || {tail}"[:limit * 2]


class StructureEvaluator:
    """Runs every declared structural case against one submission build."""

    def __init__(
        self,
        repo: Path,
        outcomes: dict[str, BuildOutcome],
        expectations: Expectations,
        contract: dict,
        scratch: Path,
        # Both are directories -- tests/probe and tests/surface -- not files.
        # Each holds one Java program, but as a directory rather than a single
        # path, so a helper class can be added beside the main one without every
        # call site learning about it.  _java_sources() resolves either form.
        probe_src: Path,
        surface_src: Path,
        log: Log,
    ) -> None:
        self.repo = repo
        self.outcomes = outcomes
        self.expect = expectations
        self.contract = contract
        self.scratch = scratch
        self.probe_src = probe_src
        self.surface_src = surface_src
        self.log = log
        self._jar_cache: dict[Path, classfile.JarFile | None] = {}
        self._class_cache: dict[str, list[classfile.ClassFile]] = {}
        self._surface_cache: dict[str, SurfaceDump] = {}
        self._probe_cache: dict[tuple[str, str], tuple[bool, str]] = {}
        self._consumer_cache: dict[tuple[str, str], tuple[bool, str]] = {}
        self._describe_cache: dict[Path, vlib.Result] = {}
        # Absences noticed while the current case ran; see jar_path and
        # _run_all_configs.  Cleared per case, so it never carries across.
        self._absent_evidence: list[str] = []
        # "" for a graded submission.  A sentence for the operator's reference
        # self-test, in which case a check with no evidence is `skip` rather than a
        # failure -- vlib.c_self_test states the three conditions and why none of
        # them is reachable by a submission.
        self.self_test = vlib.c_self_test(
            [o.prefix for o in outcomes.values() if o.installed],
            expectations.jar_relpath,
        )
        if self.self_test:
            log.write(f"structure: {self.self_test}")
        scratch.mkdir(parents=True, exist_ok=True)

    # -- plumbing --------------------------------------------------------

    @staticmethod
    def _java_sources(where: Path) -> list[Path]:
        """The .java files at `where`, whether it names a file or a directory.

        javac takes files; the verifier's Java lives in a directory per program.
        Sorted so the command line is identical between runs, which matters when a
        javac diagnostic is being compared against a previous one.
        """
        if where.is_dir():
            return sorted(where.glob("*.java"))
        return [where]

    def prefix(self, config: str) -> Path:
        return self.outcomes[config].prefix

    def outcome(self, config: str) -> BuildOutcome:
        return self.outcomes[config]

    def probe_scratch(self, config: str) -> Path:
        """Where the build probes put their alternate trees.

        Distinct from self.scratch, which is this evaluator's own space.  The probe
        trees belong to the builder, so the path comes off the outcome the builder
        produced rather than being assumed to match -- when the two sides were told
        separately they disagreed silently, and the cases reported a directory that
        "was not created" when in fact it had been created somewhere else.
        """
        return self.outcome(config).scratch or self.scratch

    def jar_path(self, config: str) -> Path:
        """Where the artifact should be, and a note when it is not there.

        The note is what makes "this check had no evidence" mechanical rather than
        a list somebody maintains.  Every case that asks about the delivery reaches
        the jar through this method -- to open it, to stat it, to hand it to javac,
        to name it in a message -- so recording the absence here records it for all
        of them, and a case that fails without ever asking has failed for its own
        reasons.  `_run_all_configs` reads the ledger and decides.

        A list rather than a flag, and appended to rather than replaced, because a
        case declared for both configurations must not have `static`'s absence
        overwritten by `shared`'s presence.
        """
        path = self.prefix(config) / self.expect.jar_relpath
        if not path.exists():
            self._absent_evidence.append(
                f"[{config}] {self.expect.jar_relpath} is not installed")
        return path

    def note_absent(self, config: str, what: str) -> None:
        """Record an absence the ledger would otherwise not see.

        jar_path() covers the cases that ask where the jar is.  Three kinds do not
        go through it and still fail only because the delivery is missing: a case
        reading a builder stamp taken in an alternate prefix, a case reading a
        cached answer whose first computation recorded the absence, and the stable
        link, which has its own path in the inventory.  They call this instead.

        Same list, same de-duplication, same rule in `_run_all_configs`: harvested
        only when the case failed, so a case that passed while noticing an absence
        keeps its real verdict.
        """
        self._absent_evidence.append(f"[{config}] {what}")

    def stable_jar_path(self, config: str) -> Path:
        """The unversioned name, which stands in for State A's libz.so.1 symlink."""
        for item in self.expect.inventory["common"]:
            if item.get("kind") == "symlink":
                path = self.prefix(config) / item["path"]
                if not path.exists() and not path.is_symlink():
                    self.note_absent(config, f"{item['path']} is not installed")
                return path
        return self.jar_path(config).with_name("zlib.jar")

    def jar(self, config: str) -> classfile.JarFile | None:
        """The installed artifact, opened once per configuration."""
        path = self.jar_path(config)
        if path not in self._jar_cache:
            try:
                self._jar_cache[path] = classfile.load_jar(path)
            except (OSError, classfile.ClassFileError) as exc:
                self.log.write(f"jar unreadable ({config}): {type(exc).__name__}: {exc}")
                self._jar_cache[path] = None
        return self._jar_cache[path]

    def classes(self, config: str) -> list[classfile.ClassFile]:
        """Every parsed class in the artifact, module-info excluded.

        The empty-cache re-note is not redundant.  Five classfile/* cases share one
        cached list, and the ledger is cleared per case, so only the first of them
        reached jar_path() and the other four failed with no absence recorded -- the
        same cache-hit gap audit.py's _observe had.
        """
        if config in self._class_cache and not self._class_cache[config]:
            self.jar(config)
        if config not in self._class_cache:
            archive = self.jar(config)
            if archive is None:
                self._class_cache[config] = []
            else:
                try:
                    self._class_cache[config] = archive.classes()
                except classfile.ClassFileError as exc:
                    self.log.write(f"class parse failed ({config}): {exc}")
                    self._class_cache[config] = []
        return self._class_cache[config]

    def describe_module(self, config: str) -> vlib.Result:
        path = self.jar_path(config)
        if path not in self._describe_cache:
            self._describe_cache[path] = jar_describe_module(
                path, self.log, label=f"describe-module-{config}"
            )
        return self._describe_cache[path]

    def surface(self, config: str) -> SurfaceDump:
        """Reflect over the installed jar from inside a JVM, once per config.

        Tried on the module path first, because that is the stricter arrangement and
        the one the contract describes.  On failure it retries on the class path and
        records which mode answered.  The fallback is not leniency: whether the
        module path works is graded by link/probe-modulepath and module/*, and
        letting a broken descriptor also void all ten api/* cases would charge one
        defect ten more times and bury the API findings underneath it.
        """
        if config in self._surface_cache:
            cached = self._surface_cache[config]
            if not cached.ok:
                # Ten api/* cases share this dump.  Without this the first one
                # records why it had no evidence and the other nine do not.
                self.jar_path(config)
            return cached
        jar = self.jar_path(config)
        if not jar.is_file():
            dump = SurfaceDump(False, "-", f"{self.expect.jar_relpath} is missing")
            self._surface_cache[config] = dump
            return dump
        last = ""
        for mode in (MODE_MODULE, MODE_CLASSPATH):
            out = self.scratch / f"surface-{config}-{mode}"
            shutil.rmtree(out, ignore_errors=True)
            compiled = java_compile(
                self._java_sources(self.surface_src), out, self.log,
                label=f"surface-javac-{config}-{mode}", mode=mode, jar=jar,
                module_name=self.expect.module_name,
            )
            if not compiled.ok:
                last = f"[{mode}] Surface.java did not compile: {compiled.tail()}"
                continue
            argv = java_command(
                "Surface", mode=mode, jar=jar, class_dir=out,
                module_name=self.expect.module_name,
            ) + list(self.expect.surface_classes)
            run = vlib.run(
                argv, cwd=self.scratch, env=vlib.base_env(),
                timeout=RUN_TIMEOUT, log=self.log,
                label=f"surface-run-{config}-{mode}",
            )
            records = run.stdout.decode("utf-8", "replace").splitlines()
            if not records:
                last = f"[{mode}] the surface dump produced no records: {run.tail()}"
                continue
            # A non-zero exit means at least one class did not load.  The records
            # are kept regardless: `missing <name> error=...` is the single most
            # useful thing in the dump, and discarding it because the process
            # reported failure would discard the diagnosis with the symptom.
            dump = SurfaceDump(
                True, mode,
                "" if run.ok else f"[{mode}] some classes did not load",
                records,
            )
            self._surface_cache[config] = dump
            self.log.write(
                f"surface ({config}/{mode}): {len(records)} records, "
                f"rc={run.returncode}"
            )
            return dump
        dump = SurfaceDump(False, "-", last or "the surface dump did not run")
        self._surface_cache[config] = dump
        return dump

    def evaluate(self, cases: list[dict]) -> list[CaseOutcome]:
        results: list[CaseOutcome] = []
        for case in cases:
            check = case["check"]
            configs = list(case.get("configs") or ["shared"])
            handler = getattr(self, f"check_{check.replace('-', '_')}", None)
            start = vlib.now()
            unanswered = ""
            if handler is None:
                passed, detail = False, f"no handler for check '{check}'"
            else:
                passed, detail, unanswered = self._run_all_configs(handler, configs)
            results.append(
                CaseOutcome(
                    case_id=case["id"],
                    family=case["family"],
                    kind="struct",
                    passed=passed,
                    weight=float(case.get("weight", 1.0)),
                    detail=detail,
                    duration=vlib.now() - start,
                    # Only in the self-test.  While grading, the absence of the
                    # delivery is the submission's own doing and stays a failure --
                    # it just now says what was missing instead of asserting
                    # something about behaviour nobody observed.
                    not_applicable=unanswered if self.self_test else "",
                )
            )
        return results

    def _run_all_configs(self, handler, configs: list[str]
                         ) -> tuple[bool, str, str]:
        """Grade one check in every configuration it is declared for.

        A case declared for both passes only if both pass, and the detail names the
        configuration that failed so the report stays actionable.

        Returns (passed, detail, not_applicable).  The third is non-empty when the
        check failed only because the delivery it reads is absent -- either because
        the handler said so outright by raising vlib.NotApplicable, or because it
        asked jar_path() for an artifact that is not there.  The distinction is
        recorded whichever run this is; what it *costs* is decided by the caller,
        and only in the reference self-test does it stop costing anything.
        """
        details: list[str] = []
        ok = True
        self._absent_evidence = []
        missing: list[str] = []
        for config in configs:
            if config not in self.outcomes:
                return False, f"[{config}] configuration was not built", ""
            try:
                passed, detail = handler(config)
            except vlib.NotApplicable as exc:
                passed, detail = False, exc.reason
                missing.append(f"[{config}] {exc.reason}")
            except Exception as exc:  # a check must never abort the suite
                passed, detail = False, f"{type(exc).__name__}: {exc}"
            if not passed:
                ok = False
                details.append(f"[{config}] {detail}")
            elif detail:
                details.append(f"[{config}] {detail}")
        # The ledger counts only for a case that failed.  A case that passed while
        # noticing an absence -- `install/no-loose-classes` finds no stray class
        # files in a tree with no classes at all -- passed, and reporting it as
        # unanswered would discard a real verdict.
        if not ok:
            missing.extend(self._absent_evidence)
        self._absent_evidence = []
        return ok, "; ".join(details), "; ".join(dict.fromkeys(missing))

    def _phase(self, config: str, name: str) -> tuple[bool, str]:
        result = getattr(self.outcome(config), name)
        if result is None:
            return False, f"{name} was never run"
        if result.ok:
            return True, ""
        return False, (f"{name} failed (rc={result.returncode}): "
                       f"{_diagnosis(result)}")

    def _probe(self, config: str, key: str, what: str) -> tuple[bool, str]:
        result = self.outcome(config).extra.get(key)
        if result is None:
            return False, f"the {what} probe did not run"
        if result.ok:
            return True, ""
        return False, f"{what} failed: {_diagnosis(result)}"

    # -- build -----------------------------------------------------------

    def check_configure(self, config: str) -> tuple[bool, str]:
        return self._phase(config, "configure")

    def check_compile(self, config: str) -> tuple[bool, str]:
        return self._phase(config, "compile")

    def check_install(self, config: str) -> tuple[bool, str]:
        return self._phase(config, "install")

    def check_build_warnings(self, config: str) -> tuple[bool, str]:
        """The build log must be free of hard errors and unresolved references.

        A build can exit 0 and still be broken.  For the C original the tell was
        an unresolved symbol reported as a warning; for javac it is
        "cannot find symbol" and "package does not exist" arriving as errors on a
        target CMake did not mark as required, and `uses unchecked or unsafe
        operations` sitting on top of a cast that will throw at runtime.  CMake's
        own deprecation notice for zlib's `cmake_minimum_required(VERSION
        2.4.4...3.15.0)` is upstream's and is not counted: State A produces it too,
        and a case that failed on it would fail the identity run.
        """
        result = self.outcome(config).compile
        if result is None:
            return False, "build was never run"
        bad: list[str] = []
        for line in result.text().splitlines():
            stripped = line.strip()
            low = stripped.lower()
            if "deprecat" in low and "cmake_minimum_required" in low:
                continue
            if low.startswith("error:") or " error:" in low:
                bad.append(stripped)
            elif "cannot find symbol" in low or "package does not exist" in low:
                bad.append(stripped)
            elif "undefined reference" in low or "undefined symbol" in low:
                bad.append(stripped)
        if bad:
            return False, f"{len(bad)} hard diagnostics, first: {bad[0][:300]}"
        return True, ""

    def check_examples_flag(self, config: str) -> tuple[bool, str]:
        """ZLIB_BUILD_EXAMPLES must still exist and still be honoured.

        Upstream defaults it ON and gates `example` and `minigzip` on it.  The
        drivers themselves are graded by the driver cases; what this asserts is
        that the switch survived -- a submission that deleted the option fails the
        configure, one that kept it but ignored it fails the drivers, and one that
        renamed it breaks every downstream that sets it.
        """
        result = self.outcome(config).configure
        if result is None:
            return False, "configure was never run"
        if not result.ok:
            return False, "configure with ZLIB_BUILD_EXAMPLES=ON failed"
        cache = self.outcome(config).build_dir / "CMakeCache.txt"
        if not cache.is_file():
            return False, "CMakeCache.txt is missing"
        match = re.search(
            r"^ZLIB_BUILD_EXAMPLES:BOOL=(\w+)", cache.read_text(errors="replace"), re.M
        )
        if match is None:
            return False, "ZLIB_BUILD_EXAMPLES is not a cache option any more"
        if match.group(1).upper() not in ("ON", "TRUE", "1", "YES"):
            return False, f"ZLIB_BUILD_EXAMPLES came out {match.group(1)}"
        return True, ""

    def check_reconfigure(self, config: str) -> tuple[bool, str]:
        return self._probe(config, "reconfigure", "second configure")

    def check_rebuild(self, config: str) -> tuple[bool, str]:
        """An immediate second build must stay green and leave the jar alone.

        The C form read the log for recompilation lines.  That does not transfer:
        one javac invocation compiles the whole source set, so there is no
        per-file line to count, and the generator prints the same custom-command
        banner whether or not the command ran.  What is observable is the
        artifact, so the builder stamps the installed jar either side of the second
        build and this reads the comparison.  A jar that gets rewritten every time
        means the custom command declared no OUTPUT or no DEPENDS, which means
        CMake does not know what the build produces -- and a build that cannot say
        what it produces cannot be a dependency of anything.
        """
        ok, detail = self._probe(config, "rebuild", "second build")
        if not ok:
            return ok, detail
        stable = self.outcome(config).extra.get("rebuild-jar-stable")
        if stable is None:
            return False, "the jar-stability stamp was not taken"
        if not stable.ok:
            # Two stamps of a file that was never installed compare equal, so the
            # builder's own message reads "unchanged by the second build" -- which is
            # the pass wording attached to a failure.  Both stamps carrying size -1
            # is that case, and it is an absence, not an unstable artifact.
            if not self.jar_path(config).exists():
                return False, (f"{self.expect.jar_relpath} was never installed, so "
                               f"there was no artifact to compare across the second "
                               f"build ({stable.tail()})")
            return False, stable.tail()
        return True, ""

    def check_cmake_version(self, config: str) -> tuple[bool, str]:
        """Every place the build states its version must state 1.3.1.

        Upstream keeps the version in two places that have to agree: `set(VERSION
        "1.3.1")`, which is substituted into zlib.pc, and a regex over zlib.h that
        lifts ZLIB_VERSION out of the header and becomes the shared library's
        VERSION property, hence its filename.  After the port the .pc file is gone
        and the filename belongs to a jar, so the three observable answers are the
        artifact's name, the manifest's Implementation-Version, and the macro in
        the retained header.

        Grading their agreement rather than any one value is the point.  A
        submission that hardcodes `zlib-1.3.1.jar` in an install rule, and stops
        deriving the version from zlib.h, passes each of those individually and
        still ships a tree whose answers diverge at the next release -- which is
        precisely the state that breaks a distribution's version matching.
        """
        wanted = str(self.contract["product"]["upstream_version"])
        answers: dict[str, str] = {}

        jar = self.jar_path(config)
        match = re.fullmatch(r"zlib-(.+)\.jar", jar.name)
        if not jar.is_file():
            return False, f"{self.expect.jar_relpath} is missing"
        if match is None:
            return False, f"the artifact name carries no version: {jar.name}"
        answers["artifact name"] = match.group(1)

        archive = self.jar(config)
        if archive is not None:
            implementation = archive.manifest().get("Implementation-Version")
            if implementation:
                answers["Implementation-Version"] = implementation

        header = self.repo / "zlib.h"
        if header.is_file():
            match = re.search(
                r'#define\s+ZLIB_VERSION\s+"([^"]+)"',
                header.read_text(errors="replace"),
            )
            if match:
                answers["zlib.h ZLIB_VERSION"] = match.group(1)

        wrong = {k: v for k, v in answers.items() if v != wanted}
        if wrong:
            return False, f"version disagreement (want {wanted}): {wrong}"
        if len(answers) < 2:
            return False, (
                f"only {len(answers)} version statement(s) found ({answers}); "
                "there is nothing left to agree with"
            )
        return True, f"{len(answers)} statements agree on {wanted}"

    def check_outofsource(self, config: str) -> tuple[bool, str]:
        """The documented out-of-source build must produce the whole product.

        Configuring is not enough to check: what has to land outside the source
        tree is now the jar and both driver scripts, and a submission that writes
        its classes next to its sources -- or installs a jar committed to the
        repository -- configures perfectly and produces an empty build directory.
        """
        ok, detail = self._probe(config, "outofsource", "out-of-source configure")
        if not ok:
            return ok, detail
        ok, detail = self._probe(config, "outofsource-build", "out-of-source build")
        if not ok:
            return ok, detail
        missing: list[str] = []
        for name in ("jar", "example", "minigzip"):
            found = self.outcome(config).extra.get(f"outofsource-{name}")
            if found is None or not found.ok:
                missing.append(name)
        if missing:
            if missing == ["jar"]:
                # The drivers appeared and the jar did not: nothing was mislaid, there
                # is no jar in this delivery at all.  Said here because the stamp was
                # taken in the out-of-source build tree, which jar_path never reads.
                self.note_absent(config,
                                 "the out-of-source build tree holds no jar")
            return False, f"produced nothing outside the source tree for: {missing}"
        return True, ""

    def check_skip_install(self, config: str) -> tuple[bool, str]:
        """SKIP_INSTALL_* must still suppress exactly what it names.

        Distributions use these to split zlib into a runtime and a development
        package, so they are part of the build interface rather than a
        convenience.  SKIP_INSTALL_FILES is the interesting one after the port: it
        named the man page and the .pc file, neither of which exists any more, so
        the jar must survive it.  A submission that hangs the jar's install rule
        off whichever guard was nearest gets that backwards, and a distribution
        that sets SKIP_INSTALL_FILES ends up with no library at all.
        """
        details: list[str] = []
        for mode, option, _ in Builder.SKIP_MODES:
            for stage in ("configure", "build", "install"):
                result = self.outcome(config).extra.get(f"skip-{mode}-{stage}")
                if result is None:
                    return False, f"the {option} probe did not reach {stage}"
                if not result.ok:
                    return False, f"{option}=ON: {stage} failed: {result.tail()}"
            verdict = self.outcome(config).extra.get(f"skip-{mode}-jar")
            if verdict is None:
                return False, f"{option}=ON: the jar was not looked for"
            if not verdict.ok:
                # This probe installs into its own prefix, so the primary install is
                # the only place that can distinguish "the guard suppressed the jar"
                # from "this delivery has no jar to suppress".
                if not self.jar_path(config).exists():
                    self.note_absent(config, f"{option}=ON: the install holds no jar, "
                                             f"and neither does the primary install")
                return False, verdict.tail()
            prefix = self.probe_scratch(config) / f"skipprefix-{config}-{mode}"
            if mode == "all":
                leftovers = sorted(
                    str(p.relative_to(prefix)) for p in prefix.rglob("*") if p.is_file()
                )
                if leftovers:
                    return False, (
                        f"SKIP_INSTALL_ALL=ON still installed {len(leftovers)} "
                        f"file(s): {leftovers[:5]}"
                    )
            details.append(f"{option} ok")
        return True, ", ".join(details)

    def check_prefix(self, config: str) -> tuple[bool, str]:
        """A second prefix must get a complete, self-consistent install tree.

        The Java form has a way to fail this that the C form did not.  The driver
        contract asks for an exec'able wrapper, and the natural way to write one
        interpolates the jar's absolute path -- which is fine, and is why the
        wrapper has to be generated per configure rather than once.  A submission
        that generates it once and reuses it produces a tree whose drivers point
        at some other tree's jar, so the scripts are read here and any absolute
        path they carry must be inside the prefix that produced them.
        """
        for stage in ("configure", "build", "install"):
            result = self.outcome(config).extra.get(f"altprefix-{stage}")
            if result is None:
                return False, f"the alternate-prefix probe did not reach {stage}"
            if not result.ok:
                return False, f"alternate prefix: {stage} failed: {result.tail()}"
        prefix = self.probe_scratch(config) / f"altprefix-{config}"
        missing = [
            item["path"] for item in self.expect.inventory["common"]
            if not (prefix / item["path"]).exists()
        ]
        if missing:
            # Only when everything missing is the delivery itself.  A prefix that got
            # the jar but not the drivers is a real defect in the install rules, and
            # must keep its verdict.
            jar_names = {self.expect.jar_relpath}
            jar_names |= {i["path"] for i in self.expect.inventory["common"]
                          if i.get("kind") == "symlink"}
            if set(missing) <= jar_names and not self.jar_path(config).exists():
                self.note_absent(config,
                                 f"the alternate prefix is missing {missing}, and so "
                                 f"is the primary install")
            return False, f"the alternate prefix is missing {missing}"
        escaped: list[str] = []
        primary = str(self.prefix(config))
        for path in sorted(prefix.rglob("*")):
            if not path.is_file() or path.suffix == ".jar":
                continue
            try:
                text = path.read_text(errors="replace")
            except OSError:
                continue
            if primary in text:
                escaped.append(f"{path.relative_to(prefix)} names {primary}")
        if escaped:
            return False, f"installed files point outside their own prefix: {escaped[:3]}"
        return True, ""

    def check_cache_options(self, config: str) -> tuple[bool, str]:
        """The documented cache options still exist with their upstream meaning.

        These are the build's interface to everyone who packages it.  ZLIB_BUILD_
        EXAMPLES is upstream's only `option()`, and the four SKIP_INSTALL_*
        switches are plain variables that upstream tests with `if(NOT ...)`, so
        they do not appear in the cache unless set -- which is why only the option
        is required to be present here and the switches are graded by what they
        do, in check_skip_install.
        """
        result = self.outcome(config).extra.get("cache")
        if result is None:
            return False, "the cache probe did not run"
        if not result.ok:
            return False, f"reading the cache failed: {result.tail()}"
        text = result.text()
        required = {"ZLIB_BUILD_EXAMPLES": ("ON", "TRUE", "1", "YES")}
        for name, allowed in required.items():
            match = re.search(rf"^{name}:BOOL=(\w+)", text, re.M)
            if match is None:
                return False, f"{name} is not a cache option any more"
            if match.group(1).upper() not in allowed:
                return False, f"{name}={match.group(1)}, expected one of {allowed}"
        # CMAKE_INSTALL_PREFIX must be the one that was asked for: a CMakeLists
        # that overrides it breaks every packaging system there is.
        match = re.search(r"^CMAKE_INSTALL_PREFIX:PATH=(.*)$", text, re.M)
        if match is None:
            return False, "CMAKE_INSTALL_PREFIX is absent from the cache"
        if Path(match.group(1).strip()) != self.prefix(config):
            return False, (
                f"CMAKE_INSTALL_PREFIX came out {match.group(1).strip()!r}, "
                f"not the {self.prefix(config)} that was configured"
            )
        return True, ""

    def check_ctest(self, config: str) -> tuple[bool, str]:
        """`example` must still be registered as a test.

        `enable_testing()` and `add_test(example example)` are both unconditional
        in State A, so `ctest -N` lists the test without running it.  Listing is
        the right question: running `example` is already a driver case, and after
        the port the registered command is a shell wrapper around `java`, so
        running it here would grade JVM startup rather than the registration.
        """
        result = self.outcome(config).extra.get("ctest")
        if result is None:
            return False, "the ctest probe did not run"
        if not result.ok:
            return False, f"`ctest -N` failed: {result.tail()}"
        text = result.text()
        if not re.search(r"^\s*Test\s*#\d+:\s*example\b", text, re.M):
            found = re.findall(r"^\s*Test\s*#\d+:\s*(\S+)", text, re.M)
            return False, f"`example` is not a registered test; ctest lists {found}"
        return True, ""

    def check_javac_invoked(self, config: str) -> tuple[bool, str]:
        """The build must compile Java, not stage something already compiled.

        This is the build-system half of the central question.  The audit gates
        establish that the shipped classes carry javac provenance and that no C was
        compiled; what they cannot see is *when* the compilation happened.  A
        submission that commits a jar and has CMake copy it into place satisfies
        every provenance gate -- the classes really were compiled by javac, once,
        on the author's machine -- while having no build at all.

        So two independent traces are required, and they are required together
        because either alone has a legitimate way to be absent.  A build may delete
        its class files after packaging, which empties the first; a build may drive
        javac through a response file or a wrapper script, which can empty the
        second.  Both empty at once is a build that produced a jar without
        compiling anything.
        """
        build_dir = self.outcome(config).build_dir
        classes = [p for p in build_dir.rglob("*.class")]
        result = self.outcome(config).compile
        log = result.text() if result is not None else ""
        saw_javac = bool(re.search(r"\bjavac\b", log))
        # The jar's own class count, as the thing that had to be compiled from
        # something.  An empty jar is a different failure and is caught elsewhere.
        archive = self.jar(config)
        in_jar = len(archive.class_entry_names()) if archive is not None else 0
        if not classes and not saw_javac:
            return False, (
                f"no .class file anywhere under {build_dir.name} and no javac "
                f"invocation in the build log, yet the jar carries {in_jar} "
                "classes: they were compiled somewhere other than this build"
            )
        return True, (
            f"{len(classes)} class file(s) in the build tree, "
            f"javac {'named' if saw_javac else 'not named'} in the log"
        )

    def check_config_agreement(self, config: str) -> tuple[bool, str]:
        """Both configurations must publish the same public surface.

        BUILD_SHARED_LIBS has no meaning for a jar, which is exactly why this is
        graded: the two configurations are the same product, so any difference
        between their artifacts is a difference nobody asked for.  A submission
        that wires the switch into its javac flags -- or that only installs the
        module descriptor in one of them -- has invented a distinction State A did
        not have, and a downstream that flips the switch gets a different library.

        The surface is compared rather than the bytes.  Two javac runs over the
        same sources are reproducible in practice, but a jar carries timestamps,
        and grading those would fail on a build that is merely slow.
        """
        del config  # this case is about the pair, not about one member of it
        surfaces: dict[str, list[str]] = {}
        for name in sorted(self.outcomes):
            archive = self.jar(name)
            if archive is None:
                return False, f"[{name}] the artifact is missing or unreadable"
            members: list[str] = []
            for parsed in self.classes(name):
                if not parsed.is_public:
                    continue
                for member in list(parsed.fields) + list(parsed.methods):
                    if member.is_public and not member.is_synthetic:
                        members.append(
                            f"{parsed.binary_name}#{member.name}{member.descriptor}"
                        )
            surfaces[name] = sorted(members)
        names = sorted(surfaces)
        if len(names) < 2:
            return True, "only one configuration was built; nothing to compare"
        first, *rest = names
        for other in rest:
            only_first = sorted(set(surfaces[first]) - set(surfaces[other]))
            only_other = sorted(set(surfaces[other]) - set(surfaces[first]))
            if only_first or only_other:
                return False, (
                    f"{first} and {other} publish different surfaces: "
                    f"{len(only_first)} only in {first} ({only_first[:3]}), "
                    f"{len(only_other)} only in {other} ({only_other[:3]})"
                )
        return True, f"{len(surfaces[first])} public members in every configuration"

    # -- install inventory -----------------------------------------------

    def _installed(self, config: str) -> list[Path]:
        """Every path under the prefix, files and symlinks, sorted."""
        prefix = self.prefix(config)
        if not prefix.is_dir():
            return []
        return sorted(
            p for p in prefix.rglob("*")
            if p.is_file() or p.is_symlink()
        )

    def _forbid_suffixes(
        self, config: str, suffixes: tuple[str, ...], what: str
    ) -> tuple[bool, str]:
        prefix = self.prefix(config)
        offenders = [
            str(p.relative_to(prefix)) for p in self._installed(config)
            if p.name.endswith(suffixes)
        ]
        if offenders:
            return False, f"{what} in the install tree: {offenders[:6]}"
        return True, ""

    def check_inv_jar(self, config: str) -> tuple[bool, str]:
        jar = self.jar_path(config)
        if not jar.exists():
            return False, f"{self.expect.jar_relpath} was not installed"
        if jar.is_symlink():
            return False, (
                f"{self.expect.jar_relpath} is a symlink; the versioned name is "
                "the artifact and the unversioned one is the link"
            )
        if not jar.is_file():
            return False, f"{self.expect.jar_relpath} is not a regular file"
        size = jar.stat().st_size
        if size < 4096:
            return False, (
                f"{self.expect.jar_relpath} is {size} bytes, which is too small to "
                "be a deflate implementation"
            )
        return True, f"{size} bytes"

    def check_inv_jar_link(self, config: str) -> tuple[bool, str]:
        """The unversioned name is what the SONAME link used to be.

        A downstream build that hardcodes a path needs a name that survives the
        next release, and `share/java/zlib.jar` is that name.  It must be a symlink
        rather than a copy: a copy doubles the artifact in every package built from
        this tree, and the two copies drift the moment one is rebuilt.
        """
        entry = next(
            (i for i in self.expect.inventory["common"] if i.get("kind") == "symlink"),
            None,
        )
        if entry is None:
            return True, "the contract declares no stable-name link"
        # Through stable_jar_path rather than composed here, so the absence reaches
        # the ledger.  It returns the same path for the same inventory entry.
        link = self.stable_jar_path(config)
        if not link.exists() and not link.is_symlink():
            return False, f"{entry['path']} was not installed"
        if not link.is_symlink():
            return False, f"{entry['path']} is a regular file, not a symlink"
        target = os.readlink(link)
        wanted = entry["target"]
        if Path(target).name != Path(wanted).name:
            return False, f"{entry['path']} -> {target}, expected {wanted}"
        if not link.resolve().is_file():
            return False, f"{entry['path']} -> {target}, which does not resolve"
        return True, f"-> {target}"

    def check_inv_jar_readable(self, config: str) -> tuple[bool, str]:
        """A JDK tool must be able to open it, not just Python's zipfile.

        Two readers rather than one, because they disagree in a way that matters: a
        zip with a corrupt central directory, or with a self-extracting prologue,
        opens in some readers and not others.  The artifact has to be a jar for the
        JVM, so the JVM's own tool gets the deciding vote.
        """
        jar = self.jar_path(config)
        if not jar.is_file():
            return False, f"{self.expect.jar_relpath} is missing"
        archive = self.jar(config)
        if archive is None:
            return False, "the artifact is not a readable zip"
        listing = vlib.run(
            [java_tool("jar"), "--list", "--file", str(jar)],
            cwd=self.scratch, env=vlib.base_env(), timeout=RUN_TIMEOUT,
            log=self.log, label=f"jar-list-{config}",
        )
        if not listing.ok:
            return False, f"`jar --list` refused the artifact: {listing.tail()}"
        names = [n for n in listing.stdout.decode("utf-8", "replace").split() if n]
        if len(names) < 5:
            return False, f"`jar --list` reported only {len(names)} entries"
        return True, f"{len(names)} entries"

    def _delivery_is_java(self, config: str) -> None:
        """Raise NotApplicable if this tree is the reference C install.

        Six cases in this family assert that the delivery contains no C: no native
        library, no header, no .pc, no man3 page, nothing undeclared, no source.
        They are the only structural cases that fail on a *presence* rather than an
        absence, so jar_path()'s ledger never sees them -- they have no reason to ask
        where the jar is, and would report a C install as six independent findings
        about a submission that does not exist.

        Under grading this raises nothing: a C install cannot occur, because the
        shim refuses to compile the sources that would produce one.  The three
        conditions are in vlib.c_self_test.

        These six stay fully scored while grading, and that is the point of putting
        the guard here rather than at weight 0 in suite.toml: "the release still
        ships libz.so" is one of the two or three most likely ways for a real
        submission to be half-migrated, and it must cost.
        """
        del config
        if self.self_test:
            raise vlib.NotApplicable(
                "this case asserts the delivery contains no C, and the tree under "
                f"test is the reference C install: {self.self_test}")

    def check_inv_no_native(self, config: str) -> tuple[bool, str]:
        """No native library anywhere under the prefix.

        The most direct way to fake this migration is to keep libz.so and load it
        from Java, and this is where the second half of that shows up: the class
        files are graded for a System.loadLibrary reference, and the install tree is
        graded for the thing it would load.  Both halves are needed -- a submission
        can ship the library and load it by absolute path from outside the tree, or
        reference the loader and ship nothing -- and neither half is sufficient.
        """
        self._delivery_is_java(config)
        prefix = self.prefix(config)
        suffixes = (".so", ".a", ".dylib", ".dll", ".jnilib")
        offenders = [
            str(p.relative_to(prefix)) for p in self._installed(config)
            # `libz.so.1.3.1` ends in none of the suffixes, and it is the single
            # most likely file to find here, so the versioned form is matched too.
            if p.name.endswith(suffixes) or re.search(r"\.so(\.\d+)+$", p.name)
        ]
        if offenders:
            return False, f"native libraries in the install tree: {offenders[:6]}"
        return True, ""

    def check_inv_no_headers(self, config: str) -> tuple[bool, str]:
        """No C header: there is no C ABI left to declare.

        zlib.h stays in the *source* tree as the behavioural specification, and
        that distinction is the whole content of this case.  Installing it would
        advertise an ABI a consumer cannot link against -- and a consumer that
        believes the advertisement fails at link time, in someone else's build,
        rather than here.
        """
        self._delivery_is_java(config)
        prefix = self.prefix(config)
        offenders = [
            str(p.relative_to(prefix)) for p in self._installed(config)
            if p.suffix in (".h", ".hpp", ".hh") or p.name in ("zlib.h", "zconf.h")
        ]
        if offenders:
            return False, f"C headers installed: {offenders[:6]}"
        return True, ""

    def check_inv_no_pc(self, config: str) -> tuple[bool, str]:
        """No pkg-config file, whose every field is now a lie.

        zlib.pc declares `-I${includedir}`, `-L${libdir}` and `-lz`.  After the
        port there is no include directory, no library to link and no `-lz`, so a
        .pc file that still resolves is describing something that is not there.
        The jar manifest and module-info answer the same question for the consumers
        who now exist.
        """
        self._delivery_is_java(config)
        return self._forbid_suffixes(config, (".pc",), "pkg-config files")

    def check_inv_no_man(self, config: str) -> tuple[bool, str]:
        """No man3 page: it documents C prototypes."""
        self._delivery_is_java(config)
        prefix = self.prefix(config)
        offenders = [
            str(p.relative_to(prefix)) for p in self._installed(config)
            if "man" in p.parts or re.search(r"\.\d(\.gz)?$", p.name)
        ]
        if offenders:
            return False, f"manual pages installed: {offenders[:6]}"
        return True, ""

    def check_inv_no_loose_classes(self, config: str) -> tuple[bool, str]:
        """Class files belong inside the jar and nowhere else.

        A loose class tree beside the artifact is a second copy of the
        implementation, and the two copies answer differently the moment one is
        rebuilt -- with which one wins decided by whatever the consumer happens to
        put first on its path.
        """
        return self._forbid_suffixes(config, (".class",), "loose class files")

    def check_inv_no_extra(self, config: str) -> tuple[bool, str]:
        """Nothing in the tree that the inventory does not name.

        Upstream's install rules are explicit about every file, and after the port
        the whole release is two entries, so there is no quirk to accommodate: an
        extra file is either a second implementation, a leftover, or something the
        contract should have declared.
        """
        self._delivery_is_java(config)
        prefix = self.prefix(config)
        declared = {item["path"] for item in self.expect.inventory["common"]}
        extra = [
            rel for p in self._installed(config)
            if (rel := str(p.relative_to(prefix))) not in declared
            and rel not in INSTALL_ALLOWED_EXTRA
        ]
        if extra:
            return False, f"{len(extra)} undeclared file(s): {sorted(extra)[:8]}"
        return True, f"exactly the {len(declared)} declared entries"

    def check_inv_no_source_leak(self, config: str) -> tuple[bool, str]:
        """No source, object or intermediate escaped into the release."""
        self._delivery_is_java(config)
        return self._forbid_suffixes(
            config, SOURCE_LEAK_SUFFIXES, "sources or intermediates"
        )

    def check_inv_prefix_contained(self, config: str) -> tuple[bool, str]:
        """Every installed path, and every link target, stays under the prefix.

        A symlink out of the tree is the failure this catches, and it is easy to
        write by accident: `file(CREATE_LINK)` with an absolute target produces a
        link that works in the build environment and dangles in the package.
        """
        prefix = self.prefix(config).resolve()
        escaped: list[str] = []
        for path in self._installed(config):
            try:
                resolved = path.resolve()
            except OSError as exc:
                escaped.append(f"{path.name}: {exc}")
                continue
            if prefix not in resolved.parents and resolved != prefix:
                escaped.append(f"{path.name} -> {resolved}")
        if escaped:
            return False, f"paths escaping the prefix: {escaped[:5]}"
        return True, ""

    def check_inv_manifest(self, config: str) -> tuple[bool, str]:
        """install_manifest.txt must list what was installed.

        CMake writes it for every `install(FILES)` and `install(TARGETS)` rule, so
        a tree whose manifest is missing an installed *file* was populated by
        something other than an install rule -- a POST_BUILD copy, usually -- and
        `cmake --install` is then not the way to package it.

        Only the file entries are required, and the reason is CMake's rather than
        the submission's.  Upstream's stable name was a library symlink created by
        `install(TARGETS)`, which records it; a jar's stable name has no target to
        hang off, so the idiom is `install(CODE)` around `create_symlink`, and
        nothing CMake does inside an `install(CODE)` block reaches the manifest.
        Demanding the link appear there would be demanding the one obscure trick
        that puts it there -- appending to `CMAKE_INSTALL_MANIFEST_FILES` from
        inside the block -- which is CMake trivia and not migration work.  The link
        itself is graded, as a link, by the inventory cases.
        """
        manifest = self.outcome(config).build_dir / "install_manifest.txt"
        if not manifest.is_file():
            return False, "install_manifest.txt was not written"
        listed = {
            Path(line.strip()) for line in manifest.read_text(errors="replace").splitlines()
            if line.strip()
        }
        if not listed:
            return False, "install_manifest.txt is empty"
        missing: list[str] = []
        wanted = 0
        for item in self.expect.inventory["common"]:
            if item.get("kind") != "file":
                continue
            wanted += 1
            target = self.prefix(config) / item["path"]
            if target not in listed and not any(p.name == target.name for p in listed):
                missing.append(item["path"])
        if missing:
            # A manifest cannot list a file no install rule ever produced.  This reads
            # the build directory's manifest rather than the prefix, so jar_path never
            # sees it -- but if the file is absent from the install too, the finding is
            # the absence and not a missing install rule.
            for item in missing:
                if not (self.prefix(config) / item).exists():
                    self.note_absent(config, f"{item} is neither installed nor listed "
                                             f"in install_manifest.txt")
            return False, f"the manifest does not list {missing}"
        return True, f"{len(listed)} entries, {wanted} required file(s) among them"

    # -- the jar as a container ------------------------------------------

    def check_jar_entries(self, config: str) -> tuple[bool, str]:
        """Every entry the contract names is present, and the manifest is first.

        The manifest's position is not pedantry.  `jar` writes it first and the
        format's own readers -- including every consumer that reads a manifest by
        streaming the archive rather than seeking the central directory -- assume
        it.  A jar assembled by a hand-rolled zip writer usually gets this wrong,
        and the failure surfaces as a manifest that appears empty.
        """
        archive = self.jar(config)
        if archive is None:
            return False, "the artifact is missing or unreadable"
        names = archive.names()
        present = set(names)
        missing = [
            item["path"] for item in self.expect.jar_contract["required_entries"]
            if item["path"] not in present
        ]
        if missing:
            return False, f"{len(missing)} required entr(ies) absent: {missing[:6]}"
        for item in self.expect.jar_contract["required_entries"]:
            if item.get("position") != "first":
                continue
            real = [n for n in names if not n.endswith("/")]
            if not real or real[0] != item["path"]:
                return False, (
                    f"{item['path']} must be the first entry; the archive starts "
                    f"with {real[:2]}"
                )
        required = len(self.expect.jar_contract["required_entries"])
        return True, f"{len(present)} entries, all {required} required ones present"

    def check_jar_forbidden(self, config: str) -> tuple[bool, str]:
        """No sources, native libraries, nested jars, signatures or versioned dirs.

        META-INF/versions is forbidden rather than merely unusual: a multi-release
        jar ships different bytecode per JDK, and only one of those bytecodes would
        ever be graded.  A submission that put a java.util.zip delegation in the
        JDK-21 tree and a real implementation in the base tree would pass every
        class-file gate on a 17 image and be a different library everywhere else.
        """
        archive = self.jar(config)
        if archive is None:
            return False, "the artifact is missing or unreadable"
        patterns = self.expect.jar_contract["forbidden_entries"]["patterns"]
        offenders: list[str] = []
        for name in archive.names():
            for pattern in patterns:
                # fnmatch's `*` crosses `/`, so `**/*.java` and `*.java` behave the
                # same here; both spellings are kept because the contract is read
                # by people too, and `**/` is how a reader expects "at any depth".
                if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(
                    name, pattern.replace("**/", "")
                ):
                    offenders.append(f"{name} ({pattern})")
                    break
        if offenders:
            return False, f"{len(offenders)} forbidden entr(ies): {offenders[:6]}"
        return True, ""

    def check_jar_packages(self, config: str) -> tuple[bool, str]:
        """Every class lives under a permitted package prefix.

        This is where a shaded dependency shows up.  The usual way to make a Java
        port of a C library work quickly is to vendor something that already
        implements the format and relocate its packages into your own namespace;
        the relocation makes the class names look local, but the package tree does
        not, and neither does the class count.
        """
        archive = self.jar(config)
        if archive is None:
            return False, "the artifact is missing or unreadable"
        allowed = tuple(self.expect.jar_contract["packages_allowed"])
        stray = [
            name for name in archive.class_entry_names()
            if name != classfile.MODULE_INFO_ENTRY
            and classfile.matches_prefix(name, allowed) is None
        ]
        if stray:
            packages = sorted({n.rsplit("/", 1)[0] for n in stray})
            return False, (
                f"{len(stray)} class(es) outside {allowed}: packages {packages[:5]}"
            )
        return True, f"{len(archive.packages())} package(s)"

    def check_jar_manifest(self, config: str) -> tuple[bool, str]:
        """The manifest carries the pinned attributes, and none of the barred ones.

        Only the attributes the contract names are examined.  Anything else is left
        alone because the `jar` tool writes its own -- Created-By records the JDK
        that assembled the archive -- and a submission cannot suppress those without
        hand-rolling the manifest, which is not an improvement.

        Attributes mapped to null in the contract must be absent rather than empty.
        Automatic-Module-Name is the one worth naming: it is how a *non*-modular jar
        asks to be treated as a module, and a jar that carries both it and a real
        module-info is telling two stories about its own identity -- which the
        module system resolves by ignoring the one the author probably meant.
        """
        archive = self.jar(config)
        if archive is None:
            return False, "the artifact is missing or unreadable"
        manifest = archive.manifest()
        if not manifest:
            return False, "META-INF/MANIFEST.MF carries no attributes"
        problems: list[str] = []
        for key, wanted in sorted(self.expect.jar_contract["manifest_attributes"].items()):
            got = manifest.get(key)
            if wanted is None:
                if got is not None:
                    problems.append(f"{key} must be absent, found {got!r}")
            elif got is None:
                problems.append(f"{key} is missing (want {wanted!r})")
            elif got != wanted:
                problems.append(f"{key}={got!r}, want {wanted!r}")
        if problems:
            return False, "; ".join(problems[:5])
        return True, f"{len(manifest)} attributes"

    def check_jar_no_main(self, config: str) -> tuple[bool, str]:
        """A library declares no entry point."""
        archive = self.jar(config)
        if archive is None:
            return False, "the artifact is missing or unreadable"
        main = archive.manifest().get("Main-Class")
        if main:
            return False, f"Main-Class: {main}"
        return True, ""

    def check_jar_unsigned(self, config: str) -> tuple[bool, str]:
        """No signature block.

        A signed jar is pinned to one signer's key, and a rebuild by anyone else
        produces an artifact whose signature no longer verifies -- so a
        distribution cannot rebuild it, which is the opposite of what shipping
        source is for.  It is also a way to make the archive tamper-evident against
        the verifier, which is not a property this task wants.
        """
        archive = self.jar(config)
        if archive is None:
            return False, "the artifact is missing or unreadable"
        signatures = archive.signature_entries()
        if signatures:
            return False, f"signature entries present: {signatures[:4]}"
        return True, ""

    def check_jar_compression(self, config: str) -> tuple[bool, str]:
        """Entries use a documented compression method.

        There is a pleasing circularity here: a jar is a zip, its entries are
        compressed with deflate, and the artifact under test is a deflate
        implementation.  The jar is built by the JDK's own zip writer rather than by
        the library it contains, so this is not self-reference -- but an entry
        stored with method 12 or 14 is a jar the JVM cannot read, whoever wrote it.
        """
        archive = self.jar(config)
        if archive is None:
            return False, "the artifact is missing or unreadable"
        allowed = {
            "stored": 0,
            "deflated": 8,
        }
        permitted = {
            allowed[name] for name in self.expect.jar_contract["compression_methods_allowed"]
            if name in allowed
        }
        offenders = [
            f"{e.name} (method {e.compress_type})" for e in archive.entries()
            if not e.is_dir and e.compress_type not in permitted
        ]
        if offenders:
            return False, f"{len(offenders)} entr(ies) oddly compressed: {offenders[:5]}"
        return True, ""

    def check_jar_no_dup(self, config: str) -> tuple[bool, str]:
        """No entry name appears twice, and no name escapes extraction.

        Which of two same-named entries a JVM loads is not specified, so a jar with
        duplicates is a jar whose contents depend on the reader.  Absolute and
        `..`-bearing names are checked in the same pass because they are the same
        class of defect: an archive whose entry names mean something other than
        what they appear to mean.
        """
        archive = self.jar(config)
        if archive is None:
            return False, "the artifact is missing or unreadable"
        duplicates = archive.duplicate_names()
        unsafe = archive.unsafe_names()
        if duplicates:
            return False, f"{len(duplicates)} duplicated entr(ies): {duplicates[:5]}"
        if unsafe:
            return False, f"unsafe entry name(s): {unsafe[:5]}"
        return True, ""

    # -- the module descriptor -------------------------------------------

    def module(self, config: str) -> classfile.ModuleDescriptor | None:
        archive = self.jar(config)
        if archive is None:
            return None
        try:
            return archive.module_descriptor()
        except classfile.ClassFileError as exc:
            self.log.write(f"module-info unparseable ({config}): {exc}")
            return None

    def check_mod_present(self, config: str) -> tuple[bool, str]:
        """module-info.class at the jar root, and it must parse.

        The location is part of the requirement.  A descriptor under
        META-INF/versions is a multi-release descriptor, which means the module's
        own identity would depend on which JDK read it.
        """
        archive = self.jar(config)
        if archive is None:
            return False, "the artifact is missing or unreadable"
        wanted = self.expect.module_contract["descriptor_path"]
        if not archive.has(wanted):
            versioned = [
                n for n in archive.names() if n.endswith("module-info.class")
            ]
            if versioned:
                return False, (
                    f"no {wanted} at the jar root; found it at {versioned[:3]} "
                    "instead, which makes the module descriptor JDK-dependent"
                )
            return False, f"{wanted} is absent: the jar is not a named module"
        descriptor = self.module(config)
        if descriptor is None:
            return False, f"{wanted} is present but does not parse"
        return True, f"module {descriptor.name}"

    def check_mod_name(self, config: str) -> tuple[bool, str]:
        descriptor = self.module(config)
        if descriptor is None:
            return False, "no parseable module descriptor"
        wanted = self.expect.module_name
        if descriptor.name != wanted:
            return False, f"module is named {descriptor.name!r}, want {wanted!r}"
        return True, ""

    def check_mod_exports(self, config: str) -> tuple[bool, str]:
        """Exactly the contract's packages, exported unqualified.

        A qualified export -- `exports org.zlib to some.friend` -- is not a
        published API: it compiles for the named module and for nobody else.  It is
        counted as a failure to export rather than as an export, because that is
        what a consumer experiences.
        """
        descriptor = self.module(config)
        if descriptor is None:
            return False, "no parseable module descriptor"
        wanted = set(self.expect.module_contract["exports"])
        unqualified = descriptor.exported_packages()
        missing = sorted(wanted - unqualified)
        if missing:
            qualified = {
                e.package: e.to for e in descriptor.exports if e.is_qualified
            }
            hint = f"; exported only to {qualified}" if qualified else ""
            return False, f"not exported: {missing}{hint}"
        return True, f"exports {sorted(unqualified)}"

    def check_mod_no_extra_exports(self, config: str) -> tuple[bool, str]:
        """Nothing beyond the contract is exported, however convenient.

        The implementation package is the reason this case exists.  A port of this
        size wants one, the contract permits it, and the temptation is to export it
        so the drivers can reach the internals -- at which point it is public API
        and every consumer can depend on it.  The drivers are supposed to be
        consumers, and this is what keeps them honest.
        """
        descriptor = self.module(config)
        if descriptor is None:
            return False, "no parseable module descriptor"
        wanted = set(self.expect.module_contract["exports"])
        extra = sorted(descriptor.exported_packages(include_qualified=True) - wanted)
        if extra:
            return False, f"{len(extra)} package(s) exported beyond the contract: {extra}"
        return True, ""

    def check_mod_requires(self, config: str) -> tuple[bool, str]:
        """java.base and nothing else.

        This is zlib.map's other half.  The version script said what left the
        library; a module descriptor says that and what the library needs, which the
        C form could only ask of DT_NEEDED after the fact.  Here it is a declaration
        the author had to write, so a dependency cannot arrive by accident.
        """
        descriptor = self.module(config)
        if descriptor is None:
            return False, "no parseable module descriptor"
        allowed = set(self.expect.module_contract["requires"])
        # java.base is implicit and javac records it whether or not it was written.
        allowed.add("java.base")
        extra = sorted(descriptor.required_modules() - allowed)
        if extra:
            return False, f"requires beyond {sorted(allowed)}: {extra}"
        if "java.base" not in descriptor.required_modules():
            return False, "the descriptor does not require java.base at all"
        return True, f"requires {sorted(descriptor.required_modules())}"

    def check_mod_forbidden_requires(self, config: str) -> tuple[bool, str]:
        """No requires on a module that would supply another implementation.

        Overlaps check_mod_requires deliberately and is scored separately, because
        the two answer different questions.  That one asks whether the dependency
        list is minimal; this one asks whether a specific named door is open.
        jdk.unsupported is the interesting entry: it is how a module legitimately
        reaches sun.misc.Unsafe, and Unsafe is how a port would reach memory the way
        the C did.
        """
        descriptor = self.module(config)
        if descriptor is None:
            return False, "no parseable module descriptor"
        forbidden = set(self.expect.module_contract["forbidden_requires"])
        found = sorted(descriptor.required_modules() & forbidden)
        if found:
            return False, f"requires forbidden module(s): {found}"
        return True, ""

    def check_mod_not_open(self, config: str) -> tuple[bool, str]:
        """The module is not open and opens nothing.

        An open module grants deep reflective access to every package it contains,
        including the implementation package -- which makes the encapsulation the
        descriptor exists to declare unenforceable.  It would also let the
        verifier's own reflection reach further than a consumer can, so the API
        cases would be grading a surface no downstream has.
        """
        descriptor = self.module(config)
        if descriptor is None:
            return False, "no parseable module descriptor"
        if descriptor.is_open:
            return False, "the module is declared `open`"
        if descriptor.opens:
            packages = sorted(e.package for e in descriptor.opens)
            return False, f"the module opens {packages}"
        return True, ""

    def check_mod_describe(self, config: str) -> tuple[bool, str]:
        """`jar --describe-module` must agree with the parsed descriptor.

        A cross-check on the reader, not on the submission.  The parser here was
        written for this task; the jar tool has been reading these files since JDK
        9.  If the two disagree about the module's name or its exports, the finding
        is most likely in the parser, and it should surface as its own case rather
        than as a mysterious failure in the ones that depend on it.
        """
        descriptor = self.module(config)
        if descriptor is None:
            return False, "no parseable module descriptor"
        result = self.describe_module(config)
        if not result.ok:
            return False, f"`jar --describe-module` failed: {result.tail()}"
        text = result.stdout.decode("utf-8", "replace")
        first = text.strip().splitlines()[0] if text.strip() else ""
        if descriptor.name not in first:
            return False, (
                f"the tool reports {first!r}, the parser reports "
                f"{descriptor.name!r}"
            )
        told = {
            line.split()[1] for line in text.splitlines()
            if line.strip().startswith("exports ") and len(line.split()) > 1
        }
        parsed = descriptor.exported_packages(include_qualified=True)
        if told != parsed:
            return False, (
                f"exports disagree: tool says {sorted(told)}, parser says "
                f"{sorted(parsed)}"
            )
        return True, ""

    def check_mod_validate(self, config: str) -> tuple[bool, str]:
        """The module must resolve in a real module graph.

        Parsing the descriptor is not the same as resolving it.  A descriptor can
        name a package it does not contain, or split a package with another module,
        and both parse perfectly and fail at resolution -- so the JVM's own resolver
        gets asked.  `--validate-modules` walks the path and reports what it finds
        wrong; `--list-modules` proves the module is actually observable, since a
        validate that silently found nothing to validate would pass.
        """
        jar = self.jar_path(config)
        if not jar.is_file():
            return False, f"{self.expect.jar_relpath} is missing"
        java = java_tool("java")
        validate = vlib.run(
            [java, "-p", str(jar), "--validate-modules"],
            cwd=self.scratch, env=vlib.base_env(), timeout=RUN_TIMEOUT,
            log=self.log, label=f"validate-modules-{config}",
        )
        if not validate.ok:
            return False, f"--validate-modules rejected the jar: {validate.tail()}"
        listing = vlib.run(
            [java, "-p", str(jar), "--list-modules"],
            cwd=self.scratch, env=vlib.base_env(), timeout=RUN_TIMEOUT,
            log=self.log, label=f"list-modules-{config}",
        )
        if not listing.ok:
            return False, f"--list-modules failed: {listing.tail()}"
        text = listing.stdout.decode("utf-8", "replace")
        wanted = self.expect.module_name
        if not re.search(rf"^{re.escape(wanted)}(@|\s|$)", text, re.M):
            return False, f"{wanted} does not appear in the resolved module graph"
        return True, ""

    # -- the class files, by parsing the bytes ---------------------------
    #
    # Everything in this group reads the delivered bytes directly, and that is the
    # point: the api group asks a JVM what it sees, and a JVM only ever answers
    # about code paths it took.  A constant-pool entry for java/util/zip/Deflater
    # is in the artifact whether or not the method holding it is ever called, so
    # the only way to find a cheat that hides behind a branch is to stop running
    # the code and read it.

    def check_cf_major(self, config: str) -> tuple[bool, str]:
        """Every class targets the pinned bytecode version, exactly.

        61 is Java 17, which is the JDK in the image.  Exact rather than "at most"
        because both directions are findings.  Higher cannot be produced by the
        image's javac at all, so it means the classes came from somewhere else.
        Lower is the interesting one: it is what `--release 11` produces, and also
        what shipping a prebuilt jar from an older toolchain produces.  Neither is
        the build this task asks for.

        The minor version is checked too.  It is 0 for everything except preview
        features, where javac writes 65535 -- and a class using preview features
        loads on no JVM but the exact build that compiled it.
        """
        wanted = self.expect.major_version
        minor_wanted = int(self.expect.classfile_contract.get("minor_version", 0))
        classes = self.classes(config)
        if not classes:
            return False, "no class files could be read from the jar"
        # module-info.class is checked below but classes() excludes it, so the
        # denominator is counted separately from that list.  It read "12 of 11"
        # before, which is the kind of detail that makes a reader distrust the
        # finding rather than the artifact.
        examined = len(classes)
        wrong: list[str] = []
        for parsed in classes:
            if parsed.major_version != wanted:
                wrong.append(f"{parsed.binary_name}: major {parsed.major_version}")
            elif parsed.minor_version != minor_wanted:
                wrong.append(f"{parsed.binary_name}: minor {parsed.minor_version}")
        # module-info.class is excluded from classes() but carries a version too,
        # and a descriptor compiled at a different level than the classes beside it
        # is the same finding.
        archive = self.jar(config)
        if archive is not None:
            path = self.expect.module_contract["descriptor_path"]
            if archive.has(path):
                examined += 1
                try:
                    descriptor = classfile.ClassFile(archive.read(path), path)
                    if descriptor.major_version != wanted:
                        wrong.append(f"{path}: major {descriptor.major_version}")
                except classfile.ClassFileError as exc:
                    wrong.append(f"{path}: unparseable ({exc})")
        if wrong:
            return False, (
                f"{len(wrong)} of {examined} classes are not major {wanted}"
                f".{minor_wanted}: {wrong[:5]}"
            )
        return True, f"{examined} classes at major {wanted}"

    def check_cf_parse(self, config: str) -> tuple[bool, str]:
        """Every entry named .class parses as a class file, start to end.

        Not a formality.  The gates in this group work by reading the constant
        pool, so a class the parser cannot reach is a class nothing in the group
        inspected -- an unparseable entry would silently exempt itself from the
        forbidden-reference scan.  This case is what stops that from being a way
        through: an entry that will not parse fails here whatever else it does.

        The whole entry has to parse, not just the header, because a truncated file
        still has a valid magic number and version.  And each class must declare
        the name its path gives it: a mismatch is either a relocated class or a
        hand-edited one, and while the JVM would refuse it at load time, it would
        not say why.
        """
        archive = self.jar(config)
        if archive is None:
            return False, "the jar could not be opened"
        names = archive.class_entry_names()
        if not names:
            return False, "the jar contains no class files"
        broken: list[str] = []
        total_pool = 0
        for name in names:
            try:
                parsed = classfile.ClassFile(archive.read(name), name)
                total_pool += len(parsed.pool)
                expected = name[: -len(".class")]
                if parsed.this_class != expected:
                    broken.append(f"{name} declares itself {parsed.this_class}")
            except (classfile.ClassFileError, KeyError, OSError) as exc:
                broken.append(f"{name}: {type(exc).__name__}: {exc}")
        if broken:
            return False, f"{len(broken)} of {len(names)} entries: {broken[:4]}"
        return True, f"{len(names)} class files, {total_pool} pool entries"

    def check_cf_no_native(self, config: str) -> tuple[bool, str]:
        """No method carries ACC_NATIVE: the C did not survive behind JNI.

        The most direct way to fail this task while passing every behavioral case.
        Keep the C, compile it into a .so, declare `private static native int
        deflate(...)`, and the jar is a thin wrapper over the implementation the
        task asked to replace.  Every byte comes out identical because it is the
        same code producing them.

        Read from the bytes rather than by reflection: a native method on a class
        that never loads is still in the artifact, and every method of every class
        is checked rather than the public surface only -- a cheat would put it on a
        package-private helper, not on the API.
        """
        classes = self.classes(config)
        if not classes:
            return False, "no class files could be read from the jar"
        found: list[str] = []
        for parsed in classes:
            for method in parsed.native_methods():
                found.append(f"{parsed.binary_name}.{method}")
        if found:
            return False, (
                f"{len(found)} native method(s), so machine code is still doing the "
                f"work: {found[:6]}"
            )
        return True, f"{len(classes)} classes, no native methods"

    def check_cf_no_forbidden_types(self, config: str) -> tuple[bool, str]:
        """No class references java.util.zip, sun.*, Unsafe, or the FFM linker.

        The central anti-cheat gate of the task, and the reason it reads the
        constant pool: every type a class touches is named there, whether the
        touching code runs or not.  No arrangement of branches, exception handlers
        or lazy initialization removes the entry.

        Three groups, for three different cheats.  java.util.zip is the JDK's own
        binding to the real libz -- calling Deflater is not a port, it is a
        redirect, and it reproduces every byte because it is the same compressor.
        Runtime and ProcessBuilder are how a submission shells out to `gzip`.
        Unsafe and java.lang.foreign are how it reaches the C anyway: FFM can call
        into a .so with no native method declared anywhere, so cf-no-native would
        not see it.

        Reported per class and per reference, because "somewhere in the jar" is not
        a finding anybody can act on.
        """
        prefixes = tuple(self.expect.classfile_contract["forbidden_prefixes"])
        exact = set(self.expect.classfile_contract["forbidden_type_references"])
        classes = self.classes(config)
        if not classes:
            return False, "no class files could be read from the jar"
        found: list[str] = []
        for parsed in classes:
            for reference in sorted(parsed.referenced_types()):
                hit = classfile.matches_prefix(reference, prefixes)
                if hit is None and reference in exact:
                    hit = reference
                if hit is not None:
                    found.append(f"{parsed.binary_name} -> {reference}")
        if found:
            return False, f"{len(found)} forbidden type reference(s): {found[:6]}"
        return True, f"{len(classes)} classes clean"

    def check_cf_no_forbidden_strings(self, config: str) -> tuple[bool, str]:
        """No class holds a forbidden class name as a string literal.

        The reflective form of the previous cheat.  `Class.forName("java.util.zip"
        + ".Deflater")` puts no type reference in the pool -- the type scan sees
        only java/lang/Class and java/lang/reflect/Method, which every honest
        program uses.  What it does put there is the name, as a string.

        Runtime concatenation would defeat this, and that is worth being honest
        about: a determined cheat can assemble the name from characters.  What the
        gate does is make the cheap version fail, so that the expensive version is
        no longer cheaper than doing the port.  The runtime half of the same
        question -- whether the delivered code ever loads a zip class at all --
        belongs to audit.py's no-jdk-deflate gate, which watches the JVM
        instead of reading it.
        """
        forbidden = set(self.expect.classfile_contract["forbidden_string_constants"])
        classes = self.classes(config)
        if not classes:
            return False, "no class files could be read from the jar"
        found: list[str] = []
        for parsed in classes:
            for literal in sorted(parsed.string_literals()):
                if literal in forbidden:
                    found.append(f"{parsed.binary_name}: {literal!r}")
                    continue
                # A literal that merely contains a forbidden name counts too: the
                # same name reached by substring, or as a resource path, is the
                # same intent.
                if any(name in literal for name in forbidden):
                    found.append(f"{parsed.binary_name}: {literal!r}")
        if found:
            return False, f"{len(found)} forbidden string literal(s): {found[:6]}"
        return True, f"{len(classes)} classes clean"

    def check_cf_no_process(self, config: str) -> tuple[bool, str]:
        """Nothing in the jar can start a process.

        A library that shells out to /usr/bin/gzip passes every gzip byte-exactness
        case in the suite, because gzip produces those bytes.  It is also not a
        library: it carries a runtime dependency the install inventory does not
        declare, it fails in any environment without the binary, and it has ported
        nothing.

        Overlaps the type scan deliberately -- Runtime and ProcessBuilder are in
        forbidden_type_references too -- because the two find it by different
        evidence, and a submission that evades one should still meet the other.
        This one also looks for the method names and for the paths a shell-out
        needs, which the type scan cannot see when the call goes through an
        interface.
        """
        classes = self.classes(config)
        if not classes:
            return False, "no class files could be read from the jar"
        # calls() reports owners dotted, referenced_types() reports them slashed.
        # The two forms are not interchangeable and mixing them fails open, so each
        # set here is written in the form of the accessor it is compared against.
        owners = {
            "java.lang.Runtime", "java.lang.ProcessBuilder", "java.lang.Process",
            "java.lang.ProcessHandle",
        }
        paths = {
            "/bin/sh", "/bin/bash", "/usr/bin/gzip", "/bin/gzip", "gzip",
            "zlib1g", "libz.so",
        }
        found: list[str] = []
        for parsed in classes:
            for owner, name, _descriptor in parsed.calls():
                if owner in owners:
                    found.append(f"{parsed.binary_name} calls {owner}.{name}")
            for literal in sorted(parsed.string_literals()):
                if literal in paths:
                    found.append(f"{parsed.binary_name}: {literal!r}")
        if found:
            return False, f"{len(found)} finding(s): {found[:6]}"
        return True, f"{len(classes)} classes clean"

    def check_cf_no_load(self, config: str) -> tuple[bool, str]:
        """Nothing in the jar loads a native library.

        The third way the C survives, and the one the other two cases miss.
        System.loadLibrary("z") declares no native method and references no
        forbidden type -- java/lang/System is in every class file ever written --
        so it is invisible to cf-no-native and to the type scan.  What it does is
        bind the real libz into the process, after which java.lang.foreign, or a
        native method on some other class file, can call it.

        The argument is reported when it is a literal, because "you load something"
        and "you load the library this task exists to replace" are different
        findings and the name is what separates them.
        """
        classes = self.classes(config)
        if not classes:
            return False, "no class files could be read from the jar"
        loaders = {"loadLibrary", "load", "mapLibraryName", "findLibrary"}
        # Dotted, because calls() is dotted.
        owners = {"java.lang.System", "java.lang.Runtime", "java.lang.ClassLoader"}
        names = {"z", "libz", "libz.so", "zlib", "zlib1"}
        found: list[str] = []
        for parsed in classes:
            hits = [
                f"{owner}.{name}"
                for owner, name, _d in parsed.calls()
                if name in loaders and owner in owners
            ]
            if hits:
                literals = sorted(parsed.string_literals() & names)
                detail = f" (name={literals})" if literals else ""
                found.append(f"{parsed.binary_name}: {hits}{detail}")
        if found:
            return False, f"{len(found)} native-library load(s): {found[:5]}"
        return True, f"{len(classes)} classes clean"

    def check_cf_deps(self, config: str) -> tuple[bool, str]:
        """Nothing is referenced that the module could not legally require.

        The class files and module-info.class are two accounts of the same
        dependency set, written by different parts of the build.  A class that
        reaches outside java.* while the descriptor requires only java.base is a
        descriptor that is wrong -- and wrong in the direction that fails at run
        time on a module path rather than at build time, so nothing else in the
        suite finds it before a consumer does.

        The comparison is by package rather than by module, and deliberately does
        not enumerate java.base's exports.  Mapping every JDK package to its module
        would need `java --list-modules` plus each descriptor, and the list would
        rot with every release; meanwhile the dangerous parts of the JDK are
        already named by the forbidden scan.  So what is checked here is the shape:
        nothing outside java.* and the module's own packages, since anything else
        is a dependency on a third-party jar that the artifact does not carry and
        the descriptor cannot name.
        """
        descriptor = self.module(config)
        classes = self.classes(config)
        if not classes:
            return False, "no class files could be read from the jar"
        # referenced_packages() is dotted; packages_allowed is internal form with a
        # trailing slash, which is what matches_prefix expects.  So the package is
        # converted to that form rather than the prefixes to this one -- the
        # matcher's contract is the internal spelling, and Expectations.load
        # asserts it against exactly that.
        allowed_own = tuple(self.expect.jar_contract["packages_allowed"])
        stray: list[str] = []
        for parsed in classes:
            for package in sorted(parsed.referenced_packages()):
                if not package or package.startswith("java."):
                    continue
                internal = package.replace(".", "/") + "/x"
                if classfile.matches_prefix(internal, allowed_own) is not None:
                    continue
                stray.append(f"{parsed.binary_name} -> {package}")
        if stray:
            return False, (
                f"{len(stray)} reference(s) outside java.* and the module's own "
                f"packages, which no `requires` can satisfy: {stray[:6]}"
            )
        if descriptor is not None:
            allowed = set(self.expect.module_contract["requires"]) | {"java.base"}
            extra = descriptor.required_modules() - allowed
            if extra:
                return False, (
                    f"the descriptor requires {sorted(extra)}, which the contract "
                    "does not permit"
                )
        return True, f"{len(classes)} classes, no undeclarable dependency"

    # -- linkage: can anything actually reach the artifact ---------------
    #
    # A failure in this group is the verifier telling itself the jar is unusable,
    # not a behavioral finding.  It is graded because "unusable" is a migration
    # outcome a submission can reach while every class file it ships is perfect: a
    # descriptor that exports nothing, a jar whose classes sit under a directory
    # the module system will not read, a package name that disagrees with the path.

    def _compile_and_run(
        self, config: str, name: str, source: str, mode: str,
    ) -> tuple[bool, str, str]:
        """Compile one throwaway Java program against the jar and run it.

        Returns (ok, detail, stdout).  Used for the consumer; the probe has its own
        path because it is the graded instrument and its sources live on disk.
        """
        jar = self.jar_path(config)
        if not jar.is_file():
            return False, f"{self.expect.jar_relpath} is missing", ""
        work = self.scratch / f"{name}-{config}-{mode}"
        shutil.rmtree(work, ignore_errors=True)
        (work / "src").mkdir(parents=True)
        source_path = work / "src" / f"{name}.java"
        source_path.write_text(source, encoding="utf-8")
        classes = work / "classes"
        compiled = java_compile(
            [source_path], classes, self.log, label=f"{name}-javac-{config}-{mode}",
            mode=mode, jar=jar, module_name=self.expect.module_name,
        )
        if not compiled.ok:
            return False, f"[{mode}] did not compile: {compiled.tail()}", ""
        argv = java_command(
            name, mode=mode, jar=jar, class_dir=classes,
            module_name=self.expect.module_name,
        )
        result = vlib.run(
            argv, cwd=work, env=vlib.base_env(), timeout=RUN_TIMEOUT,
            log=self.log, label=f"{name}-run-{config}-{mode}",
        )
        text = result.stdout.decode("utf-8", "replace")
        if not result.ok:
            return False, f"[{mode}] exited {result.returncode}: {result.tail()}", text
        return True, "", text

    def _probe_linkage(self, config: str, mode: str) -> tuple[bool, str]:
        """Build and smoke-run the differential probe in one linkage mode.

        Cached, because both cases in this group and every probe case downstream
        would otherwise recompile the same program.  The smoke run asks the probe
        for its own self-description rather than for a case: a probe that reports
        its key list is a probe that loaded the library and resolved every entry
        point it knows about, which is the whole question here.
        """
        key = (config, mode)
        if key in self._probe_cache:
            return self._probe_cache[key]
        jar = self.jar_path(config)
        if not jar.is_file():
            answer = (False, f"{self.expect.jar_relpath} is missing")
            self._probe_cache[key] = answer
            return answer
        work = self.scratch / f"probe-{config}-{mode}"
        shutil.rmtree(work, ignore_errors=True)
        classes = work / "classes"
        sources = self._java_sources(self.probe_src)
        if not sources:
            answer = (False, f"no probe sources under {self.probe_src}")
            self._probe_cache[key] = answer
            return answer
        compiled = java_compile(
            sources, classes, self.log, label=f"probe-javac-{config}-{mode}",
            mode=mode, jar=jar, module_name=self.expect.module_name,
        )
        if not compiled.ok:
            answer = (False, f"[{mode}] Probe.java did not compile: {compiled.tail()}")
            self._probe_cache[key] = answer
            return answer
        argv = java_command(
            "Probe", mode=mode, jar=jar, class_dir=classes,
            module_name=self.expect.module_name,
        ) + ["--list-keys"]
        result = vlib.run(
            argv, cwd=work, env=vlib.base_env(), timeout=RUN_TIMEOUT,
            log=self.log, label=f"probe-keys-{config}-{mode}",
        )
        if not result.ok:
            answer = (
                False,
                f"[{mode}] the probe compiled but would not run: {result.tail()}",
            )
            self._probe_cache[key] = answer
            return answer
        keys = [
            line for line in result.stdout.decode("utf-8", "replace").splitlines()
            if line.strip()
        ]
        if not keys:
            answer = (False, f"[{mode}] the probe reported no keys")
            self._probe_cache[key] = answer
            return answer
        answer = (True, f"[{mode}] {len(keys)} probe keys resolved")
        self._probe_cache[key] = answer
        return answer

    def check_probe_modulepath(self, config: str) -> tuple[bool, str]:
        """The probe compiles and runs with the jar as a named module.

        The stricter of the two arrangements, and the one the contract describes.
        On the module path the descriptor decides everything: a package the
        descriptor does not export is invisible to the probe no matter that the
        class file is right there in the archive, and a descriptor that names a
        package the jar does not carry makes the whole module unresolvable.  So
        this case failing while probe-classpath passes localizes the defect
        precisely to module-info.class.
        """
        return self._probe_linkage(config, MODE_MODULE)

    def check_probe_classpath(self, config: str) -> tuple[bool, str]:
        """The same probe runs with the jar on the class path.

        Graded separately because the two modes fail apart, which was confirmed
        rather than assumed: a jar with a correct descriptor and classes under an
        unexpected root runs on the class path and not as a module, and a jar whose
        packages are laid out correctly but whose descriptor exports nothing does
        the reverse.  Most of the world still puts libraries on the class path, so
        a module-only artifact is a migration that broke its consumers while
        passing every behavioral case.
        """
        return self._probe_linkage(config, MODE_CLASSPATH)

    def check_consumer_compiles(self, config: str) -> tuple[bool, str]:
        """A naive downstream compiles against the installed jar and gets right answers.

        Deliberately not the probe.  The probe is the verifier's instrument: it is
        compiled with flags the verifier chose, it reaches for everything, and it
        is written by someone who knows the contract.  This is what a consumer does
        -- `import org.zlib.Zlib`, call four methods, print the results -- with the
        installed jar as its only input, no source tree, no build directory.

        Both linkage modes, and the printed values are checked rather than the exit
        status: a library that compiles, links, runs and computes the wrong CRC has
        not demonstrated anything a consumer wanted.  The values checked are the
        ones fixed by the format rather than by the implementation -- the
        round-tripped length, a known Adler-32, compressBound's documented formula
        -- because the compressed bytes themselves are graded to the byte by
        thousands of probe cases and do not need a fifth opinion here.
        """
        problems: list[str] = []
        for mode in (MODE_MODULE, MODE_CLASSPATH):
            ok, detail, text = self._compile_and_run(
                config, "Consumer", CONSUMER_SRC, mode
            )
            if not ok:
                problems.append(detail)
                continue
            printed = {}
            for line in text.splitlines():
                key, _, value = line.partition("=")
                printed[key.strip()] = value.strip()
            for key, wanted in sorted(CONSUMER_EXPECTED.items()):
                got = printed.get(key)
                if got is None:
                    problems.append(f"[{mode}] the consumer printed no {key}")
                elif got != wanted:
                    problems.append(f"[{mode}] {key}={got}, want {wanted}")
        if problems:
            return False, f"{len(problems)} finding(s): {problems[:4]}"
        return True, "both linkage modes, all pinned values"

    # -- the source tree the migration leaves behind ---------------------
    #
    # Not "did the C go" -- that is a mandatory gate in audit.py, and grading
    # it here too would weight one fact twice and stop State A from reaching a
    # clean structural score, which the mutation sweep depends on.  These cases are
    # about what a downstream still finds in the tree after the rewrite: the
    # specification, the declarations, the build's own account of itself.

    def _repo_text(self, relative: str) -> str | None:
        path = self.repo / relative
        if not path.is_file():
            return None
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    def check_src_spec_header(self, config: str) -> tuple[bool, str]:
        """zlib.h stays in the tree, with its documentation intact.

        The one preserved file whose reason is not obvious, because it is no longer
        installed: shipping a C header would advertise an ABI that no longer
        exists, so the install inventory drops it.  It stays in the *source* tree
        because it is the specification the port was written against -- the only
        human-readable statement of what each of the 88 entry points promises about
        return codes, buffer ownership and stream state.

        A migration that deletes its own specification has removed the evidence
        that the behavior was preserved on purpose rather than reproduced by
        imitation.  So the file must be present, must still be the documented
        header rather than a stub, and must still document the entry points: the
        check counts how many of the C names it mentions, because a submission that
        kept the file and emptied it has done the same thing as deleting it.
        """
        del config
        text = self._repo_text("zlib.h")
        if text is None:
            return False, "zlib.h is not in the tree"
        names = [name for name in self.expect.symbol_map if name in text]
        # 88 entry points, and the threshold is most of them rather than all: a few
        # are macros whose names appear only in a #define, and zlib.h's own
        # deflateInit/inflateInit macro forms differ from the linker names.
        floor = int(len(self.expect.symbol_map) * 0.75)
        if len(names) < floor:
            return False, (
                f"zlib.h mentions {len(names)} of {len(self.expect.symbol_map)} "
                f"entry points, fewer than the {floor} expected of the documented "
                "header; it has been emptied rather than kept"
            )
        if len(text) < 20_000:
            return False, (
                f"zlib.h is {len(text)} bytes; the documented header is an order of "
                "magnitude larger, so this is a stub"
            )
        return True, f"{len(names)}/{len(self.expect.symbol_map)} entry points documented"

    def check_src_module_info(self, config: str) -> tuple[bool, str]:
        """module-info.java exists in the source tree and declares the exports.

        The successor to zlib.map.  That file listed which symbols the shared
        object made visible, version by version, and it is the one deleted file
        whose *job* survives: a module descriptor is the same declaration in the
        same place in the build, and it is checked here in source form because
        module/* already checks the compiled form.

        Source and compiled are two different questions.  A submission could
        assemble module-info.class by hand, or copy one from another project, and
        every module/* case would pass while the build had no descriptor to
        compile.  What this case establishes is that the declaration is part of the
        source the build reads.
        """
        del config
        candidates = sorted(self.repo.rglob("module-info.java"))
        if not candidates:
            if self.self_test:
                # The one source-shaped case with the same shape as the delivery
                # ones: State A's declaration of what is visible is zlib.map, and
                # this case asks for its successor.  There is nothing to read here,
                # not a descriptor that reads wrongly.
                raise vlib.NotApplicable(
                    "no module-info.java: the tree under test is State A, whose "
                    "visibility declaration is still zlib.map")
            return False, "no module-info.java anywhere in the tree"
        wanted = set(self.expect.module_contract["exports"])
        problems: list[str] = []
        for path in candidates:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                problems.append(f"{path.name}: {exc}")
                continue
            relative = path.relative_to(self.repo)
            if not re.search(
                rf"\bmodule\s+{re.escape(self.expect.module_name)}\b", text
            ):
                problems.append(
                    f"{relative} does not declare module {self.expect.module_name}"
                )
            missing = {
                package for package in wanted
                if not re.search(rf"\bexports\s+{re.escape(package)}\s*;", text)
            }
            if missing:
                problems.append(f"{relative} does not export {sorted(missing)}")
        # Any one descriptor satisfying the contract is enough: a tree may carry a
        # second one for tests or for a shaded build, and that is not a finding.
        if len(problems) < len(candidates):
            return True, f"{len(candidates)} descriptor(s), one declaring the contract"
        return False, "; ".join(problems[:4])

    def check_src_cmake_targets(self, config: str) -> tuple[bool, str]:
        """The zlib and zlibstatic target names survive the rewrite.

        A CMake target name is a public interface.  Every downstream project that
        builds zlib as a subdirectory writes `target_link_libraries(app zlib)`, and
        those two names are what it writes -- so renaming them is a breaking change
        no different in kind from renaming a function, and it breaks at
        configure time in someone else's project rather than in this one.

        What the targets *are* changes completely: after the port there is no
        shared library and no static archive, so both become whatever CMake calls a
        thing that produces a jar.  The names are the contract; the kind is not.
        """
        del config
        text = self._repo_text("CMakeLists.txt")
        if text is None:
            return False, "CMakeLists.txt is not in the tree"
        missing = [
            name for name in self.expect.contract["build_contract"]["target_names"]
            if not re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", text)
        ]
        if missing:
            return False, (
                f"CMakeLists.txt no longer names {missing}, so every downstream "
                "that links these targets stops configuring"
            )
        return True, "target names present"

    def check_src_cmake_dangling(self, config: str) -> tuple[bool, str]:
        """CMakeLists.txt references no file the migration removed.

        The defect this catches is specific and common: the port deletes the C,
        rewrites the parts of CMakeLists.txt it needs, and leaves a
        `set(ZLIB_SRCS ...)` or an `install(FILES zconf.h ...)` naming files that no
        longer exist.  Whether that is fatal depends on the CMake command -- some
        error at configure time, some silently do nothing -- so a build can succeed
        with a build file that is half about a codebase that is gone.

        Only names that look like paths to real removable files are considered, and
        a reference inside a comment does not count.  The list of what was removed
        comes from the contract rather than from the diff, because the point is not
        "you deleted something" but "you are still asking for something you
        deleted".
        """
        del config
        text = self._repo_text("CMakeLists.txt")
        if text is None:
            return False, "CMakeLists.txt is not in the tree"
        lines = [
            line.split("#", 1)[0] for line in text.splitlines()
        ]
        body = "\n".join(lines)
        removable = [
            entry for entry in self.expect.contract["removable_paths"]["paths"]
            if not entry.endswith("/")
        ]
        forbidden = list(
            self.expect.contract["forbidden_paths"]["c_sources_that_must_be_removed"]
        )
        dangling: list[str] = []
        for name in sorted(set(removable) | set(forbidden)):
            if (self.repo / name).exists():
                continue
            # `.included` counts as present, and the reason is upstream's own.
            # CMakeLists.txt:76 renames a checked-in zconf.h to zconf.h.included at
            # configure time, so that the copy configure_file() generates in the
            # build directory is the one the compiler sees.  This case reads the tree
            # *after* the build has run, so on any tree that still has that block --
            # State A's, and any port that kept it -- zconf.h has moved and the
            # remaining references to it looked dangling.  They are not: the file is
            # there under the name upstream's build gave it.  A port that deletes
            # zconf.h outright and leaves the references is still caught, because
            # then neither name exists.
            if (self.repo / (name + ".included")).exists():
                continue
            # Word-boundary on a path needs care in both directions.  Trailing:
            # "zconf.h" must not match "zconf.h.in", and "configure" must not match
            # "configure_file", which is a CMake command every build file uses --
            # both handled by excluding a following word character, dot or dash.
            #
            # Leading: a slash must be *allowed*, which is the whole difficulty.
            # This lookbehind excluded one at first, on the theory that it was
            # anchoring the name to a path component; the effect was that
            # ${CMAKE_CURRENT_SOURCE_DIR}/zlib.map -- the exact form CMake writes
            # every path reference in -- did not match, and the case passed a tree
            # with zlib.map deleted and still referenced.  A gate that cannot see
            # the only spelling its target actually uses is a gate that passes
            # everything, and it did.
            if re.search(rf"(?<![\w.-]){re.escape(name)}(?![\w.-])", body):
                dangling.append(name)
        if dangling:
            return False, (
                f"CMakeLists.txt still references {len(dangling)} removed file(s): "
                f"{dangling[:6]}"
            )
        return True, "no reference to a removed file"

    def check_src_license(self, config: str) -> tuple[bool, str]:
        """LICENSE survives, and so does the notice zlib.h carries.

        The one requirement that is not about engineering.  zlib's licence obliges
        anyone who distributes the source to keep the notice, and a translation is
        a derivative work: the port inherits the obligation along with the
        behavior.  Deleting the notice is the one defect in this suite that is a
        legal problem before it is a technical one, which is why it is graded
        rather than assumed.

        Two places, because they are two separate obligations: the LICENSE file for
        the distribution, and the in-file notice that travels with the code even
        when the file is copied out of the tree by itself.
        """
        del config
        text = self._repo_text("LICENSE")
        if text is None:
            return False, "LICENSE is not in the tree"
        marks = ("Jean-loup Gailly", "Mark Adler")
        missing = [mark for mark in marks if mark not in text]
        if missing:
            return False, f"LICENSE no longer attributes {missing}"
        if "warranty" not in text.lower():
            return False, "LICENSE no longer carries the warranty disclaimer"
        header = self._repo_text("zlib.h") or ""
        if not any(mark in header for mark in marks):
            return False, (
                "zlib.h carries no copyright notice, so a copy of it taken out of "
                "the tree carries none either"
            )
        return True, "LICENSE and the in-file notice both present"

    def check_src_docs(self, config: str) -> tuple[bool, str]:
        """The format specification and the project's own history survive.

        The three RFCs are to the bytes what zlib.h is to the API: 1950 is the zlib
        stream, 1951 is deflate, 1952 is gzip, and together they are the only
        normative statement of what this implementation must produce.  A rewrite
        that deletes them has deleted the evidence that its output is correct on
        purpose rather than by imitation -- and unlike doc/algorithm.txt, which
        describes the C's own data structures and may go with the C, an RFC is
        about the format and survives any implementation.

        README and ChangeLog are the project's account of itself and of every
        release before this one.  A change of implementation language does not start
        the history over, and a distribution ships both.
        """
        del config
        required = [
            path for path in self.expect.contract["preserved_paths"]["paths"]
            if path.startswith("doc/") or path in ("README", "ChangeLog")
        ]
        if not required:
            return False, (
                "the contract preserves no documentation, so this case grades "
                "nothing; preserved_paths and this check have drifted apart"
            )
        missing = [path for path in required if not (self.repo / path).is_file()]
        if missing:
            return False, f"{len(missing)} document(s) removed: {missing}"
        thin = [
            path for path in required
            if (self.repo / path).stat().st_size < 512
        ]
        if thin:
            return False, (
                f"present but emptied, which is deletion with extra steps: {thin}"
            )
        return True, f"{len(required)} documents preserved"

    # -- the API, by reflection ------------------------------------------

    def _surface_or_fail(self, config: str) -> tuple[SurfaceDump | None, str]:
        dump = self.surface(config)
        if not dump.ok:
            return None, dump.detail or "the surface dump did not run"
        return dump, ""

    @staticmethod
    def _class_records(dump: SurfaceDump) -> dict[str, dict[str, str]]:
        """`class X kind=.. mods=.. super=.. ifaces=..` -> {X: {kind: ..}}."""
        out: dict[str, dict[str, str]] = {}
        for record in dump.of_kind("class"):
            name, _, rest = record.partition(" ")
            attrs: dict[str, str] = {}
            for token in rest.split():
                key, _, value = token.partition("=")
                attrs[key] = value
            out[name] = attrs
        return out

    @staticmethod
    def _member_records(dump: SurfaceDump, kind: str) -> dict[str, dict[str, str]]:
        """A member record keyed by its signature, with the trailing k=v pairs.

        `method org.zlib.Zlib#version() returns=java.lang.String mods=public+static
        throws=-` becomes {"org.zlib.Zlib#version()": {"returns": ..., ...}}.
        """
        out: dict[str, dict[str, str]] = {}
        for record in dump.of_kind(kind):
            head, _, rest = record.partition(" ")
            attrs: dict[str, str] = {}
            for token in rest.split():
                key, _, value = token.partition("=")
                attrs[key] = value
            out[head] = attrs
        return out

    @staticmethod
    def _signature(owner: str, member: dict) -> str:
        return f"{owner}#{member['name']}({','.join(member.get('params', []))})"

    def check_api_types(self, config: str) -> tuple[bool, str]:
        """Every declared type exists, with the declared kind and modifiers.

        Modifiers are compared as a set rather than tested one at a time, because
        the omissions matter as much as the presences.  `Zlib` is final so nobody
        subclasses a namespace; `ZStream` is abstract so a bare one is not mistaken
        for an engine; the hook interfaces are static so they can be implemented
        without an enclosing instance.  Each of those is a decision a consumer can
        depend on, and dropping any of them widens the surface silently.
        """
        dump, why = self._surface_or_fail(config)
        if dump is None:
            return False, why
        classes = self._class_records(dump)
        absent = {
            record.split(" ", 1)[0]: record for record in dump.of_kind("missing")
        }
        problems: list[str] = []
        for name, spec in sorted(self.expect.api_types.items()):
            if name in absent:
                problems.append(f"{name} does not load: {absent[name]}")
                continue
            attrs = classes.get(name)
            if attrs is None:
                problems.append(f"{name} was not reported at all")
                continue
            kind = spec.get("kind", "class")
            if attrs.get("kind") != kind:
                problems.append(f"{name} is a {attrs.get('kind')}, want {kind}")
            wanted = set(spec.get("modifiers", ["public"]))
            got = set(attrs.get("mods", "-").split("+")) - {"-"}
            if got != wanted:
                problems.append(
                    f"{name} mods={sorted(got)}, want {sorted(wanted)}"
                )
            parent = spec.get("extends")
            if parent and attrs.get("super") != parent:
                problems.append(
                    f"{name} extends {attrs.get('super')}, want {parent}"
                )
        if problems:
            return False, f"{len(problems)} finding(s): {problems[:4]}"
        return True, f"{len(self.expect.api_types)} types"

    def check_api_members(self, config: str) -> tuple[bool, str]:
        """Every declared member exists with the declared signature.

        This is where the C layout probe went.  That one measured sizeof(z_stream)
        and two field offsets, because in C those three numbers are what a consumer
        compiled against the old header depends on.  On the JVM the equivalent
        dependency is the descriptor: a consumer's class file names the parameter
        types and the return type of every call it makes, and a method whose long
        became an int is as unreachable as a struct whose fields moved.

        Static is part of the signature for this purpose -- `Zlib.adler32` is called
        with invokestatic and an instance method of the same name is not the same
        member -- so it is compared.  `final` and `synchronized` are not: they
        constrain the implementation, not the call.
        """
        dump, why = self._surface_or_fail(config)
        if dump is None:
            return False, why
        methods = self._member_records(dump, "method")
        fields = self._member_records(dump, "field")
        ctors = self._member_records(dump, "ctor")
        problems: list[str] = []
        counted = 0
        for name, spec in sorted(self.expect.api_types.items()):
            is_interface = spec.get("kind") == "interface"
            for member in spec.get("methods", []):
                counted += 1
                key = self._signature(name, member)
                attrs = methods.get(key)
                if attrs is None:
                    problems.append(f"missing method {key}")
                    continue
                if attrs.get("returns") != member["returns"]:
                    problems.append(
                        f"{key} returns {attrs.get('returns')}, "
                        f"want {member['returns']}"
                    )
                mods = set(attrs.get("mods", "-").split("+")) - {"-"}
                if "public" not in mods:
                    problems.append(f"{key} is not public (mods={sorted(mods)})")
                if bool(member.get("static")) != ("static" in mods):
                    seen_as = "static" if "static" in mods else "an instance method"
                    problems.append(
                        f"{key} is {seen_as}, and the contract says otherwise"
                    )
                if is_interface and "abstract" not in mods:
                    problems.append(f"{key} is a default method, not abstract")
            for member in spec.get("fields", []):
                counted += 1
                key = f"{name}#{member['name']}"
                attrs = fields.get(key)
                if attrs is None:
                    problems.append(f"missing field {key}")
                    continue
                if attrs.get("type") != member["type"]:
                    problems.append(
                        f"{key} is {attrs.get('type')}, want {member['type']}"
                    )
            for member in spec.get("constructors", []):
                counted += 1
                key = f"{name}({','.join(member.get('params', []))})"
                if key not in ctors:
                    problems.append(f"missing constructor {key}")
        if problems:
            return False, f"{len(problems)} of {counted} members wrong: {problems[:4]}"
        return True, f"{counted} members"

    def check_api_no_extra(self, config: str) -> tuple[bool, str]:
        """No public member beyond the contract: the surface did not widen.

        A published surface is a promise to keep it, so an extra public method is
        not a harmless bonus -- it is a promise nobody meant to make, and removing
        it later is a breaking change.  It is also how an implementation detail
        escapes: the fastest way to make a driver work is to make the internals
        public, and then the internals are API.

        Overriding Object's three methods is allowed, and nothing else is.
        toString on a stream is a debugging courtesy that costs nobody anything;
        equals and hashCode travel with it because a class that overrides one and
        not the others is the more common defect.
        """
        dump, why = self._surface_or_fail(config)
        if dump is None:
            return False, why
        declared: set[str] = set()
        for name, spec in self.expect.api_types.items():
            for member in spec.get("methods", []):
                declared.add(self._signature(name, member))
            for member in spec.get("fields", []):
                declared.add(f"{name}#{member['name']}")
            for member in spec.get("constructors", []):
                declared.add(f"{name}({','.join(member.get('params', []))})")
        # The constants are declared in their own section rather than as fields of
        # the holder, because there are 40 of them and listing them twice would be
        # two lists to keep in step.  They are still public fields, so this case has
        # to know about them or it reports all 40 as undeclared -- which it did.
        holder = self.expect.constants_holder
        for name in list(self.expect.int_constants) + list(self.expect.string_constants):
            declared.add(f"{holder}#{name}")
        for name in self.expect.api_types:
            declared.add(f"{name}#toString()")
            declared.add(f"{name}#hashCode()")
            declared.add(f"{name}#equals(java.lang.Object)")
        # An abstract class's constructor is not a member a consumer can call, and
        # a subclass has to be able to reach it -- ZStream declares none precisely
        # so that Deflater and Inflater inherit the default, which javac makes
        # public.  Counting that as a widened surface would fail every correct
        # submission, so constructors of abstract classes are not counted.
        abstract = {
            name for name, spec in self.expect.api_types.items()
            if "abstract" in spec.get("modifiers", []) and spec.get("kind") != "interface"
        }
        extra: list[str] = []
        for kind in ("method", "field", "ctor"):
            for key, attrs in self._member_records(dump, kind).items():
                owner = key.split("#")[0].split("(")[0]
                if owner not in self.expect.api_types:
                    continue
                if kind == "ctor" and owner in abstract:
                    continue
                if "public" not in set(attrs.get("mods", "").split("+")):
                    continue
                if key not in declared:
                    extra.append(f"{kind} {key}")
        if extra:
            return False, f"{len(extra)} undeclared public member(s): {sorted(extra)[:6]}"
        return True, ""

    def check_api_constants(self, config: str) -> tuple[bool, str]:
        """The pinned constants hold their pinned values, read two ways.

        Reflection reads the field; the class file's ConstantValue attribute is
        what a consumer's compiler copies into its own class file.  Those are
        different numbers when a static initializer assigns something other than
        the declared literal, and javac has already inlined the declared one
        everywhere by then -- so the library and everything built against it
        disagree, and nothing reports it.  Both readings are taken and both must
        equal the contract.

        The values themselves are not arbitrary.  Z_STREAM_SIZE is 112, which is
        sizeof(z_stream) on the reference platform, and it survives into a language
        with no sizeof because zlib's init macros pass it across the ABI boundary as
        a version check.  Renumbering any of these compiles perfectly and produces a
        library that silently means something else.
        """
        dump, why = self._surface_or_fail(config)
        if dump is None:
            return False, why
        holder = self.expect.constants_holder
        seen: dict[str, str] = {}
        for kind in ("const", "sconst", "lconst"):
            for record in dump.of_kind(kind):
                key, _, value = record.partition(" = ")
                if key.startswith(holder + "#"):
                    seen[key.split("#", 1)[1]] = value.strip()
        failures = [r for r in dump.of_kind("constfail") if r.startswith(holder + "#")]
        problems: list[str] = [f"unreadable: {f}" for f in failures]

        wanted: dict[str, str] = {
            name: str(value) for name, value in self.expect.int_constants.items()
        }
        wanted.update(self.expect.string_constants)
        for name, value in sorted(wanted.items()):
            got = seen.get(name)
            if got is None:
                problems.append(f"{name} is not a public static final constant")
            elif got != value:
                problems.append(f"{name} = {got}, want {value}")

        # The declared type, not just the value.  A `public static final long Z_OK`
        # holds the right number and is still the wrong constant: it inlines as a
        # long, so `switch (rc)` on it will not compile in a consumer and every
        # overload it is passed to resolves differently.  Nothing about the value
        # reading catches that -- 0 prints as 0 either way.
        fields = self._member_records(dump, "field")
        for name in sorted(self.expect.int_constants):
            attrs = fields.get(f"{holder}#{name}")
            if attrs is not None and attrs.get("type") != "int":
                problems.append(f"{name} is declared {attrs.get('type')}, want int")
        for name in sorted(self.expect.string_constants):
            attrs = fields.get(f"{holder}#{name}")
            if attrs is not None and attrs.get("type") != "java.lang.String":
                problems.append(
                    f"{name} is declared {attrs.get('type')}, want java.lang.String"
                )

        # The second reading.  A missing ConstantValue is itself the finding: it
        # means the field is assigned at class-initialization time, so no consumer
        # can inline it and every consumer has to load the class to read it.
        declaring = classfile.internal(holder)
        parsed = next(
            (c for c in self.classes(config) if c.this_class == declaring), None
        )
        if parsed is None:
            problems.append(f"{holder} is not in the jar as a class file")
        else:
            byte_values: dict[str, object] = {}
            for member in parsed.fields:
                value = member.constant_value(parsed.pool)
                if value is not None:
                    byte_values[member.name] = value
            for name, value in sorted(self.expect.int_constants.items()):
                got = byte_values.get(name)
                if got is None:
                    problems.append(f"{name} has no ConstantValue attribute")
                elif got != value:
                    problems.append(
                        f"{name}: ConstantValue is {got} but the contract says "
                        f"{value}"
                    )
            for name, value in sorted(self.expect.string_constants.items()):
                got = byte_values.get(name)
                if got is None:
                    problems.append(f"{name} has no ConstantValue attribute")
                elif got != value:
                    problems.append(f"{name}: ConstantValue is {got!r}")
        if problems:
            return False, f"{len(problems)} finding(s): {problems[:4]}"
        return True, f"{len(wanted)} constants, both readings agreeing"

    def check_api_constants_final(self, config: str) -> tuple[bool, str]:
        """The constants are public static final, not mutable fields.

        A non-final `public static int Z_OK` is writable by anybody in the process,
        and one library initialising badly would change what every other consumer
        of these names computes.  It also cannot be inlined, so it is not a
        constant in the sense the contract means.
        """
        dump, why = self._surface_or_fail(config)
        if dump is None:
            return False, why
        holder = self.expect.constants_holder
        names = set(self.expect.int_constants) | set(self.expect.string_constants)
        fields = self._member_records(dump, "field")
        wanted = set(self.expect.api["constants"]["modifiers"])
        problems: list[str] = []
        for name in sorted(names):
            attrs = fields.get(f"{holder}#{name}")
            if attrs is None:
                problems.append(f"{name} is not a declared field of {holder}")
                continue
            mods = set(attrs.get("mods", "-").split("+")) - {"-"}
            missing = wanted - mods
            if missing:
                problems.append(f"{name} is not {'+'.join(sorted(missing))}")
            if "final" not in mods:
                problems.append(f"{name} is not final")
        if problems:
            return False, f"{len(problems)} finding(s): {problems[:4]}"
        return True, ""

    def check_api_stream_fields(self, config: str) -> tuple[bool, str]:
        """ZStream's public fields exist with the declared types.

        These are z_stream, field for field, and they stay public for the reason
        they were public in C: the caller sets next_in and reads total_out, so the
        struct is the calling convention rather than an implementation detail.  The
        pairs that replace a C pointer -- nextIn with nextInIndex, nextOut with
        nextOutIndex -- are the one place the port had to invent something, and a
        submission that folded the index into the array or into a wrapper object has
        changed how every caller drives the library.

        Types are compared exactly.  totalIn is a long because C's uLong can exceed
        2^31, and a port that made it an int works on every test small enough to
        pass and silently truncates on a 3 GB stream.
        """
        dump, why = self._surface_or_fail(config)
        if dump is None:
            return False, why
        spec = self.expect.api_types.get("org.zlib.ZStream")
        if spec is None:
            return False, "the contract declares no ZStream"
        fields = self._member_records(dump, "field")
        problems: list[str] = []
        for member in spec.get("fields", []):
            key = f"org.zlib.ZStream#{member['name']}"
            attrs = fields.get(key)
            if attrs is None:
                problems.append(f"missing {member['name']}")
                continue
            if attrs.get("type") != member["type"]:
                problems.append(
                    f"{member['name']} is {attrs.get('type')}, want {member['type']}"
                )
            mods = set(attrs.get("mods", "-").split("+")) - {"-"}
            if "public" not in mods:
                problems.append(f"{member['name']} is not public")
            if "static" in mods:
                problems.append(f"{member['name']} is static; it is per-stream state")
            if "final" in mods:
                problems.append(f"{member['name']} is final; the caller writes it")
        if problems:
            return False, f"{len(problems)} finding(s): {problems[:4]}"
        return True, f"{len(spec.get('fields', []))} fields"

    def check_api_nested_hooks(self, config: str) -> tuple[bool, str]:
        """The four callback interfaces are present, static, and single-method.

        Allocator and Deallocator are C's zalloc and zfree; In and Out are
        inflateBack's in_func and out_func.  They are the part of the surface most
        likely to be quietly dropped -- a Java port allocates for itself, so the
        allocator hook does nothing the implementation needs -- and dropping it
        removes a capability rather than a convenience: a sandboxed consumer uses it
        to account for every buffer the library takes, and there is no other way to
        ask.
        """
        dump, why = self._surface_or_fail(config)
        if dump is None:
            return False, why
        classes = self._class_records(dump)
        methods = self._member_records(dump, "method")
        nested = [
            (name, spec) for name, spec in sorted(self.expect.api_types.items())
            if "$" in name
        ]
        if not nested:
            return False, "the contract declares no nested hook types"
        problems: list[str] = []
        for name, spec in nested:
            attrs = classes.get(name)
            if attrs is None:
                problems.append(f"{name} was not reported")
                continue
            if attrs.get("kind") != "interface":
                problems.append(f"{name} is a {attrs.get('kind')}, want an interface")
            mods = set(attrs.get("mods", "-").split("+")) - {"-"}
            if "static" not in mods:
                problems.append(
                    f"{name} is an inner interface, so implementing it would need "
                    "an enclosing instance"
                )
            declared = spec.get("methods", [])
            for member in declared:
                key = self._signature(name, member)
                if key not in methods:
                    problems.append(f"missing {key}")
            found = [k for k in methods if k.startswith(name + "#")]
            if len(found) > len(declared):
                problems.append(
                    f"{name} declares {len(found)} methods, so it is not a "
                    "behavioural interface any more"
                )
        if problems:
            return False, f"{len(problems)} finding(s): {problems[:4]}"
        return True, f"{len(nested)} hook interfaces"

    def check_api_crc_table(self, config: str) -> tuple[bool, str]:
        """crcTable() must hand out a copy of the table, not the table.

        This is the one place the port is *required* to differ from C.
        get_crc_table() returns a pointer to a static array, so two calls give the
        same pointer and a caller that writes through it corrupts every checksum the
        process computes afterwards.  Java cannot publish a shared int[] and stay
        safe, so the contract requires a copy -- which means the two languages give
        opposite correct answers to "is it the same object twice", and this cannot
        be a differential case.  It is graded here instead.

        Three facts, and the third is the one with teeth: the table is 256 entries,
        two calls return distinct arrays, and mutating the returned array does not
        change what the next call returns.  A port that copies on the first call and
        caches the copy passes the first two and fails the third.
        """
        dump, why = self._surface_or_fail(config)
        if dump is None:
            return False, why
        behavior = dump.behavior()
        if "crc-table-error" in behavior:
            return False, f"crcTable() threw {behavior['crc-table-error']}"
        problems: list[str] = []
        if behavior.get("crc-table-length") != "256":
            problems.append(
                f"the table is {behavior.get('crc-table-length')} entries, want 256"
            )
        if behavior.get("crc-table-identity") != "distinct":
            problems.append(
                "two calls returned the same array, so a caller can corrupt the "
                "library's own table"
            )
        if behavior.get("crc-table-mutation") != "isolated":
            problems.append(
                f"writing to the returned array was {behavior.get('crc-table-mutation')} "
                "from what the next call returns"
            )
        # Entry 0 of the standard CRC-32 table is 0 for the reflected polynomial;
        # a table of the right length full of the wrong numbers is still wrong.
        if behavior.get("crc-table-entry0") not in (None, "0"):
            problems.append(
                f"table[0] = {behavior['crc-table-entry0']}, want 0"
            )
        if problems:
            return False, "; ".join(problems)
        return True, ""

    def check_api_exceptions(self, config: str) -> tuple[bool, str]:
        """No method declares a checked exception the contract does not.

        The contract declares none, and that is a deliberate reading of what a port
        may change.  C signals failure by return code, every one of them named in
        the constants, and a caller written against those codes has an if-chain.  A
        port that throws IOException from gzread instead has not translated the
        interface -- it has replaced it, and every caller has to be restructured
        rather than recompiled.

        Unchecked exceptions in a throws clause are documentation and cost the
        caller nothing, so they are allowed by name.

        What is read is the *callable* surface, public and protected, and not every
        declared method.  Surface.java dumps declared members so the closed-surface
        check can see what a class kept private; a private helper that wraps a
        FileChannel read has to say `throws IOException`, because that is what NIO
        throws, and no consumer can observe it.  Grading it here would have made the
        contract's "no checked exceptions" mean "do not use java.nio", which is a
        different requirement and not one anybody published.
        """
        dump, why = self._surface_or_fail(config)
        if dump is None:
            return False, why
        problems: list[str] = []
        for kind in ("method", "ctor"):
            for key, attrs in sorted(self._member_records(dump, kind).items()):
                owner = key.split("#")[0].split("(")[0]
                if owner not in self.expect.api_types:
                    continue
                mods = set(attrs.get("mods", "").split("+"))
                if not mods & {"public", "protected"}:
                    continue
                thrown = attrs.get("throws", "-")
                if thrown == "-":
                    continue
                for name in thrown.split(","):
                    if name not in UNCHECKED_EXCEPTIONS:
                        problems.append(f"{key} throws {name}")
        if problems:
            return False, f"{len(problems)} checked exception(s): {problems[:5]}"
        return True, ""

    def check_api_back_window(self, config: str) -> tuple[bool, str]:
        """A window shorter than 1 << windowBits is refused, not overrun.

        The one case that exists only because the target is a JVM.  C's
        inflateBackInit_ takes `unsigned char *window` and cannot see its length, so
        it does not check: a short window is undefined behaviour and that is C's
        contract.  A Java implementation cannot help seeing the length, and the
        contract requires Z_STREAM_ERROR rather than letting an array bound decide
        -- because "throws ArrayIndexOutOfBoundsException from somewhere deep in the
        inflate loop" is not a return code any caller can act on.

        probe.c cannot ask this question, which is why it is here.  Both directions
        are required: refusing every window would satisfy the short case, so the
        exact-size window must be accepted.
        """
        dump, why = self._surface_or_fail(config)
        if dump is None:
            return False, why
        behavior = dump.behavior()
        if "back-window-error" in behavior:
            return False, (
                f"init threw {behavior['back-window-error']} instead of returning "
                "a code"
            )
        stream_error = str(self.expect.int_constants.get("Z_STREAM_ERROR", -2))
        ok = str(self.expect.int_constants.get("Z_OK", 0))
        expected = {
            "back-window-short": stream_error,
            "back-window-exact": ok,
            "back-window-empty": stream_error,
            "back-window-null": stream_error,
        }
        problems = [
            f"{key}: got {behavior.get(key)}, want {value}"
            for key, value in sorted(expected.items())
            if behavior.get(key) != value
        ]
        if problems:
            return False, "; ".join(problems)
        return True, ""
