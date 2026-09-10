#!/usr/bin/env python3
"""Structural verification: the build, the install tree, the ABI, the package.

A compatibility rewrite is not finished when the new code produces the right
bytes.  It is finished when what a distributor ships is still what downstreams
consume: the same SONAME, the same 70 exported symbols, the same headers, the
same pkg-config and CMake package files, and a C program written against State A
that still compiles and links against the result without being touched.

Every check here is a question a packager would ask, and each is scored on its
own so a submission that gets the library right but drops the CMake package file
loses only that case.  Checks read the *installed* tree rather than the build
directory, because the install tree is the release.

The expected ABI is not transcribed: it is read from the reference install built
in this image from the pinned C sources.  The count in the contract is asserted
against that reading, so a drift in either is caught rather than absorbed.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import elflib
import vlib
from build import BuildOutcome, compile_probe
from vlib import CaseOutcome, Log

CONSUMER_TIMEOUT = 300.0
RUN_TIMEOUT = 60.0

INSTALL_ALLOWED_EXTRA = {"lib/libcmark.so.0.31"}

SOURCE_LEAK_SUFFIXES = (
    ".rs", ".c", ".h.in", ".o", ".obj", ".rlib", ".rmeta", ".d",
    ".toml", ".lock", ".py", ".sh", ".cmake.in",
)

# The reference does not create an intermediate SONAME symlink, but a Rust build
# that produces the full libcmark.so -> .so.0.31 -> .so.0.31.1 chain is shipping
# a superset a distributor would accept rather than a second implementation.
INSTALL_ALLOWED_EXTRA_NOTE = "intermediate SONAME symlink only"


# A downstream consumer, written against the State A header and never modified.
# It is compiled against the reference install and against the submission, and
# the two runs must agree byte for byte -- so this doubles as an API-level
# differential test that goes through the real installed header and library.
CONSUMER_SRC = r"""
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <cmark.h>

static void emit(const char *label, char *text) {
  if (text == NULL) {
    printf("%s=<null>\n", label);
    return;
  }
  printf("%s=%s", label, text);
  printf("--end-%s--\n", label);
  free(text);
}

int main(void) {
  const char *md = "# Heading\n\nA *paragraph* with `code`, a [link](/u), and\n"
                   "a list:\n\n- one\n- two\n\n> quote\n\n    indented\n";
  size_t len = strlen(md);

  emit("html", cmark_markdown_to_html(md, len, CMARK_OPT_DEFAULT));
  emit("html_safe", cmark_markdown_to_html(md, len, CMARK_OPT_SAFE));

  cmark_node *doc = cmark_parse_document(md, len, CMARK_OPT_DEFAULT);
  if (doc == NULL) {
    fprintf(stderr, "parse returned NULL\n");
    return 2;
  }
  printf("root=%s\n", cmark_node_get_type_string(doc));
  emit("xml", cmark_render_xml(doc, CMARK_OPT_DEFAULT));
  emit("man", cmark_render_man(doc, CMARK_OPT_DEFAULT, 72));
  emit("latex", cmark_render_latex(doc, CMARK_OPT_DEFAULT, 72));
  emit("cm", cmark_render_commonmark(doc, CMARK_OPT_DEFAULT, 72));

  int children = 0;
  for (cmark_node *c = cmark_node_first_child(doc); c != NULL;
       c = cmark_node_next(c)) {
    printf("child%d=%s\n", children++, cmark_node_get_type_string(c));
  }
  cmark_node_free(doc);

  printf("version=%d\n", cmark_version());
  printf("version_string=%s\n", cmark_version_string());
  return 0;
}
"""

# Compile-time assertions on the installed version macros.  A consumer that
# guards on CMARK_VERSION must still compile, which constrains cmark_version.h
# to keep real preprocessor constants rather than opaque function calls.
VERSION_GUARD_SRC = r"""
#include <stdio.h>
#include <cmark.h>
#include <cmark_version.h>

#if !defined(CMARK_VERSION)
#error "CMARK_VERSION is not defined"
#endif
#if !defined(CMARK_VERSION_STRING)
#error "CMARK_VERSION_STRING is not defined"
#endif
#if CMARK_VERSION < ((0 << 16) | (30 << 8) | 0)
#error "CMARK_VERSION older than the pinned release"
#endif

/* The macro has to be usable in a constant expression, not just in #if. */
static const int kVersion = CMARK_VERSION;
static const char kVersionString[] = CMARK_VERSION_STRING;

