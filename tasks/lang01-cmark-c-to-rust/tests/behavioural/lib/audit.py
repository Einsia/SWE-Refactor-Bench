#!/usr/bin/env python3
"""Migration-audit gates: did the debt actually get paid?

The behavioural suite answers "does it still work".  It cannot answer "was the
work done", and on a rewrite task those come apart badly.  A submission that
keeps the C implementation and adds a thin Rust veneer passes every behavioral
test by construction, because the behavior is still being produced by the code
that was supposed to be replaced.  So the behavioural score is withheld unless
the mandatory gates here hold.

Every gate here has an *artifact* to point at.  That is the line: a claim about
what the submitted files say is stage 1's, where a scan reads both trees and a
reviewer with both trees open judges; a claim about what the build produced is
this module's, because producing it is the only way to find out.  The gates group
by the claim they defend:

  G2  no C toolchain participated in producing the artifacts
  G3  the shipped binaries carry Rust provenance and no C provenance
  G4  one implementation, on the default path, computing rather than recalling
  G5  the release contract (version, ABI surface) is intact

G1 -- "the old implementation has left the source closure" -- was here too, and
is the clearest illustration of the line.  Answering it means reading source, so
it is scored where reading source is what happens.

Two principles run through what remains.  First, prefer structural impossibility
over detection: the compiler shim makes compiling repository C fail rather than
merely logging it, so G2 is mostly a matter of reading what the shim recorded.
Second, prefer evidence over inference: each gate reports the specific paths,
symbols or log lines that decided it, because a gate that fails without saying
why is indistinguishable from a broken gate.

A gate that cannot run -- because the build failed, say -- fails closed.  Being
unable to demonstrate audit is not the same as demonstrating it, and the
alternative would let a submission that breaks the build inherit a pass.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import elflib
import vlib
from build import REAL_PATH, BuildOutcome
from vlib import GateOutcome, Log

RUN_TIMEOUT = 60.0

# Directories the build itself creates.  Only one caller remains -- the snapshot
# `driver.py` takes of the tree it was handed, before any build runs -- and it
# wants the submitted files without the outputs of a build the submitter happened
# to leave behind.
BUILD_DIR_MARKERS = ("CMakeCache.txt", "CACHEDIR.TAG", ".rustc_info.json",
                     "build.ninja", ".ninja_deps")

# Names that identify a Rust build directory even when empty of markers.
GENERATED_DIR_NAMES = {"target", "__pycache__", ".git"}

# Symbols that would let the library reach an implementation outside itself.
DYNAMIC_LOADING_SYMBOLS = {
    "dlopen", "dlmopen", "dlsym", "dlvsym", "dladdr", "dlinfo", "dlclose",
}

# Symbols whose presence means the artifact can hand work to another program.
#
# Deliberately narrower than "everything that creates a process": Rust's std
# references posix_spawn, fork and clone from its own process module, and the
# linker does not always drop those references even when nothing calls them.
# Flagging them would fail honest submissions.
#
# Narrowness is not the whole answer either, because std's main codegen unit
# references `execvp` -- and `dlsym` above -- so a `crate-type = ["staticlib"]`
# archive, which contains the whole of std, imports them however the submission is
# written.  Dropping those two names would lose a real signal, so instead
# `_imported_symbols` attributes each import to the archive member it came from:
# the side effect is reported against the toolchain that caused it rather than
# against the submission.
PROCESS_SPAWN_SYMBOLS = {
    "system", "popen", "execl", "execlp", "execle", "execv", "execvp",
    "execvpe", "execve", "execveat", "fexecve",
}

#: `None` is a real answer from the toolchain-member lookup, so the cache needs a
#: third state for "not asked yet".
_UNSET = object()


@dataclass
class Artifacts:
    """The delivered files one link configuration produced."""

    config: str
    prefix: Path
    build_dir: Path
    library: Path | None
    executable: Path | None
    shim_log: Path | None


def _is_generated_dir(path: Path) -> bool:
    if path.name in GENERATED_DIR_NAMES:
        return True
    return any((path / marker).exists() for marker in BUILD_DIR_MARKERS)

def walk_source(root: Path) -> list[Path]:
    """Every submitted file, excluding directories the build produced.

    The distinction matters: a `.o` inside an out-of-source build directory is a
    normal build output, while the same file at the top of the tree is a
    prebuilt binary someone checked in.  Build directories are identified by the
    markers their generators leave, not by a guessed name, so a submission that
    calls its build directory something unusual is treated the same way.
    """
    out: list[Path] = []
    for current, dirs, files in os.walk(root):
        here = Path(current)
        dirs[:] = sorted(d for d in dirs if not _is_generated_dir(here / d))
        for name in sorted(files):
            out.append(here / name)
    return out


def read_text(path: Path, limit: int = 4_000_000) -> str:
    try:
        with path.open("rb") as handle:
            return handle.read(limit).decode("utf-8", "replace")
    except OSError:
        return ""


class IntegrityAuditor:
    """Evaluates every declared audit gate against one submission's install.

    Note what is not a parameter: the submitted tree.  Every gate this class
    evaluates is a claim about the built and installed artifacts -- a symbol the
    shipped ELF imports, the libraries a prefix received, the version on four
    published surfaces, six renderings of a document composed at grading time from
    `os.urandom(16)`.  Not taking `repo` is the mechanical statement of that: a gate
    here *cannot* assert about the repository, because it cannot see it.

    Which is the point, because the repository claims are the ones that do not
    belong in this stage.  Walking the tree for `.o` files or grepping build files
    for a fetch, scored where two builds are compared on what they answer, would put
    "a stale object file is checked in" and "the library does not reproduce the
    reference on unseen input" on the same footing.  Those readings are stage 1's,
    where a read-only scan reads and a model with both trees open judges -- and a
    licence hash or a conformance-data hash is the scan's alone, reported to the
    reviewer without a vote attached, because neither is a claim about whether the
    port works.

    `baseline` stays: State A is not the submission, and `no-c-provenance` needs the
    *names* of State A's twenty C files so it can look for them in the shipped symbol
    table.  Reading State A tells you nothing about what a submission did.
    """

    def __init__(
        self,
        outcomes: dict[str, BuildOutcome],
        baseline: Path,
        reference_prefixes: dict[str, Path],
        contract: dict,
        scratch: Path,
        log: Log,
    ) -> None:
        self.outcomes = outcomes
        self.baseline = baseline
        self.reference_prefixes = reference_prefixes
        self.contract = contract
        self.scratch = scratch
        self.log = log
        self._elf_cache: dict[Path, elflib.ElfFile | None] = {}
        #: Sentinel-initialised because None is a real answer here: it means the
        #: sysroot was unreadable, which the symbol gates report.
        self._toolchain_member_cache: frozenset[str] | None | object = _UNSET
        scratch.mkdir(parents=True, exist_ok=True)

    # -- shared reads -----------------------------------------------------

    def baseline_c_basenames(self) -> set[str]:
        """The C translation units State A shipped, by basename."""
        src = self.baseline / "src"
        if not src.is_dir():
            return set()
        return {p.name for p in src.glob("*.c")}

    def elf(self, path: Path) -> elflib.ElfFile | None:
        if path not in self._elf_cache:
            try:
                self._elf_cache[path] = elflib.load(path)
            except (OSError, elflib.ElfError):
                self._elf_cache[path] = None
        return self._elf_cache[path]

    def artifacts(self, config: str) -> Artifacts | None:
        outcome = self.outcomes.get(config)
        if outcome is None:
            return None
        prefix = outcome.prefix
        libname = "libcmark.so.0.31.1" if config == "shared" else "libcmark.a"
        library = prefix / "lib" / libname
        executable = prefix / "bin" / "cmark"
        return Artifacts(
            config=config,
            prefix=prefix,
            build_dir=outcome.build_dir,
            library=library if library.is_file() else None,
            executable=executable if executable.is_file() else None,
            shim_log=outcome.shim_log,
        )

    def shim_events(self, config: str) -> list[dict]:
        outcome = self.outcomes.get(config)
        if outcome is None or outcome.shim_log is None:
            return []
        if not outcome.shim_log.is_file():
            return []
        import json

        events: list[dict] = []
        for line in outcome.shim_log.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
        return events

    # -- the gate table ---------------------------------------------------

    def evaluate(self, cases: list[dict]) -> list[GateOutcome]:
        results: list[GateOutcome] = []
        for case in cases:
            check = case["check"]
            mandatory = bool((case.get("params") or {}).get("mandatory", True))
            handler = getattr(self, f"gate_{check.replace('-', '_')}", None)
            if handler is None:
                results.append(
                    GateOutcome(
                        gate_id=case["id"],
                        check=check,
                        mandatory=mandatory,
                        passed=False,
                        detail=f"no handler implemented for gate '{check}'",
                    )
                )
                continue
            try:
                passed, detail, evidence = handler()
            except Exception as exc:
                passed = False
                detail = f"gate raised {type(exc).__name__}: {exc}"
                evidence = []
            results.append(
                GateOutcome(
                    gate_id=case["id"],
                    check=check,
                    mandatory=mandatory,
                    passed=passed,
                    detail=detail,
                    evidence=evidence,
                )
            )
        return results

    # == G1: the old implementation has left the source closure ============
    def gate_compiler_shim_clean(self) -> tuple[bool, str, list[str]]:
        """The shimmed compilers must never have been asked to compile.

        This is the gate the shim exists to make cheap: whether a repository C
        source was compiled during the graded build is a line in a ledger rather
        than an inference from a build log.  Either ledger entry answers it.

        A `reject` is a compile the shim refused, which only happens under
        SRB_SHIM=enforce.  A forwarded `compile` is one it recorded and let
        through, which is what the graded `record` mode does.  The two differ in
        what happened to the build, not in what the build asked for, so both fail
        this gate -- and the distinction is kept in the evidence, because "the shim
        permitted this" and "the shim was bypassed" are different findings and only
        the second is a hole in the measurement.
        """
        rejects: list[str] = []
        recorded_compiles: list[str] = []
        bypassed: list[str] = []
        saw_any = False
        for config in sorted(self.outcomes):
            for event in self.shim_events(config):
                saw_any = True
                kind = event.get("event")
                verdict = event.get("verdict")
                sources = " ".join(event.get("sources") or [])[:200]
                if kind == "reject":
                    rejects.append(
                        f"[{config}] {event.get('tool')} refused: {sources}")
                elif kind == "forward" and verdict == "compile":
                    where = recorded_compiles if event.get("mode") == "record" \
                        else bypassed
                    where.append(
                        f"[{config}] {event.get('tool')} compiled {sources}")
        attempts = len(rejects) + len(recorded_compiles) + len(bypassed)
        if attempts:
            # A build that compiles C is the finding whichever way the shim was
            # configured; the summary names which, so the report is readable
            # without the ledger beside it.
            how = []
            if recorded_compiles:
                how.append(f"{len(recorded_compiles)} recorded and permitted")
            if rejects:
                how.append(f"{len(rejects)} refused")
            if bypassed:
                how.append(f"{len(bypassed)} forwarded outside record mode")
            return (
                False,
                f"the build compiled C/C++ from the repository {attempts} time(s) "
                f"({'; '.join(how)}); the submission still depends on a C compiler",
                (recorded_compiles + rejects + bypassed)[:30],
            )
        if not saw_any:
            return True, "the shimmed compilers were never invoked at all", []
        return True, "the shimmed compilers saw only link and query invocations", []

    def gate_no_compile_syscalls(self) -> tuple[bool, str, list[str]]:
        """A C compiler must not have been reached by a path around the shim.

        The shim owns the well-known driver names on PATH, so the remaining way
        to compile C is to call a compiler by absolute path or under a name the
        shim does not shadow.  Both leave the same fingerprint: a compiler
        invocation in the build's own generated rules or logs that the shim
        ledger has no matching record of.
        """
        suspicious: list[str] = []
        absolute_cc = re.compile(
            r"(?:/usr(?:/local)?)?/(?:bin|lib(?:exec)?)/[\w./-]*"
            r"(?:cc1plus|cc1|\bas\b|gcc-\d+|g\+\+-\d+|clang-\d+)"
        )
        for config, outcome in sorted(self.outcomes.items()):
            for label, result in (
                ("configure", outcome.configure),
                ("build", outcome.compile),
            ):
                if result is None:
                    continue
                text = (result.stdout + b"\n" + result.stderr).decode(
                    "utf-8", "replace"
                )
                for match in absolute_cc.finditer(text):
                    token = match.group(0)
                    # rustc's own link step legitimately reaches the assembler
                    # and linker; those are not compile actions.
                    if token.endswith(("/as", "/ld", "/ld.bfd", "/ld.gold")):
                        continue
                    suspicious.append(f"[{config}] {label}: {token}")
            build_dir = outcome.build_dir
            for name in ("build.ninja", "Makefile", "CMakeCache.txt"):
                candidate = build_dir / name
                if not candidate.is_file():
                    continue
                for line in read_text(candidate).splitlines():
                    if "cc1" in line or "cc1plus" in line:
                        suspicious.append(f"[{config}] {name}: {line.strip()[:200]}")
        if suspicious:
            unique = sorted(set(suspicious))
            return (
                False,
                f"evidence of a C compiler back end being reached: {len(unique)} "
                f"occurrence(s)",
                unique[:30],
            )
        # Deliberately narrower than "no C was compiled".  Under the graded
        # `record` mode a compile that went *through* the shim is permitted and
        # already recorded, so it is `compiler-shim-clean`'s finding; this gate
        # only answers whether a back end was reached by a path the ledger cannot
        # see, and passing it means the ledger is complete, not that it is empty.
        return (
            True,
            "no C compiler back end was reached by a path around the shim, so the "
            "ledger is a complete record of what was compiled",
            [],
        )

    def _shipped_objects(self) -> list[tuple[str, Path, elflib.ElfFile]]:
        """Every ELF the submission installs, including archive members."""
        out: list[tuple[str, Path, elflib.ElfFile]] = []
        for config in sorted(self.outcomes):
            art = self.artifacts(config)
            if art is None:
                continue
            for path in (art.library, art.executable):
                if path is None:
                    continue
                if elflib.is_archive(path):
                    try:
                        for name, obj in elflib.archive_objects(path):
                            out.append((f"{config}:{path.name}({name})", path, obj))
                    except elflib.ElfError:
                        continue
                else:
                    obj = self.elf(path)
                    if obj is not None:
                        out.append((f"{config}:{path.name}", path, obj))
        return out

    def gate_elf_rust_provenance(self) -> tuple[bool, str, list[str]]:
        """The shipped binaries must show they came from rustc.

        Several independent signals are accepted, because a submission may
        legitimately strip symbols or configure LTO in ways that erase any one of
        them.  Requiring a specific marker would fail honest work; requiring at
        least one keeps the claim meaningful.
        """
        objects = self._shipped_objects()
        if not objects:
            return False, "no shipped ELF objects to inspect; the build produced none", []
        evidence: list[str] = []
        without: list[str] = []
        for label, _path, obj in objects:
            signals: list[str] = []
            comments = " ".join(obj.strings_in(".comment"))
            if "rustc" in comments:
                signals.append("rustc in .comment")
            if any(name.startswith((".rustc", ".rmeta")) for name in obj.section_names):
                signals.append("rust metadata section")
            mangled = [
                s.name for s in obj.symtab
                if s.name.startswith("_ZN") and re.search(r"17h[0-9a-f]{16}E$", s.name)
            ]
            if mangled:
                signals.append(f"{len(mangled)} rust-mangled symbols")
            if any(
                s.name in ("rust_eh_personality", "__rust_alloc", "__rust_dealloc")
                for s in obj.symtab
            ):
                signals.append("rust runtime symbols")
            if signals:
                evidence.append(f"{label}: {', '.join(signals)}")
            else:
                without.append(label)
        if not evidence:
            return (
                False,
                "none of the shipped objects carry any Rust provenance marker "
                "(.comment producer, metadata section, mangled symbols or runtime "
                "symbols)",
                without[:20],
            )
        return True, f"{len(evidence)} shipped object(s) carry Rust provenance", evidence[:20]

    def gate_no_c_provenance(self) -> tuple[bool, str, list[str]]:
        """No shipped object may contain a translation unit from State A's C.

        A C compile records its source file as an STT_FILE symbol, and a static
        archive records it in the member name.  Both survive an ordinary release
        build, so a library that still contains `blocks.c` or `inlines.c` says so
        in its own symbol table.  Only basenames that State A actually shipped
        are treated as evidence -- the C runtime's own `crtstuff.c` is linked into
        every binary including a pure Rust one and proves nothing.
        """
        baseline = self.baseline_c_basenames()
        if not baseline:
            return False, "the baseline C sources are unavailable to compare against", []
        offenders: list[str] = []
        for label, path, obj in self._shipped_objects():
            for sym in obj.symtab:
                if sym.type != 4:  # STT_FILE
                    continue
                if sym.name in baseline:
                    offenders.append(f"{label}: translation unit {sym.name}")
            if "(" in label:
                member = label[label.index("(") + 1 : -1]
                stem = member[:-2] if member.endswith(".o") else member
                if stem in baseline:
                    offenders.append(f"{label}: archive member is {stem}")
        if offenders:
            unique = sorted(set(offenders))
            return (
                False,
                f"the shipped artifacts still contain {len(unique)} C translation "
                f"unit(s) from State A",
                unique[:30],
            )
        return True, "no State A translation unit appears in the shipped artifacts", []

    def gate_needed_whitelist(self) -> tuple[bool, str, list[str]]:
        allowed = set(self.contract["abi_contract"]["allowed_needed_libraries"])
        # The shared-mode executable links against the library it ships with, so
        # its own SONAME is expected here.  State A does exactly this, which is
        # the point: the release layout being preserved is a requirement, not a
        # violation.
        allowed.add(str(self.contract["abi_contract"]["soname"]))
        offenders: list[str] = []
        for config in sorted(self.outcomes):
            art = self.artifacts(config)
            if art is None:
                continue
            for path in (art.library, art.executable):
                if path is None or elflib.is_archive(path):
                    continue
                obj = self.elf(path)
                if obj is None:
                    continue
                for name in obj.needed:
                    if name not in allowed:
                        offenders.append(f"[{config}] {path.name} NEEDED {name}")
        if offenders:
            return (
                False,
                "a shipped artifact depends on a library outside the platform C "
                "runtime, which is how an outside implementation would be reached",
                sorted(set(offenders))[:20],
            )
        return True, "shipped artifacts depend only on the platform C runtime", []

    def _toolchain_members(self) -> frozenset[str] | None:
        """sha256 of every archive member the installed Rust toolchain ships.

        Read out of the sysroot: each rlib in
        `$(rustc --print sysroot)/lib/rustlib/*/lib` is an `ar` archive, and a member
        of the submission's `libcmark.a` whose bytes equal one of theirs *is* that
        object, put there by `crate-type = ["staticlib"]`.

        Keyed on content and not on the member name, which would have a hole.  Names
        mostly carry a crate metadata hash --
        `std-210854cf1daa4bec.std.9479b33d5e275715-cgu.0.rcgu.o` is not a name a
        submission can invent -- but `lib.rmeta` is in every rlib and identifies no
        crate at all, so an object called `lib.rmeta` would be excused whatever was in
        it.  A digest cannot be forged that way: matching the sysroot's bytes means
        being the sysroot's object, which by construction contains nothing the
        submitter wrote.

        Measured on the graded submission, the two keys agree on exactly which 309
        of `libcmark.a`'s 326 members are the toolchain's -- so the stricter key costs
        nothing on a correct tree.  The remaining 17 are the submission's own crate.

        None means the sysroot could not be read, which the callers report rather
        than resolve in either direction.
        """
        if self._toolchain_member_cache is not _UNSET:
            return self._toolchain_member_cache
        result: frozenset[str] | None = None
        try:
            # build.REAL_PATH, not base_env()'s: rustc lives in /usr/local/cargo/bin,
            # which the default PATH omits.  Without this the lookup fails, the
            # attribution degrades to the fallback, and the degrade is silent apart
            # from one line in the detail -- so the wrong PATH here would read as
            # "the sysroot is unreadable on this machine" forever.
            proc = vlib.run(["rustc", "--print", "sysroot"], timeout=RUN_TIMEOUT,
                            env=vlib.base_env(PATH=REAL_PATH),
                            log=self.log, label="rustc-sysroot")
            sysroot = Path(proc.text().strip())
            if proc.ok and sysroot.is_dir():
                digests: set[str] = set()
                for rlib in sorted(sysroot.glob("lib/rustlib/*/lib/*.rlib")):
                    try:
                        for _name, obj in elflib.archive_objects(rlib):
                            digests.add(vlib.sha256_bytes(obj.data))
                    except elflib.ElfError:
                        continue
                if digests:
                    result = frozenset(digests)
        except Exception:  # noqa: BLE001 -- absence is reported, not raised
            result = None
        self._toolchain_member_cache = result
        return result

    def _imported_symbols(self) -> tuple[list[tuple[str, str]],
                                         list[tuple[str, str]], str | None]:
        """(submission imports, toolchain imports, why attribution failed).

        Both lists are (label, symbol).  The split exists because an archive is not
        a linked image and the two answer different questions.

        A shared object and an executable have been through a link: their undefined
        symbols are what the finished artifact will ask the loader for, so an import
        there is a capability the delivered thing has.  A static archive has been
        through no link at all.  It is a bag of objects, and `libcmark.a` produced
        by `crate-type = ["staticlib"]` contains the whole of `std` -- whose main
        codegen unit references `dlsym` and `execvp` for its own reasons.  Reading
        every member's undefined symbols therefore charged every correct Rust
        submission with dynamic loading and process spawning: measured, that
        failed a check no submission could pass, because the symbols come from the
        toolchain rather than from any line the submitter wrote.

        The member-level reachability closure a linker would compute does not help
        and was tried: seeding from the members defining `cmark_*` and following
        undefined symbols pulled std's cgu.0 in anyway, dragged by a thread-local
        panic helper.  Any Rust staticlib that can panic reaches it.

        So archive members are attributed instead, by content: a member whose bytes
        are a sysroot object's is the toolchain's, reported but not charged, and
        everything else is the submission's and charged exactly as before.  The claim the gates rest on is
        still the linked image, which is measured unconditionally: the graded
        submission's `libcmark.so.0.31.1` and both `cmark` binaries import none of
        these symbols, which is what "this code cannot reach dlopen" actually means.
        A submission that shipped a sysroot object verbatim would be excused here and
        would have gained nothing, since that object contains none of its code; one
        that modified a sysroot object would change its bytes and be charged.
        """
        mine: list[tuple[str, str]] = []
        theirs: list[tuple[str, str]] = []
        toolchain = self._toolchain_members()
        for label, path, obj in self._shipped_objects():
            if not elflib.is_archive(path):
                for name in obj.undefined_symbols():
                    mine.append((label, name))
                continue
            if toolchain is None:
                # Attribution unavailable.  Charging these to the submission is the
                # behaviour being fixed, so the archive is reported and not charged;
                # the linked images above carry the claim, and both link
                # configurations are built from the same source.
                for sym in obj.symtab:
                    if not sym.defined and sym.bind != elflib.STB_LOCAL:
                        theirs.append((label, sym.name))
                continue
            bucket = theirs if vlib.sha256_bytes(obj.data) in toolchain else mine
            for sym in obj.symtab:
                if not sym.defined and sym.bind != elflib.STB_LOCAL:
                    bucket.append((label, sym.name))
        note = None if toolchain is not None else (
            "the Rust sysroot was unreadable, so archive members could not be "
            "attributed and were not charged; the linked images were")
        return mine, theirs, note

    def gate_no_dlopen(self) -> tuple[bool, str, list[str]]:
        """The delivered artifacts must not be able to load code at run time.

        Measured on the shipped ELF's undefined symbols, which is the claim that
        matters: a library that can reach `dlopen` can reach it whether or not the
        word appears in any source file, and one that cannot reach it cannot be made
        to by a comment.

        There is deliberately no source half.  A matcher over `.rs` files fails a
        submission whose comment reads "we deliberately do not dlopen anything" --
        an assertion about the text of the implementation, scored, in the stage that
        measures artifacts.  Stage 1's scan reports those strings advisorily, with
        the path and the line, for a reviewer who can read what surrounds them.

        Static archives are attributed rather than read whole: see
        `_imported_symbols`.  A `dlsym` that arrived inside the toolchain's own copy
        of std is stated in the detail and not charged.
        """
        mine, theirs, note = self._imported_symbols()
        offenders = [
            f"{label}: imports {name}" for label, name in mine
            if name.split("@")[0] in DYNAMIC_LOADING_SYMBOLS
        ]
        if offenders:
            return (
                False,
                "dynamic loading is reachable from the delivered library; the "
                "implementation must be the submission's own code",
                sorted(set(offenders))[:20],
            )
        inherited = sorted({
            f"{label}: imports {name} (Rust toolchain object, not charged)"
            for label, name in theirs
            if name.split("@")[0] in DYNAMIC_LOADING_SYMBOLS
        })
        detail = "no dynamic loading in the delivered artifacts"
        if inherited:
            detail += (f"; {len(inherited)} import(s) come from the toolchain's own "
                       f"objects inside the static archive")
        if note:
            detail += f"; {note}"
        return True, detail, inherited[:20]

    def gate_no_exec_helpers(self) -> tuple[bool, str, list[str]]:
        """Parsing and rendering must happen in the delivered process.

        Same shape as the gate above, and the source half had a legitimate use this
        measurement cannot be confused by: a `build.rs` may run a program at build
        time without the shipped library being able to spawn anything at all.  What is
        checked is whether the installed artifacts import `execve` and its family.

        Attributed the same way, and for the same measured reason: std's cgu.0
        references `execvp`, so an unattributed read of `libcmark.a` failed this gate
        for every Rust submission that ships a static library at all.
        """
        mine, theirs, note = self._imported_symbols()
        offenders = [
            f"{label}: imports {name}" for label, name in mine
            if name.split("@")[0] in PROCESS_SPAWN_SYMBOLS
        ]
        if offenders:
            return (
                False,
                "the delivered artifacts can spawn a helper process, which would "
                "let the real work happen outside the submission",
                sorted(set(offenders))[:20],
            )
        inherited = sorted({
            f"{label}: imports {name} (Rust toolchain object, not charged)"
            for label, name in theirs
            if name.split("@")[0] in PROCESS_SPAWN_SYMBOLS
        })
        detail = "no process spawning in the delivered artifacts"
        if inherited:
            detail += (f"; {len(inherited)} import(s) come from the toolchain's own "
                       f"objects inside the static archive")
        if note:
            detail += f"; {note}"
        return True, detail, inherited[:20]

    # == G4: one implementation, on the default path =======================

    def gate_single_implementation(self) -> tuple[bool, str, list[str]]:
        """Exactly one cmark library lands in each install prefix.

        The install is the whole of the claim here.  A submission that ships two
        engines and installs both has widened what downstream can link against, and
        that is visible in a directory listing of `lib/`.

        The other half of this gate was a regex over cargo `[features]` *names*,
        failing any name matching `c|legacy|native|ffi|orig|old|fallback|reference` as
        a word.  `ffi` is what an honest port calls the module holding its
        `extern "C"` layer, so the gate failed correct work for its choice of
        identifier -- and a feature name is at best a hint about which implementation
        a downstream packager ends up with, which is `default-path`'s question and is
        answered by reading what the feature guards.  Stage 1 reports the names, and
        deliberately still reports `ffi`: a reviewer dismisses it in one look, and a
        `legacy_c` feature nobody mentions is the worse failure.
        """
        offenders: list[str] = []
        for config in sorted(self.outcomes):
            art = self.artifacts(config)
            if art is None:
                continue
            libdir = art.prefix / "lib"
            if not libdir.is_dir():
                continue
            libs = [
                p for p in libdir.iterdir()
                if p.is_file() and not p.is_symlink()
                and (elflib.is_elf(p) or elflib.is_archive(p))
            ]
            if len(libs) > 1:
                offenders.append(
                    f"[{config}] install/lib holds {len(libs)} libraries: "
                    f"{sorted(p.name for p in libs)}"
                )
        if offenders:
            return (
                False,
                "more than one library is installed, so downstream still gets to "
                "choose an implementation",
                offenders[:20],
            )
        return True, "one library per install prefix", []

    def gate_no_env_dispatch(self) -> tuple[bool, str, list[str]]:
        """Behavior must not change with the environment.

        Run the installed CLI on a fixed document under a clean environment and
        again under an environment stuffed with names a submission might key on.
        Identical output is required: a library that renders differently when
        `CMARK_IMPL` or `NDEBUG` is set has kept a switch the grader could be on
        the wrong side of.
        """
        document = b"# t\n\nA *b* [c](/d) `e`\n\n- x\n- y\n\n> q\n"
        hostile = {
            "CMARK_IMPL": "c", "CMARK_BACKEND": "legacy", "CMARK_ENGINE": "reference",
            "CMARK_MODE": "compat", "CMARK_LEGACY": "1", "CMARK_RUST": "0",
            "CMARK_FALLBACK": "1", "CMARK_TEST": "1", "TESTING": "1",
            "NDEBUG": "1", "CI": "1", "SWEREFACTOR": "1", "VERIFIER": "1",
        }
        offenders: list[str] = []
        for config in sorted(self.outcomes):
            art = self.artifacts(config)
            if art is None or art.executable is None:
                continue
            libdir = str(art.prefix / "lib")
            base = vlib.run(
                [str(art.executable)],
                env=vlib.base_env(LD_LIBRARY_PATH=libdir),
                stdin_data=document,
                timeout=RUN_TIMEOUT,
                full_capture=True,
            )
            if not base.ok:
                offenders.append(f"[{config}] the baseline run failed rc={base.returncode}")
                continue
            for name, value in sorted(hostile.items()):
                env = vlib.base_env(LD_LIBRARY_PATH=libdir)
                env[name] = value
                other = vlib.run(
                    [str(art.executable)],
                    env=env,
                    stdin_data=document,
                    timeout=RUN_TIMEOUT,
                    full_capture=True,
                )
                if other.stdout != base.stdout or other.returncode != base.returncode:
                    offenders.append(
                        f"[{config}] output changes when {name}={value} is set"
                    )
        if offenders:
            return False, "behavior depends on the environment", offenders[:20]
        return True, "behavior is identical under every probed environment", []
    def gate_version_unchanged(self) -> tuple[bool, str, list[str]]:
        """Every place the version appears must still say 0.31.1.

        A migration is not a release.  Downstream pins, distribution packages
        and `pkg-config --modversion` all read this number, and bumping it to
        signal "now in Rust" would silently invalidate every one of them.  Four
        independent surfaces are checked because they are produced by different
        parts of the build and can disagree.
        """
        want = str(self.contract["product"]["upstream_version"])
        problems: list[str] = []
        # A submission that never installed has no version surface to have
        # changed.  Counting that as a version change would report a finding the
        # verifier did not observe; the build gates already fail such a
        # submission on the grounds that actually apply.
        measured = 0
        for config in sorted(self.outcomes):
            prefix = self.reference_prefixes.get(config)
            art = self.artifacts(config)
            outcome = self.outcomes.get(config)
            if art is None or outcome is None or not outcome.installed:
                continue
            measured += 1
            header = art.prefix / "include" / "cmark_version.h"
            ref_header = (
                prefix / "include" / "cmark_version.h" if prefix else None
            )
            if not header.is_file():
                problems.append(f"[{config}] include/cmark_version.h is missing")
            elif ref_header is not None and ref_header.is_file():
                # Compared against the reference header rather than against macro
                # names written here: the release defines whichever macros it
                # defines, and an expectation restated in the verifier would be a
                # second definition of the version free to drift from the first.
                want_text = read_text(ref_header)
                got_text = read_text(header)
                if got_text != want_text:
                    problems.append(
                        f"[{config}] cmark_version.h differs from the reference:\n"
                        + vlib.unified_diff(
                            want_text.encode(), got_text.encode(), limit=12
                        )
                    )
                if f'"{want}"' not in got_text:
                    problems.append(
                        f"[{config}] cmark_version.h does not contain the string "
                        f'"{want}"'
                    )
            pc = art.prefix / "lib" / "pkgconfig" / "libcmark.pc"
            if pc.is_file():
                found = re.search(r"(?mi)^Version:\s*(\S+)", read_text(pc))
                if found is None or found.group(1) != want:
                    got = found.group(1) if found else "absent"
                    problems.append(f"[{config}] libcmark.pc Version: {got}")
            else:
                problems.append(f"[{config}] lib/pkgconfig/libcmark.pc is missing")
            if art.executable is not None:
                res = vlib.run([str(art.executable), "--version"], timeout=RUN_TIMEOUT,
                               log=self.log, label=f"version-{config}")
                text = (res.stdout + res.stderr).decode("utf-8", "replace")
                if want not in text:
                    problems.append(
                        f"[{config}] cmark --version prints {text.strip()[:60]!r}"
                    )
                if prefix is not None:
                    ref_cli = prefix / "bin" / "cmark"
                    if ref_cli.is_file():
                        ref = vlib.run([str(ref_cli), "--version"], timeout=RUN_TIMEOUT)
                        if res.stdout != ref.stdout:
                            problems.append(
                                f"[{config}] --version output differs from the "
                                f"reference byte-for-byte"
                            )
        if problems:
            return False, f"the version surface changed (expected {want})", problems[:20]
        if measured == 0:
            return (
                True,
                "no installed version surface to check: nothing was installed",
                ["no configuration installed, so the version was not measurable"],
            )
        return True, f"version {want} reported consistently", []

    def gate_no_abi_widening(self) -> tuple[bool, str, list[str]]:
        """The public surface may not grow.

        The behavioural suite checks that the 70 documented entry points are all
        present; this checks the other direction.  Adding `cmark_parse_rust` or
        exporting an internal helper creates a symbol downstream code can bind
        to, which the project would then owe compatibility for -- a migration
        that quietly widens the ABI has changed the product, not ported it.
        Both the binary and the installed header are checked, since a
        declaration in the header is a promise even before anything calls it.
        """
        problems: list[str] = []
        for config in sorted(self.outcomes):
            art = self.artifacts(config)
            ref_prefix = self.reference_prefixes.get(config)
            if art is None or ref_prefix is None:
                continue
            if config == "shared" and art.library is not None:
                obj = self.elf(art.library)
                ref_obj = self.elf(ref_prefix / "lib" / "libcmark.so.0.31.1")
                if obj is not None and ref_obj is not None:
                    have = {s.name for s in obj.exported_symbols()}
                    expected = {s.name for s in ref_obj.exported_symbols()}
                    for name in sorted(have - expected):
                        problems.append(f"[{config}] new export {name}")
            ref_header = ref_prefix / "include" / "cmark.h"
            header = art.prefix / "include" / "cmark.h"
            if header.is_file() and ref_header.is_file():
                pattern = re.compile(r"\bcmark_[A-Za-z0-9_]*")
                have = set(pattern.findall(read_text(header)))
                expected = set(pattern.findall(read_text(ref_header)))
                for name in sorted(have - expected):
                    problems.append(f"[{config}] cmark.h introduces {name}")
        if problems:
            return (
                False,
                f"{len(problems)} addition(s) to the public surface; the release "
                f"contract is the pinned surface, not a superset of it",
                problems[:25],
            )
        return True, "the public surface matches the reference exactly", []

    # Every output format the CLI can produce, because a lookup table keyed on the
    # input would be equally absent for all six and a partial reimplementation is
    # most likely to diverge in the ones nobody reads: `man` and `latex` escape
    # differently from `html`, and `commonmark` round-trips through the renderer
    # that the parser feeds.
    NOVEL_FORMATS = (
        ("html", ["-t", "html"]),
        ("html-smart", ["-t", "html", "--smart"]),
        ("xml", ["-t", "xml"]),
        ("commonmark", ["-t", "commonmark"]),
        ("latex", ["-t", "latex"]),
        ("man", ["-t", "man"]),
    )

    def gate_no_corpus_answers(self) -> tuple[bool, str, list[str]]:
        """Answers must be computed, not recalled.

        The graded corpus is fixed and lives in the image, so in principle a
        submission could map input digests to stored outputs and never parse
        anything.  Pattern-matching for that is unreliable, so the test is
        behavioral instead: documents are composed here, at grading time, from a
        seed drawn from the OS, and compared against the reference byte-for-byte
        in every output format.  A real parser cannot tell these from the corpus;
        a lookup table has never seen them.

        This is the one gate whose input the task author does not know either,
        which is what makes it worth having.
        """
        import corpus as corpus_mod

        seed = f"verify-novel/{os.urandom(16).hex()}"
        documents = corpus_mod.compose_documents(24, seed)
        extra = [
            ("novel-thematic", "***\n\ntext\n\n___\n"),
            ("novel-tightlist", "- a\n- b\n\n- c\n"),
            ("novel-linkref", "[a]: /u \"t\"\n\nsee [a] and [a][]\n"),
            ("novel-entity", "&amp;&#x41;&copy; &nosuch;\n"),
            ("novel-tabs", "\tcode\n\n-\tx\n\n>\ty\n"),
            ("novel-html", "<div>\n*x*\n</div>\n\n<span>*y*</span>\n"),
        ]
        cases = documents + extra
        problems: list[str] = []
        checked = 0
        for config in sorted(self.outcomes):
            art = self.artifacts(config)
            ref_prefix = self.reference_prefixes.get(config)
            if art is None or art.executable is None or ref_prefix is None:
                continue
            ref_cli = ref_prefix / "bin" / "cmark"
            if not ref_cli.is_file():
                continue
            env = vlib.base_env(LD_LIBRARY_PATH=str(art.prefix / "lib"))
            ref_env = vlib.base_env(LD_LIBRARY_PATH=str(ref_prefix / "lib"))
            for name, text in cases:
                payload = text.encode("utf-8")
                for fmt, flags in self.NOVEL_FORMATS:
                    got = vlib.run([str(art.executable), *flags], env=env,
                                   stdin_data=payload, timeout=RUN_TIMEOUT,
                                   full_capture=True)
                    want = vlib.run([str(ref_cli), *flags], env=ref_env,
                                    stdin_data=payload, timeout=RUN_TIMEOUT,
                                    full_capture=True)
                    checked += 1
                    if got.stdout == want.stdout and got.returncode == want.returncode:
                        continue
                    keep = self.scratch / f"novel-{config}-{name}-{fmt}.md"
                    keep.write_bytes(payload)
                    problems.append(
                        f"[{config}] {name} -t {fmt}: differs from the reference "
                        f"on a document generated at grading time "
                        f"(rc {got.returncode} vs {want.returncode}, "
                        f"{len(got.stdout)} vs {len(want.stdout)} bytes); "
                        f"input saved to {keep}"
                    )
                    if len(problems) >= 12:
                        break
                if len(problems) >= 12:
                    break
        if problems:
            # No source-level corroboration is appended here -- an `include_str!`, a
            # sha256, a DefaultHasher in a .rs file are stage 1's readings.  They
            # were never part of this measurement: this gate composes documents at
            # grading time from `os.urandom(16)` and diffs six output formats against
            # the reference, so a submission that fails it has failed on output nobody
            # could have stored in advance, and grep hits about where to look next
            # belong with the reviewer who can go and look.
            return (
                False,
                "the implementation does not reproduce the reference on unseen "
                "input, so whatever it does on the graded corpus is not parsing",
                problems[:20],
            )
        if not checked:
            return False, "no runnable artifact to test on novel input", []
        return (
            True,
            f"{checked} novel input/format pairs match the reference exactly "
            f"(seed {seed})",
            [],
        )