int main(void) {
  printf("macro=%d\n", kVersion);
  printf("macro_string=%s\n", kVersionString);
  printf("runtime=%d\n", cmark_version());
  printf("runtime_string=%s\n", cmark_version_string());
  return (kVersion == cmark_version()) ? 0 : 3;
}
"""

CONSUMER_CMAKELISTS = """cmake_minimum_required(VERSION 3.16)
project(cmark_consumer C)
set(CMAKE_C_STANDARD 11)
find_package(cmark REQUIRED CONFIG)
add_executable(consumer consumer.c)
target_link_libraries(consumer PRIVATE cmark::cmark)
"""

@dataclass
class RefInfo:
    """What the reference install says the release looks like."""

    prefix_shared: Path
    prefix_static: Path
    exported: list[str]
    soname: str
    consumer_output: dict[str, bytes]


class StructureEvaluator:
    """Runs every declared structural case against one submission build."""

    def __init__(
        self,
        repo: Path,
        outcomes: dict[str, BuildOutcome],
        reference: RefInfo,
        contract: dict,
        scratch: Path,
        probe_src: Path,
        log: Log,
    ) -> None:
        self.repo = repo
        self.outcomes = outcomes
        self.reference = reference
        self.contract = contract
        self.scratch = scratch
        self.probe_src = probe_src
        self.log = log
        self._elf_cache: dict[Path, elflib.ElfFile | None] = {}
        self._consumer_cache: dict[tuple[str, str], tuple[bool, str]] = {}
        scratch.mkdir(parents=True, exist_ok=True)

    # -- plumbing --------------------------------------------------------

    def prefix(self, config: str) -> Path:
        return self.outcomes[config].prefix

    def outcome(self, config: str) -> BuildOutcome:
        return self.outcomes[config]

    def elf(self, path: Path) -> elflib.ElfFile | None:
        if path not in self._elf_cache:
            try:
                self._elf_cache[path] = elflib.load(path)
            except (OSError, elflib.ElfError):
                self._elf_cache[path] = None
        return self._elf_cache[path]

    def libfile(self, config: str) -> Path:
        lib = self.prefix(config) / "lib"
        return lib / ("libcmark.so.0.31.1" if config == "shared" else "libcmark.a")

    def evaluate(self, cases: list[dict]) -> list[CaseOutcome]:
        results: list[CaseOutcome] = []
        for case in cases:
            check = case["check"]
            configs = case.get("configs") or ["shared"]
            config = configs[0]
            # A case declared for both link modes is graded on both; it passes
            # only if every mode passes, and the detail names the mode that
            # failed so the report stays actionable.
            handler = getattr(self, f"check_{check.replace('-', '_')}", None)
            start = vlib.now()
            if handler is None:
                passed, detail = False, f"no handler for check '{check}'"
            else:
                passed, detail = self._run_all_configs(handler, configs)
            results.append(
                CaseOutcome(
                    case_id=case["id"],
                    family=case["family"],
                    kind="struct",
                    passed=passed,
                    weight=float(case.get("weight", 1.0)),
                    detail=detail,
                    duration=vlib.now() - start,
                )
            )
        return results

    def _run_all_configs(self, handler, configs: list[str]) -> tuple[bool, str]:
        details: list[str] = []
        ok = True
        for config in configs:
            if config not in self.outcomes:
                return False, f"[{config}] configuration was not built"
            try:
                passed, detail = handler(config)
            except Exception as exc:  # a check must never abort the suite
                passed, detail = False, f"{type(exc).__name__}: {exc}"
            if not passed:
                ok = False
                details.append(f"[{config}] {detail}")
            elif detail:
                details.append(f"[{config}] {detail}")
        return ok, "; ".join(details)

    # -- build -----------------------------------------------------------

    def check_configure(self, config: str) -> tuple[bool, str]:
        result = self.outcome(config).configure
        if result is None:
            return False, "configure was never run"
        if result.ok:
            return True, ""
        return False, f"cmake configure failed rc={result.returncode}: {result.tail()}"

    def check_compile(self, config: str) -> tuple[bool, str]:
        result = self.outcome(config).compile
        if result is None:
            return False, "build was not reached (configure failed)"
        if result.ok:
            return True, ""
        return False, f"build failed rc={result.returncode}: {result.tail()}"

    def check_install(self, config: str) -> tuple[bool, str]:
        result = self.outcome(config).install
        if result is None:
            return False, "install was not reached"
        if result.ok:
            return True, ""
        return False, f"install failed rc={result.returncode}: {result.tail()}"

    def check_build_warnings(self, config: str) -> tuple[bool, str]:
        """The build log must be free of hard errors and link-level complaints.

        Ordinary compiler warnings are not graded -- a rewrite is allowed to be
        noisy.  What is graded is evidence that something did not actually
        resolve: undefined references, missing symbols, or a linker warning that
        a real distributor would treat as a broken build.
        """
        result = self.outcome(config).compile
        if result is None or not result.ok:
            return False, "no successful build log to inspect"
        text = (result.stdout + b"\n" + result.stderr).decode("utf-8", "replace")
        needles = (
            "undefined reference",
            "undefined symbol",
            "cannot find -l",
            "DSO missing from command line",
            "multiple definition of",
            "relocation truncated",
        )
        hits = [n for n in needles if n in text]
        if hits:
            return False, f"build log reports {hits}"
        return True, ""

    def check_insource_refused(self, config: str) -> tuple[bool, str]:
        result = self.outcome(config).extra.get("insource")
        if result is None:
            return False, "in-source probe did not run"
        if result.ok:
            return False, (
                "an in-source `cmake .` succeeded; upstream refuses it to protect "
                "the user's tree and that guard must survive the migration"
            )
        return True, ""

    def check_reconfigure(self, config: str) -> tuple[bool, str]:
        result = self.outcome(config).extra.get("reconfigure")
        if result is None:
            return False, "reconfigure probe did not run"
        if not result.ok:
            return False, f"second configure failed rc={result.returncode}: {result.tail()}"
        return True, ""

    def check_rebuild(self, config: str) -> tuple[bool, str]:
        result = self.outcome(config).extra.get("rebuild")
        if result is None:
            return False, "rebuild probe did not run"
        if not result.ok:
            return False, f"rebuild failed rc={result.returncode}: {result.tail()}"
        return True, ""

    #: A rule head in a generated `build.make`: a target, then `:`, but not the
    #: `:=` of an assignment.
    _RULE_HEAD = re.compile(r"^([^\s:#][^:=]*?):(?!=)")

    @staticmethod
    def _recipe_outputs(build_make: Path) -> set[str]:
        """Outputs this makefile carries a *recipe* for, not a dependency on.

        The distinction is the whole check: every target that depends on an
        output mentions it, and only the one that builds it follows the rule with
        a command.  Object files are skipped -- their path contains the target
        directory that owns them, so they cannot be duplicated across targets and
        counting them would put 90 entries of noise in front of the 5 that matter.
        """
        found: set[str] = set()
        try:
            lines = build_make.read_text(encoding="utf-8",
                                        errors="replace").splitlines()
        except OSError:
            return found
        for index, line in enumerate(lines):
            head = StructureEvaluator._RULE_HEAD.match(line)
            if not head:
                continue
            target = head.group(1).strip()
            if target.startswith(".") or "/CMakeFiles/" in target:
                continue
            for follow in lines[index + 1:]:
                if follow.startswith("\t"):
                    found.add(target)
                    break
                if follow.strip():
                    break
        return found

    @staticmethod
    def _ordered_target_pairs(makefile2: Path) -> set[frozenset[str]]:
        """Target-directory pairs `make` will not run at the same time."""
        pairs: set[frozenset[str]] = set()
        try:
            text = makefile2.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return pairs
        for line in text.splitlines():
            if line.startswith("\t") or "/all:" not in line:
                continue
            left, _, right = line.partition(":")
            lhs = left.strip().removesuffix("/all")
            if not lhs.endswith(".dir"):
                continue
            for dep in right.split():
                dep = dep.strip().removesuffix("/all")
                if dep.endswith(".dir"):
                    pairs.add(frozenset((lhs, dep)))
        return pairs

    @staticmethod
    def parallel_clashes(build_dir: Path) -> list[str]:
        """Outputs with a recipe in two target makefiles nothing orders.

        Separate from the check so the build-time assertion on State A can call
        this instead of reimplementing it.  A second copy would be free to agree
        with a broken original, which is the whole value of asserting anything.
        """
        ordered = StructureEvaluator._ordered_target_pairs(
            build_dir / "CMakeFiles" / "Makefile2")
        owners: dict[str, list[str]] = {}
        for build_make in build_dir.rglob("CMakeFiles/*.dir/build.make"):
            owner = str(build_make.parent.relative_to(build_dir))
            for target in StructureEvaluator._recipe_outputs(build_make):
                owners.setdefault(target, []).append(owner)
        clashes: list[str] = []
        for target, dirs in sorted(owners.items()):
            for i, first in enumerate(dirs):
                for second in dirs[i + 1:]:
                    if frozenset((first, second)) in ordered:
                        continue
                    clashes.append(f"{target} (built by {first} and {second})")
        return clashes

    def check_parallel_safe(self, config: str) -> tuple[bool, str]:
        """No output may be built by two targets that nothing orders.

        Static, and that is the point.  The condition it describes is a race, and
        a check that reproduced the race would pass the defect most of the time:
        the tree this was written against failed one build in nine, so a
        behavioural probe would have called it parallel-safe eight times and the
        score would have gone on being a dice roll.  What is not probabilistic is
        the generated makefile -- the duplicate recipe is in it whether or not the
        race is lost, so this reads that instead.

        Naming another custom command's OUTPUT in a second `add_custom_target` is
        the way to write this by accident: cmake copies that command's entire rule
        chain into `T.dir/build.make` rather than making `T` depend on the target
        that owns it.  Measured on a fixture, `DEPENDS` and `SOURCES` both do it
        and both then lose the build -- so the message names both rather than
        sending a reader to whichever one happens to be second on the line.
        `add_dependencies(T owner)` removes the duplicate for either spelling, and
        the same file usually already has one: here `cmark_artifacts` did and
        `cmark_exe_artifacts`, which spells both, did not.

        Reported per duplicated output, since the count is what says whether this
        is one stray `SOURCES` or a build graph assembled that way throughout.
        """
        build_dir = self.outcome(config).build_dir
        makefile2 = build_dir / "CMakeFiles" / "Makefile2"
        if not makefile2.is_file():
            # The graded configure passes no `-G`, so this is the Makefiles
            # generator's layout by construction.  Absent means configure did not
            # finish, which its own check reports; saying "not parallel-safe"
            # here would charge one defect twice.
            return True, ("no CMakeFiles/Makefile2, so the build graph could not "
                          "be read; build/configure reports that")
        clashes = self.parallel_clashes(build_dir)
        if not clashes:
            return True, ""
        return False, (
            "%d output(s) have a build recipe in two target makefiles that "
            "nothing orders against each other, so `make -j` can run both at "
            "once: %s. A second `add_custom_target` naming another custom "
            "command's OUTPUT in `DEPENDS` or `SOURCES` produces this -- cmake "
            "copies the rule rather than ordering the targets. "
            "`add_dependencies` on the target that owns the command is what "
            "orders it instead."
            % (len(clashes), "; ".join(clashes[:6])))

    def check_cmake_version(self, config: str) -> tuple[bool, str]:
        """The project must still declare version 0.31.1 to CMake."""
        cache = self.outcome(config).build_dir / "CMakeCache.txt"
        if not cache.is_file():
            return False, "CMakeCache.txt absent; configure did not complete"
        text = cache.read_text(encoding="utf-8", errors="replace")
        wanted = "0.31.1"
        for line in text.splitlines():
            if line.startswith("CMAKE_PROJECT_VERSION:") and line.endswith(wanted):
                return True, ""
        found = [
            line for line in text.splitlines()
            if line.startswith("CMAKE_PROJECT_VERSION:")
        ]
        return False, f"expected project version {wanted}, cache says {found or '<unset>'}"

    # -- install inventory -----------------------------------------------

    def check_inv_lib_shared(self, config: str) -> tuple[bool, str]:
        lib = self.prefix(config) / "lib"
        real = lib / "libcmark.so.0.31.1"
        if not real.is_file():
            present = sorted(p.name for p in lib.glob("libcmark*")) if lib.is_dir() else []
            return False, f"lib/libcmark.so.0.31.1 missing; lib/ holds {present}"
        obj = self.elf(real)
        if obj is None:
            return False, "libcmark.so.0.31.1 is not a readable ELF object"
        if not obj.is_shared_object:
            return False, f"expected a shared object, ELF type is {elflib.ET_NAMES.get(obj.type)}"
        if obj.soname != "libcmark.so.0.31.1":
            return False, f"SONAME is {obj.soname!r}, expected 'libcmark.so.0.31.1'"
        return True, ""

    def check_inv_lib_static(self, config: str) -> tuple[bool, str]:
        archive = self.prefix(config) / "lib" / "libcmark.a"
        if not archive.is_file():
            return False, "lib/libcmark.a missing from the static install"
        if not elflib.is_archive(archive):
            return False, "lib/libcmark.a is not an ar archive"
        members = elflib.archive_objects(archive)
        if not members:
            return False, "lib/libcmark.a contains no ELF objects"
        return True, f"{len(members)} object(s)"

    def check_inv_exe(self, config: str) -> tuple[bool, str]:
        exe = self.prefix(config) / "bin" / "cmark"
        if not exe.is_file():
            return False, "bin/cmark missing"
        if not os.access(exe, os.X_OK):
            return False, "bin/cmark is not executable"
        result = self._run_installed(exe, ["--version"], config)
        if not result.ok:
            return False, f"bin/cmark --version failed rc={result.returncode}: {result.tail()}"
        if b"cmark 0.31.1" not in result.stdout:
            return False, f"--version printed {result.stdout[:120]!r}"
        return True, ""

    def _run_installed(self, exe: Path, argv: list[str], config: str):
        """Run an installed executable with the install tree's lib on the path."""
        libdir = self.prefix(config) / "lib"
        return vlib.run(
            [str(exe), *argv],
            env=vlib.base_env(LD_LIBRARY_PATH=str(libdir)),
            timeout=RUN_TIMEOUT,
        )

    def check_inv_header_cmark(self, config: str) -> tuple[bool, str]:
        header = self.prefix(config) / "include" / "cmark.h"
        if not header.is_file():
            return False, "include/cmark.h missing"
        text = header.read_text(encoding="utf-8", errors="replace")
        # The installed header is the ABI contract.  Spot-check that it still
        # declares the surface rather than having become a stub that includes
        # something else.
        required = (
            "cmark_markdown_to_html",
            "cmark_parse_document",
            "cmark_node_new",
            "cmark_render_html",
            "CMARK_OPT_DEFAULT",
            "cmark_mem",
        )
        missing = [name for name in required if name not in text]
        if missing:
            return False, f"installed cmark.h does not declare {missing}"
        return True, f"{len(text)} bytes"

    def check_inv_header_export(self, config: str) -> tuple[bool, str]:
        header = self.prefix(config) / "include" / "cmark_export.h"
        if not header.is_file():
            return False, "include/cmark_export.h missing"
        text = header.read_text(encoding="utf-8", errors="replace")
        if "CMARK_EXPORT" not in text:
            return False, "cmark_export.h does not define CMARK_EXPORT"
        if "CMARK_NO_EXPORT" not in text:
            return False, "cmark_export.h does not define CMARK_NO_EXPORT"
        return True, ""

    def check_inv_header_version(self, config: str) -> tuple[bool, str]:
        header = self.prefix(config) / "include" / "cmark_version.h"
        if not header.is_file():
            return False, "include/cmark_version.h missing"
        text = header.read_text(encoding="utf-8", errors="replace")
        if "CMARK_VERSION" not in text:
            return False, "cmark_version.h does not define CMARK_VERSION"
        if '"0.31.1"' not in text:
            return False, f"CMARK_VERSION_STRING is not \"0.31.1\": {text[:200]!r}"
        return True, ""

    def check_inv_man(self, config: str) -> tuple[bool, str]:
        share = self.prefix(config) / "share" / "man"
        wanted = [share / "man1" / "cmark.1", share / "man3" / "cmark.3"]
        missing = [str(p.relative_to(self.prefix(config))) for p in wanted if not p.is_file()]
        if missing:
            return False, f"man pages missing from the install tree: {missing}"
        return True, ""

    def _installed_entries(self, config: str) -> list[str]:
        prefix = self.prefix(config)
        out: list[str] = []
        for root, _dirs, files in os.walk(prefix):
            for name in files:
                path = Path(root) / name
                out.append(str(path.relative_to(prefix)))
            for name in _dirs:
                path = Path(root) / name
                if path.is_symlink():
                    out.append(str(path.relative_to(prefix)))
        return sorted(out)

    def _reference_prefix(self, config: str) -> Path:
        return (
            self.reference.prefix_shared
            if config == "shared"
            else self.reference.prefix_static
        )

    def _expected_entries(self, config: str) -> set[str]:
        """What the reference install contains, plus a narrow allowance.

        Reading the reference tree rather than restating a list keeps this check
        honest: whatever the pinned C release installs is by definition the
        release inventory, so the two can never drift apart.
        """
        ref = self._reference_prefix(config)
        wanted: set[str] = set()
        for root, dirs, files in os.walk(ref):
            for name in list(files) + [d for d in dirs if (Path(root) / d).is_symlink()]:
                wanted.add(str((Path(root) / name).relative_to(ref)))
        return wanted | INSTALL_ALLOWED_EXTRA

    def check_inv_no_extra(self, config: str) -> tuple[bool, str]:
        actual = set(self._installed_entries(config))
        allowed = self._expected_entries(config)
        extra = sorted(actual - allowed)
        if extra:
            return False, (
                f"{len(extra)} entr(ies) the reference release does not install: "
                f"{extra[:12]}"
            )
        missing = sorted(allowed - actual - INSTALL_ALLOWED_EXTRA)
        if missing:
            return False, f"install tree is missing {missing[:12]}"
        return True, f"{len(actual)} entries"

    def check_inv_no_source_leak(self, config: str) -> tuple[bool, str]:
        """No implementation source or intermediate object may be installed.

        Installing the Rust sources or a .rlib is not a compatibility failure but
        it is a packaging failure: the release is a library and its headers, not
        a copy of the tree it was built from.
        """
        offenders: list[str] = []
        for rel in self._installed_entries(config):
            lower = rel.lower()
            if lower.endswith(SOURCE_LEAK_SUFFIXES) and not lower.endswith(".cmake"):
                offenders.append(rel)
        if offenders:
            return False, f"install tree carries build inputs: {sorted(offenders)[:12]}"
        return True, ""

    def check_inv_prefix(self, config: str) -> tuple[bool, str]:
        """`cmake --install` must write only under the requested prefix."""
        result = self.outcome(config).install
        if result is None or not result.ok:
            return False, "no successful install to inspect"
        prefix = str(self.prefix(config))
        stray: list[str] = []
        for line in result.stdout.decode("utf-8", "replace").splitlines():
            line = line.strip()
            for marker in ("-- Installing: ", "-- Up-to-date: "):
                if line.startswith(marker):
                    target = line[len(marker):].strip()
                    if not target.startswith(prefix):
                        stray.append(target)
        if stray:
            return False, f"installed outside the prefix: {stray[:8]}"
        return True, ""

    # -- ABI -------------------------------------------------------------

    def _shared_object(self, config: str) -> tuple[elflib.ElfFile | None, str]:
        path = self.prefix(config) / "lib" / "libcmark.so.0.31.1"
        if not path.is_file():
            return None, "lib/libcmark.so.0.31.1 is absent"
        obj = self.elf(path)
        if obj is None:
            return None, "lib/libcmark.so.0.31.1 is not a readable ELF object"
        return obj, ""

    def check_abi_soname(self, config: str) -> tuple[bool, str]:
        obj, err = self._shared_object(config)
        if obj is None:
            return False, err
        expected = self.reference.soname
        if obj.soname != expected:
            return False, (
                f"SONAME is {obj.soname!r}; the reference release declares "
                f"{expected!r}, and a consumer linked against the old library "
                f"resolves by that name"
            )
        return True, ""

    def check_abi_symbol_count(self, config: str) -> tuple[bool, str]:
        obj, err = self._shared_object(config)
        if obj is None:
            return False, err
        exported = {s.name for s in obj.exported_symbols()}
        expected_count = int(self.contract["abi_contract"]["exported_symbol_count"])
        public = {n for n in exported if n.startswith("cmark_")}
        if len(public) != expected_count:
            return False, (
                f"{len(public)} exported cmark_* symbols, expected {expected_count}"
            )
        return True, f"{len(public)} symbols"

    def check_abi_symbol_names(self, config: str) -> tuple[bool, str]:
        obj, err = self._shared_object(config)
        if obj is None:
            return False, err
        actual = {s.name for s in obj.exported_symbols() if s.name.startswith("cmark_")}
        expected = set(self.reference.exported)
        missing = sorted(expected - actual)
        added = sorted(actual - expected)
        if missing or added:
            return False, (
                f"exported surface differs from the reference: missing={missing[:10]} "
                f"unexpected={added[:10]}"
            )
        return True, ""

    def check_abi_no_extra_exports(self, config: str) -> tuple[bool, str]:
        """Nothing beyond cmark_* may be globally visible.

        A Rust cdylib exports its own runtime scaffolding unless visibility is
        managed deliberately, so this is where an otherwise-working rewrite
        usually leaks: __rust_alloc, rust_eh_personality, or a helper module's
        symbols become part of the library's public surface and collide with
        another Rust library in the same process.
        """
        obj, err = self._shared_object(config)
        if obj is None:
            return False, err
        leaked = [
            s.describe()
            for s in obj.exported_symbols()
            if not s.name.startswith("cmark_")
        ]
        if leaked:
            return False, (
                f"{len(leaked)} non-cmark symbol(s) exported with default "
                f"visibility: {leaked[:12]}"
            )
        return True, ""

    def check_abi_symbol_type(self, config: str) -> tuple[bool, str]:
        obj, err = self._shared_object(config)
        if obj is None:
            return False, err
        wrong = [
            s.describe()
            for s in obj.exported_symbols()
            if s.name.startswith("cmark_") and s.type != elflib.STT_FUNC
        ]
        if wrong:
            return False, (
                f"every cmark_* export is a function in the reference; these are "
                f"not: {wrong[:12]}"
            )
        return True, ""

    def check_abi_elf_class(self, config: str) -> tuple[bool, str]:
        obj, err = self._shared_object(config)
        if obj is None:
            return False, err
        if obj.elf_class != 2 or obj.machine != 62:
            return False, f"expected 64-bit x86-64, got class={obj.elf_class} machine={obj.machine}"
        return True, ""

    def check_abi_needed(self, config: str) -> tuple[bool, str]:
        obj, err = self._shared_object(config)
        if obj is None:
            return False, err
        allowed = set(self.contract["abi_contract"]["allowed_needed_libraries"])
        unexpected = [name for name in obj.needed if name not in allowed]
        if unexpected:
            return False, (
                f"depends on {unexpected}; only the platform C runtime is "
                f"permitted, so a dependency here means an outside "
                f"implementation is being linked or loaded"
            )
        return True, f"NEEDED={obj.needed}"

    def check_abi_no_undefined(self, config: str) -> tuple[bool, str]:
        """Every unresolved symbol must be satisfiable by the NEEDED closure.

        Left unchecked, a library can install and even load while carrying an
        unresolved reference that only fails when the affected code path runs.
        """
        obj, err = self._shared_object(config)
        if obj is None:
            return False, err
        undefined = obj.undefined_symbols()
        if not undefined:
            return True, ""
        available: set[str] = set()
        for soname in obj.needed:
            for libdir in ("/lib/x86_64-linux-gnu", "/usr/lib/x86_64-linux-gnu", "/lib64"):
                candidate = Path(libdir) / soname
                if candidate.is_file():
                    dep = self.elf(candidate)
                    if dep is not None:
                        available |= {s.name for s in dep.exported_symbols()}
                    break
        # Symbols the dynamic linker itself provides, which no NEEDED entry owns.
        available |= {
            "__gmon_start__", "_ITM_deregisterTMCloneTable",
            "_ITM_registerTMCloneTable", "__cxa_finalize", "_Jv_RegisterClasses",
            "__tls_get_addr",
        }
        unresolved = sorted(set(undefined) - available)
        if unresolved:
            return False, f"unresolved symbol(s) outside the NEEDED closure: {unresolved[:12]}"
        return True, f"{len(undefined)} imports, all resolvable"

    def check_abi_static_symbols(self, config: str) -> tuple[bool, str]:
        """The archive must define the same public surface as the shared library."""
        archive = self.prefix(config) / "lib" / "libcmark.a"
        if not archive.is_file():
            return False, "lib/libcmark.a is absent"
        try:
            defined = elflib.archive_defined_symbols(archive)
        except elflib.ElfError as exc:
            return False, str(exc)
        expected = set(self.reference.exported)
        missing = sorted(expected - defined)
        if missing:
            return False, (
                f"{len(missing)} public symbol(s) the archive does not define: "
                f"{missing[:12]}"
            )
        return True, f"{len(expected)} public symbols defined"

    def check_abi_links(self, config: str) -> tuple[bool, str]:
        """The development symlink chain a `-lcmark` link depends on."""
        lib = self.prefix(config) / "lib"
        dev = lib / "libcmark.so"
        if not dev.exists():
            return False, "lib/libcmark.so development symlink is absent"
        if not dev.is_symlink():
            # A real file here is unusual but only a problem if it is not the
            # library itself; a copy still lets -lcmark resolve.
            obj = self.elf(dev)
            if obj is None or obj.soname != self.reference.soname:
                return False, "lib/libcmark.so is neither a symlink nor the library"
            return True, "libcmark.so is a regular file carrying the right SONAME"
        resolved = dev.resolve()
        if not resolved.is_file():
            return False, f"lib/libcmark.so dangles -> {os.readlink(dev)}"
        obj = self.elf(resolved)
        if obj is None:
            return False, f"lib/libcmark.so resolves to a non-ELF file: {resolved.name}"
        if obj.soname != self.reference.soname:
            return False, (
                f"lib/libcmark.so resolves to {resolved.name} whose SONAME is "
                f"{obj.soname!r}"
            )
        return True, f"libcmark.so -> {resolved.name}"

    # -- package metadata -------------------------------------------------

    def _pc_path(self, config: str) -> Path:
        return self.prefix(config) / "lib" / "pkgconfig" / "libcmark.pc"

    def check_pc_exists(self, config: str) -> tuple[bool, str]:
        path = self._pc_path(config)
        if not path.is_file():
            return False, "lib/pkgconfig/libcmark.pc missing"
        return True, ""

    def check_pc_fields(self, config: str) -> tuple[bool, str]:
        path = self._pc_path(config)
        if not path.is_file():
            return False, "lib/pkgconfig/libcmark.pc missing"
        text = path.read_text(encoding="utf-8", errors="replace")
        fields = {}
        for line in text.splitlines():
            if ":" in line and not line.strip().startswith("#"):
                key, _, value = line.partition(":")
                fields[key.strip().lower()] = value.strip()
        problems: list[str] = []
        if fields.get("name", "").lower() != "libcmark":
            problems.append(f"Name={fields.get('name')!r}")
        if fields.get("version") != "0.31.1":
            problems.append(f"Version={fields.get('version')!r}")
        if "-lcmark" not in fields.get("libs", ""):
            problems.append(f"Libs={fields.get('libs')!r} lacks -lcmark")
        if "includedir" not in fields.get("cflags", ""):
            problems.append(f"Cflags={fields.get('cflags')!r} lacks the include dir")
        if not fields.get("description"):
            problems.append("Description is empty")
        if problems:
            return False, f"libcmark.pc fields wrong: {problems}"
        return True, ""

    def check_pc_query(self, config: str) -> tuple[bool, str]:
        """pkg-config must be able to answer for the installed module."""
        pcdir = self.prefix(config) / "lib" / "pkgconfig"
        env = vlib.base_env(PKG_CONFIG_PATH=str(pcdir))
        checks = (
            (["--modversion", "libcmark"], b"0.31.1"),
            (["--cflags", "libcmark"], b"-I"),
            (["--libs", "libcmark"], b"-lcmark"),
        )
        for argv, needle in checks:
            result = vlib.run(["pkg-config", *argv], env=env, timeout=60.0)
            if not result.ok:
                return False, f"pkg-config {' '.join(argv)} failed: {result.tail()}"
            if needle not in result.stdout:
                return False, (
                    f"pkg-config {' '.join(argv)} printed {result.stdout[:120]!r}, "
                    f"expected it to contain {needle!r}"
                )
        return True, ""

    def _cmake_pkg_dir(self, config: str) -> Path:
        return self.prefix(config) / "lib" / "cmake" / "cmark"

    def check_cmake_config(self, config: str) -> tuple[bool, str]:
        path = self._cmake_pkg_dir(config) / "cmark-config.cmake"
        if not path.is_file():
            return False, "lib/cmake/cmark/cmark-config.cmake missing"
        text = path.read_text(encoding="utf-8", errors="replace")
        if "cmark-targets.cmake" not in text:
            return False, "cmark-config.cmake does not include the targets file"
        return True, ""

    def check_cmake_version_file(self, config: str) -> tuple[bool, str]:
        path = self._cmake_pkg_dir(config) / "cmark-config-version.cmake"
        if not path.is_file():
            return False, "lib/cmake/cmark/cmark-config-version.cmake missing"
        text = path.read_text(encoding="utf-8", errors="replace")
        if "0.31.1" not in text:
            return False, "the version file does not carry 0.31.1"
        if "PACKAGE_VERSION_COMPATIBLE" not in text:
            return False, "the version file sets no compatibility policy"
        return True, ""

    def check_cmake_targets(self, config: str) -> tuple[bool, str]:
        path = self._cmake_pkg_dir(config) / "cmark-targets.cmake"
        if not path.is_file():
            return False, "lib/cmake/cmark/cmark-targets.cmake missing"
        text = path.read_text(encoding="utf-8", errors="replace")
        if "cmark::cmark" not in text:
            return False, "the targets file does not define cmark::cmark"
        return True, ""

    def check_cmake_namespace(self, config: str) -> tuple[bool, str]:
        """The imported target must resolve to the installed library file."""
        pkg = self._cmake_pkg_dir(config)
        blob = ""
        for entry in sorted(pkg.glob("cmark-targets*.cmake")):
            blob += entry.read_text(encoding="utf-8", errors="replace")
        if not blob:
            return False, "no cmark-targets*.cmake in the install tree"
        if "add_library(cmark::cmark" not in blob:
            return False, "cmark::cmark is not declared as an imported library"
        expected = "libcmark.so.0.31.1" if config == "shared" else "libcmark.a"
        if expected not in blob:
            return False, f"the imported target does not point at {expected}"
        return True, ""

    def check_static_define(self, config: str) -> tuple[bool, str]:
        """Static consumers need CMARK_STATIC_DEFINE from the imported target.

        Without it the installed cmark_export.h expands CMARK_EXPORT to a
        dllimport-style declaration and a static link produces the wrong
        visibility attributes, so this is a real downstream break rather than a
        cosmetic difference.
        """
        blob = ""
        for entry in sorted(self._cmake_pkg_dir(config).glob("cmark-targets*.cmake")):
            blob += entry.read_text(encoding="utf-8", errors="replace")
        if not blob:
            return False, "no cmark-targets*.cmake in the install tree"
        if "CMARK_STATIC_DEFINE" not in blob:
            return False, (
                "the static package does not put CMARK_STATIC_DEFINE in "
                "INTERFACE_COMPILE_DEFINITIONS"
            )
        return True, ""

    # -- downstream consumers --------------------------------------------

    def _write_consumer(self, name: str, source: str) -> Path:
        directory = self.scratch / name
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "consumer.c"
        path.write_text(source, encoding="utf-8")
        return directory

    def _cc(self, argv: list[str], label: str):
        """Compile with the real toolchain: this is the verifier's own code."""
        return vlib.run(
            argv, env=vlib.base_env(), timeout=CONSUMER_TIMEOUT, log=self.log, label=label
        )

    def _reference_consumer_output(self, key: str) -> bytes | None:
        return self.reference.consumer_output.get(key)

    def check_consumer_pc(self, config: str) -> tuple[bool, str]:
        """Build the unmodified State A consumer using pkg-config flags."""
        prefix = self.prefix(config)
        pcdir = prefix / "lib" / "pkgconfig"
        env = vlib.base_env(PKG_CONFIG_PATH=str(pcdir))
        flags: list[str] = []
        for query in ("--cflags", "--libs"):
            result = vlib.run(
                ["pkg-config", query, "libcmark"], env=env, timeout=60.0
            )
            if not result.ok:
                return False, f"pkg-config {query} failed: {result.tail()}"
            flags.extend(result.text().split())
        directory = self._write_consumer(f"consumer-pc-{config}", CONSUMER_SRC)
        binary = directory / "consumer"
        argv = ["/usr/bin/cc", "-std=c11", "-O1", "-o", str(binary),
                str(directory / "consumer.c"), *flags]
        if config == "shared":
            argv.append(f"-Wl,-rpath,{prefix / 'lib'}")
        compiled = self._cc(argv, f"consumer-pc-{config}")
        if not compiled.ok:
            return False, f"consumer failed to build against the install: {compiled.tail()}"
        return self._compare_consumer(binary, config, "consumer")

    def _compare_consumer(
        self, binary: Path, config: str, key: str
    ) -> tuple[bool, str]:
        """Run a consumer and require it to match the reference byte for byte."""
        result = self._run_installed(binary, [], config)
        if not result.ok:
            return False, (
                f"the consumer built but exited rc={result.returncode}: {result.tail()}"
            )
        expected = self._reference_consumer_output(key)
        if expected is None:
            return True, "no reference output recorded for this consumer"
        if result.stdout != expected:
            return False, (
                "consumer output differs from the reference:\n"
                + vlib.unified_diff(expected, result.stdout, limit=24)
            )
        return True, ""

    def check_consumer_cmake(self, config: str) -> tuple[bool, str]:
        """find_package(cmark) + cmark::cmark must build a working program."""
        prefix = self.prefix(config)
        directory = self._write_consumer(f"consumer-cmake-{config}", CONSUMER_SRC)
        (directory / "CMakeLists.txt").write_text(CONSUMER_CMAKELISTS, encoding="utf-8")
        build_dir = directory / "build"
        configure = vlib.run(
            [
                "cmake", "-S", str(directory), "-B", str(build_dir),
                "-DCMAKE_BUILD_TYPE=Release",
                f"-DCMAKE_PREFIX_PATH={prefix}",
                f"-DCMAKE_BUILD_RPATH={prefix / 'lib'}",
            ],
            env=vlib.base_env(),
            timeout=CONSUMER_TIMEOUT,
            log=self.log,
            label=f"consumer-cmake-configure-{config}",
        )
        if not configure.ok:
            return False, (
                f"find_package(cmark) did not resolve against the install tree: "
                f"{configure.tail()}"
            )
        built = vlib.run(
            ["cmake", "--build", str(build_dir)],
            env=vlib.base_env(),
            timeout=CONSUMER_TIMEOUT,
            log=self.log,
            label=f"consumer-cmake-build-{config}",
        )
        if not built.ok:
            return False, f"the CMake consumer failed to link: {built.tail()}"
        binary = build_dir / "consumer"
        if not binary.is_file():
            return False, "the CMake consumer produced no executable"
        return self._compare_consumer(binary, config, "consumer")

    def check_consumer_manual(self, config: str) -> tuple[bool, str]:
        """Hand-written -I/-L/-lcmark, the way a Makefile in the wild does it."""
        prefix = self.prefix(config)
        directory = self._write_consumer(f"consumer-manual-{config}", CONSUMER_SRC)
        binary = directory / "consumer"
        argv = [
            "/usr/bin/cc", "-std=c11", "-O1", "-o", str(binary),
            str(directory / "consumer.c"),
            f"-I{prefix / 'include'}", f"-L{prefix / 'lib'}", "-lcmark",
            f"-Wl,-rpath,{prefix / 'lib'}",
        ]
        compiled = self._cc(argv, f"consumer-manual-{config}")
        if not compiled.ok:
            return False, f"-lcmark did not resolve: {compiled.tail()}"
        return self._compare_consumer(binary, config, "consumer")

    def check_consumer_version(self, config: str) -> tuple[bool, str]:
        """Compile-time version guards must still compile and agree at runtime."""
        prefix = self.prefix(config)
        directory = self._write_consumer(f"consumer-version-{config}", VERSION_GUARD_SRC)
        binary = directory / "consumer"
        argv = [
            "/usr/bin/cc", "-std=c11", "-O1", "-o", str(binary),
            str(directory / "consumer.c"),
            f"-I{prefix / 'include'}", f"-L{prefix / 'lib'}",
        ]
        if config == "static":
            argv += ["-DCMARK_STATIC_DEFINE", "-l:libcmark.a"]
        else:
            argv += ["-lcmark", f"-Wl,-rpath,{prefix / 'lib'}"]
        compiled = self._cc(argv, f"consumer-version-{config}")
        if not compiled.ok:
            return False, (
                f"a consumer guarding on CMARK_VERSION did not compile: "
                f"{compiled.tail()}"
            )
        return self._compare_consumer(binary, config, "version")

    def check_consumer_probe(self, config: str) -> tuple[bool, str]:
        """The differential probe itself must link against the submission.

        This case is the gate the behavioural suite depends on: if the probe
        cannot be built against the installed header and library, no API-level
        comparison is possible at all, and saying so as its own case makes that
        failure legible instead of appearing as thousands of unrelated errors.
        """
        prefix = self.prefix(config)
        binary = self.scratch / f"probe-{config}"
        result = compile_probe(
            self.probe_src,
            prefix,
            binary,
            self.log,
            label=f"probe-link-{config}",
            static=(config == "static"),
        )
        if not result.ok:
            return False, (
                f"the probe did not build against the {config} install: "
                f"{result.tail()}"
            )
        if not binary.is_file():
            return False, "the probe compile reported success but produced no binary"
        return True, ""


def probe_binary_path(scratch: Path, config: str) -> Path:
    """Where check_consumer_probe leaves the probe the behavioural suite reuses."""
    return scratch / f"probe-{config}"


def build_ref_info(
    prefix_shared: Path, prefix_static: Path, scratch: Path, log: Log
) -> RefInfo:
    """Read the release facts and consumer outputs off the reference install.

    Every expectation used by the structural cases comes from here rather than
    from a literal in this file.  The exported-symbol list, the SONAME and the
    consumer transcripts are all whatever the pinned C release actually produces,
    so there is no second definition of the release to drift out of sync with it.

    The consumer is compiled against the reference for the same reason it is
    compiled against the submission: its output is the comparison, and running
    the identical program against both installs is what makes the comparison
    mean something.
    """
    library = prefix_shared / "lib" / "libcmark.so.0.31.1"
    obj = elflib.load(library)
    exported = sorted(
        s.name for s in obj.exported_symbols() if s.name.startswith("cmark_")
    )
    outputs: dict[str, bytes] = {}
    scratch.mkdir(parents=True, exist_ok=True)
    for key, source in (("consumer", CONSUMER_SRC), ("version", VERSION_GUARD_SRC)):
        directory = scratch / f"ref-{key}"
        directory.mkdir(parents=True, exist_ok=True)
        csrc = directory / "consumer.c"
        csrc.write_text(source, encoding="utf-8")
        binary = directory / "consumer"
        compiled = vlib.run(
            [
                "/usr/bin/cc", "-std=c11", "-O1", "-o", str(binary), str(csrc),
                f"-I{prefix_shared / 'include'}", f"-L{prefix_shared / 'lib'}",
                "-lcmark", f"-Wl,-rpath,{prefix_shared / 'lib'}",
            ],
            env=vlib.base_env(),
            timeout=CONSUMER_TIMEOUT,
            log=log,
            label=f"ref-consumer-{key}",
        )
        if not compiled.ok:
            # The reference is the standard; if it cannot build its own consumer
            # the verifier is broken and must say so rather than grade against
            # an absent expectation.
            raise RuntimeError(
                f"reference consumer '{key}' failed to build: {compiled.tail()}"
            )
        result = vlib.run(
            [str(binary)],
            env=vlib.base_env(LD_LIBRARY_PATH=str(prefix_shared / "lib")),
            timeout=RUN_TIMEOUT,
            full_capture=True,
        )
        if not result.ok:
            raise RuntimeError(
                f"reference consumer '{key}' exited rc={result.returncode}"
            )
        outputs[key] = result.stdout
    log.write(
        f"reference: soname={obj.soname} exports={len(exported)} "
        f"consumers={sorted(outputs)}"
    )
    return RefInfo(
        prefix_shared=prefix_shared,
        prefix_static=prefix_static,
        exported=exported,
        soname=obj.soname or "",
        consumer_output=outputs,
    )
