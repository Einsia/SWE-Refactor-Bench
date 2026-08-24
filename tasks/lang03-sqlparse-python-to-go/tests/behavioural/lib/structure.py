#!/usr/bin/env python3
"""Structural verification: the module, the binary, the packages, the interface.

A compatibility rewrite is not finished when the new code produces the right
bytes.  It is finished when what a distributor ships is still something a
downstream can consume.  For the Python original that meant an importable package,
four top-level functions, a token lattice other projects subclass, and a console
script.  After a port to Go it means a different list of the same kind of thing: a
module at a published path with no requirements, ten packages publishing exactly
155 exported names, a statically linked binary carrying its own provenance, and
three layers that still compile without each other.

Every check is scored on its own, so a submission that gets the parser right but
collapses the package layering loses only that case.

Four things are specific to this task.

**The expectations are the contract, not a reference build.**  The Python
reference publishes a package directory and a console script; State B publishes a
Go module and an ELF binary.  There is nothing to diff.  So the structural
expectations come from source-contract.json -- a deliverable of the task rather
than a description of the reference -- and Expectations.load asserts the
contract's internal consistency at startup, so a contract that drifted from itself
fails loudly instead of grading every submission against a stale number.

**The interface is read from source, not from the built module.**  apidump parses
the tree with go/ast.  A submission that does not compile still has an interface,
and the interface half and the behavioral half measure different things: one
failing should not zero the other's rate.  It also means the API cases work on a
tree whose `filters` package is broken, which is the common half-finished case.

**The binary is read twice, by two independent readers.**  golib.py parses the ELF
and the Go build blob from bytes; `go version -m` asks the toolchain.  Neither
subsumes the other -- the toolchain can refuse a file it does not recognize while
saying nothing about why, and a hand-built blob can satisfy a byte reader while the
toolchain rejects it -- so where they overlap they must agree, and a disagreement
is itself reported.

**The layering cases are a build plus a closure.**  `go build ./sql ./tokens`
succeeding proves less than it looks: Go compiles the transitive closure without
complaint, so the build alone passes even if `sql` imports the whole module.  Each
standalone case therefore reads `go list -deps` back and checks the closure against
the allow list the contract publishes, which is State A's own layering.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import golib
import vlib
from build import (BINARY_NAME, CMD_PACKAGE, MODULE_PATH, PROBE_TIERS,
                   BuildOutcome)
from vlib import CaseOutcome, Log

# --------------------------------------------------------------------------- #
# What a pre-migration tree can be asked
# --------------------------------------------------------------------------- #
#
# Every case in this module reads something the build produced, and for most of
# them the thing produced is a Go artifact: a go.mod, an ELF header, a buildinfo
# blob, a package that compiles alone.  A Python tree has none of those, and it has
# none of them absolutely -- `.note.go.buildid` is not missing from a CPython
# package, it is unsatisfiable by one, in the same way and for the same reason that
# `Py_Initialize` is unsatisfiable by a Go binary.  That second case has been
# unscored since it was written, with the criterion in catalog.py: an expectation is
# scored only if State A can answer it, because State A is the oracle every frozen
# expectation in this suite was computed from.
#
# The tables below apply that criterion to the rest of the family instead of
# asserting in a comment that it does not bite them.  Applied per submission and at
# grading time rather than as a weight in the catalog, so a Go submission is asked
# every one of these exactly as before: nothing about the Go path changes.
#
# `skip` rather than `fail` is a label on the reason and not a discount.  result.py
# keeps a skipped case in the denominator and scores it 0, so a pre-migration tree is
# charged all 28 and rates 4/32 here.  That is the intended price: the 28 read a Go
# artifact, the task was to produce one, and the submissions that produced one answer
# every case.  What the label still buys is a report that says "not applicable,
# because" instead of one that reads like 28 behavioural diagnoses.

#: The checks that are about a deliverable rather than about a toolchain, and that
#: a pre-migration tree therefore answers on its own terms.  Four, and each is a
#: real question with a real answer: the source builds, the command installs and
#: runs, nothing is vendored, the man page still documents the flags.
PY_STRUCT_KEEP = frozenset({
    "build-all", "install-sqlformat", "no-vendor-dir", "man-page-installed",
})

#: Every other check, with the reason it cannot be asked.  Grouped, because 27
#: separate sentences saying "this reads Go" would be 27 chances to say it
#: differently; the group is the reason and the check names are the evidence.
PY_STRUCT_SKIP_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("go.mod and go.sum are Go module metadata; a tree without a module graph "
     "has nothing for these to read, and `go-present` in stage 1 is where the "
     "absence of a go.mod is a finding",
     ("go-mod-parses", "go-mod-directive", "go-mod-no-requires",
      "go-sum-absent-or-empty")),
    ("this reads the installed ELF executable -- its headers, its Go buildinfo, "
     "its build settings, its symbols, its reproducibility -- and a Python tree "
     "installs a launcher over an interpreter, so there is no such artifact to "
     "read rather than a bad one",
     ("binary-is-elf", "binary-static", "binary-buildinfo", "binary-buildid",
      "binary-trimpath", "binary-cgo-disabled", "binary-module-path",
      "binary-reproducible", "binary-no-cpython", "install-inventory")),
    ("this is about the Go package layout the contract names and about which "
     "packages compile without the others; a Python tree has modules rather "
     "than compiled packages and no layering a compiler can be asked about",
     ("package-set", "package-core-standalone", "package-model-standalone",
      "package-keywords-standalone", "package-cmd-main")),
    ("this compiles Go source against the submission -- the four probe tiers and "
     "the conformance consumer -- so it can only be asked of a Go module. What "
     "it stands for, that the library API answers from outside, is measured on "
     "this path too: `build/probe-tiers` runs the same four tiers and the "
     "behavioural modules put 8,326 cases through them",
     ("probe-core-compiles", "probe-model-compiles", "probe-keywords-compiles",
      "probe-parts-compiles", "consumer-compiles")),
    ("this reads `go list` output to check the dependency closure is the "
     "standard library alone. The pre-migration package has no third-party "
     "runtime dependency either, but proving it would mean a different tool "
     "reading a different graph, and a check that changes what it measures with "
     "the language is not the same check",
     ("import-closure-stdlib", "module-closure-empty")),
    ("`go vet` is a Go analyser with no cross-language analogue",
     ("vet-all",)),
    ("this runs the submission's own test suite through `go test`. sqlparse's "
     "suite is pytest's, and this image has no pytest and no network to fetch "
     "one -- deliberately, since the image is offline -- so the pre-migration "
     "tree cannot be asked whether its own tests pass",
     ("test-all",)),
)

#: check name -> reason, flattened from the groups above.
PY_STRUCT_SKIP: dict[str, str] = {
    name: reason
    for reason, names in PY_STRUCT_SKIP_GROUPS
    for name in names
}

# A check in both tables, or in neither, is the bug this assertion exists to
# catch. Neither is the dangerous one: a check added later would be asked of a
# Python tree, fail on it, and read in the report as the pre-migration
# implementation having lost a behaviour it never had. Both is a table that
# contradicts itself about what it measures. The names are checked against the
# handlers this class actually defines rather than against a list typed here,
# because a list typed here is a second place for the set of checks to live.
_STRUCT_OVERLAP = PY_STRUCT_KEEP & set(PY_STRUCT_SKIP)
if _STRUCT_OVERLAP:
    raise SystemExit(
        f"verifier defect: structure check(s) {sorted(_STRUCT_OVERLAP)} are both "
        f"kept and skipped for a pre-migration tree")


def py_struct_skip_reason(check: str) -> str:
    """Why `check` is not asked of a pre-migration tree, or "" if it is asked."""
    if check in PY_STRUCT_KEEP:
        return ""
    reason = PY_STRUCT_SKIP.get(check)
    if reason is None:
        # Not classified.  Refused rather than defaulted either way: defaulting to
        # asked makes a Go-only check fail on a Python tree and read as a lost
        # behaviour, and defaulting to skipped silently drops a case that might
        # have been measurable.
        raise SystemExit(
            f"verifier defect: structure check {check!r} is in neither "
            f"PY_STRUCT_KEEP nor PY_STRUCT_SKIP, so there is no answer to "
            f"whether a pre-migration tree can be asked it")
    return reason


# Byte sequences that mean a CPython runtime is inside the artifact.  Searched in
# the binary's own bytes rather than in its symbol table: a statically linked
# binary has no dynamic symbols to inspect, and a submission that embedded an
# interpreter would strip the table long before it stripped the strings.
CPYTHON_MARKERS = (
    b"Py_Initialize", b"PyRun_", b"libpython", b"Py_Main", b"Py_BytesMain",
    b"PyImport_", b"PyEval_EvalCode",
)

# Suffixes that mean a Python implementation is still in the tree.  Checked here
# for the structural report; the mandatory gate in audit.py checks the same
# thing and is the one that withholds the score.
PYTHON_SUFFIXES = (".py", ".pyi", ".pyc", ".pyo", ".pyd")

# Markers a machine translation leaves behind.  `DO NOT EDIT` is the conventional
# generated-file header, and a tree full of them is a transpiler's output rather
# than a port -- which is a legitimate way to start but not a legitimate way to
# finish, because the result is unmaintainable by the humans who own the
# repository.  The pattern is deliberately narrow: a single generated table with a
# provenance line is normal Go practice, so the case fails on breadth, not
# presence.
GENERATED_MARKERS = (
    re.compile(rb"^// Code generated .* DO NOT EDIT\.$", re.MULTILINE),
    re.compile(rb"^// Automatically generated .* DO NOT EDIT", re.MULTILINE),
    re.compile(rb"^/\* Generated by py2go", re.MULTILINE),
    re.compile(rb"DO NOT EDIT THIS FILE", re.MULTILINE),
)
GENERATED_FILE_LIMIT = 2

# `go list -deps` output that is neither the standard library nor the module
# itself.  A stdlib import path has no dot in its first component -- "net/http" is
# stdlib, "github.com/x/y" is not -- which is the same rule the toolchain uses.
def _is_stdlib(path: str) -> bool:
    first = path.split("/", 1)[0]
    return "." not in first


@dataclass
class Expectations:
    """The part of the contract this stage builds against, self-checked as it loads.

    Everything here comes from source-contract.json.  The assertions in `load` are
    about the contract rather than about a submission: they fire at image build
    time, when the file is wrong, rather than at grading time, when a wrong file
    would quietly grade every submission against an impossible target.

    Not every contract key appears as a field.  The contract is a shared input --
    the audit stage reads the same file -- and the keys that describe the
    *repository* (`preserved_paths`, `python_policy.must_delete`, the doc-comment
    ratio, the Go logic floor) are read there, by an agent looking at the tree.
    This stage builds the submission and asks the binary questions, so it loads the
    keys a build needs and leaves the rest to the reader that can act on them.
    """

    module_path: str
    go_min: tuple[int, int]
    go_max: tuple[int, int]
    packages: dict[str, dict]
    closures: dict
    install_inventory: dict
    forbidden_paths: list[str]
    state_a: dict
    probe_tiers: dict

    @classmethod
    def load(cls, contract: dict) -> Expectations:
        go = contract["go_contract"]
        build = contract["build_contract"]

        packages = dict(go["packages"])

        if go["module_path"] != build["module_path"]:
            raise SystemExit(
                "verifier corrupted: go_contract.module_path "
                f"({go['module_path']}) and build_contract.module_path "
                f"({build['module_path']}) disagree"
            )
        if MODULE_PATH != go["module_path"]:
            raise SystemExit(
                f"verifier corrupted: build.MODULE_PATH ({MODULE_PATH}) does not "
                f"match the contract ({go['module_path']})"
            )

        closures = go["standalone_closures"]["closures"]
        for name, entry in closures.items():
            allowed = entry["allowed"]
            if isinstance(allowed, list):
                unknown = [p for p in allowed if p not in packages]
                if unknown:
                    raise SystemExit(
                        f"verifier corrupted: standalone closure '{name}' allows "
                        f"packages that are not in the contract: {unknown}"
                    )
            # `go build ./sql` names a package by directory, so every build target
            # has to be a directory the contract knows about.
            for target in entry["build"]:
                rel = "." if target == "." else target.lstrip("./")
                if rel not in packages:
                    raise SystemExit(
                        f"verifier corrupted: standalone closure '{name}' builds "
                        f"'{target}', which is not a contracted package"
                    )

        for tier, entry in go["probe_tiers"].items():
            for imported in entry["imports"]:
                rel = ("." if imported == go["module_path"]
                       else imported[len(go["module_path"]) + 1:])
                if rel not in packages:
                    raise SystemExit(
                        f"verifier corrupted: probe tier '{tier}' imports "
                        f"'{imported}', which is not a contracted package"
                    )

        def version(text: str) -> tuple[int, int]:
            major, _, minor = text.partition(".")
            return int(major), int(minor)

        go_min = version(build["go_directive_min"])
        go_max = version(build["go_directive_max"])
        if go_min > go_max:
            raise SystemExit(
                f"verifier corrupted: go directive range is empty "
                f"({build['go_directive_min']} > {build['go_directive_max']})"
            )

        return cls(
            module_path=go["module_path"],
            go_min=go_min,
            go_max=go_max,
            packages=packages,
            closures=closures,
            install_inventory=contract["install_inventory"],
            forbidden_paths=list(contract["forbidden_paths"]),
            state_a=contract["state_a"],
            probe_tiers=go["probe_tiers"],
        )


@dataclass
class GoMod:
    """The parts of go.mod the structural cases ask about.

    Hand-parsed rather than read through `go mod edit -json`, for the same reason
    golib exists: the toolchain's answer is a second opinion worth having, and a
    go.mod malformed enough that the toolchain refuses to read it must still
    produce a specific finding rather than a tool error.  `go mod edit -json` is
    consulted too, by check_go_mod_parses, and the two are compared.
    """

    present: bool
    module: str = ""
    go_directive: str = ""
    requires: list[str] = field(default_factory=list)
    toolchain: str = ""
    replaces: list[str] = field(default_factory=list)
    excludes: list[str] = field(default_factory=list)
    error: str = ""

    @classmethod
    def parse(cls, path: Path) -> GoMod:
        if not path.is_file():
            return cls(present=False, error="go.mod does not exist")
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return cls(present=True, error=f"go.mod could not be read: {exc}")

        mod = cls(present=True)
        block: str | None = None
        for raw in text.splitlines():
            line = raw.split("//", 1)[0].strip()
            if not line:
                continue
            if block is not None:
                if line == ")":
                    block = None
                    continue
                getattr(mod, block).append(line)
                continue
            for keyword, attr in (("require", "requires"),
                                  ("replace", "replaces"),
                                  ("exclude", "excludes")):
                if line == f"{keyword} (":
                    block = attr
                    break
                if line.startswith(f"{keyword} "):
                    getattr(mod, attr).append(line[len(keyword) + 1:].strip())
                    break
            else:
                if line.startswith("module "):
                    mod.module = line[len("module "):].strip()
                elif line.startswith("go "):
                    mod.go_directive = line[len("go "):].strip()
                elif line.startswith("toolchain "):
                    mod.toolchain = line[len("toolchain "):].strip()
        return mod


class Structure:
    """Evaluates the structural cases against one built submission."""

    def __init__(self, repo: Path, outcome: BuildOutcome, expect: Expectations,
                 log: Log, *, pristine: Path | None = None) -> None:
        self.repo = repo
        self.outcome = outcome
        self.expect = expect
        self.log = log
        # No Builder here on purpose.  Everything this module needs about the
        # build is on the outcome, which survives the process boundary; a Builder
        # does not, since it owns a live scratch directory.  Taking one would make
        # the structural cases silently unrunnable in the module that grades them.
        # State A, extracted from the pinned tarball.  Present for the
        # byte-identical cases; when absent those cases fall back to the digests
        # recorded in the contract, which are the same numbers.
        self.pristine = pristine

        self.go_mod = GoMod.parse(repo / "go.mod")
        self._binary: golib.ElfFile | None = None
        self._binary_bytes: bytes | None = None
        self._binary_error = ""
        self._buildinfo: golib.GoBuildInfo | None = None
        self._go_files: list[Path] | None = None

    # -- dispatch ----------------------------------------------------------

    def evaluate(self, cases: list[dict]) -> list[CaseOutcome]:
        results: list[CaseOutcome] = []
        skipped = 0
        for case in cases:
            check = case["check"]
            start = vlib.now()
            reason = (py_struct_skip_reason(check)
                      if self.outcome.language != "go" else "")
            if reason:
                skipped += 1
                results.append(
                    CaseOutcome(
                        case_id=case["id"], family=case["family"], kind="struct",
                        passed=False, weight=float(case.get("weight", 1.0)),
                        skipped=True, skip_reason=vlib.clip(reason, 2000),
                        duration=vlib.now() - start,
                    )
                )
                continue
            handler = getattr(self, f"check_{check.replace('-', '_')}", None)
            if handler is None:
                passed, detail = False, f"no handler for check '{check}'"
            else:
                try:
                    passed, detail = handler()
                except Exception as exc:  # a check must never abort the suite
                    passed = False
                    detail = f"{type(exc).__name__}: {exc}"
            results.append(
                CaseOutcome(
                    case_id=case["id"],
                    family=case["family"],
                    kind="struct",
                    passed=passed,
                    weight=float(case.get("weight", 1.0)),
                    detail=vlib.clip(detail, 2000),
                    duration=vlib.now() - start,
                )
            )
        if skipped:
            self.log.write(
                f"structure: {skipped} of {len(cases)} case(s) are not "
                f"applicable to a {self.outcome.language} tree and were skipped; "
                f"{len(cases) - skipped} were asked")
        return results

    # -- shared readers ----------------------------------------------------

    def _phase(self, name: str) -> tuple[bool, str]:
        result = getattr(self.outcome, name)
        if result is None:
            return False, f"{name} was never run"
        if result.ok:
            return True, ""
        return False, f"{name} failed (rc={result.returncode}): {result.tail()}"

    def binary(self) -> tuple[golib.ElfFile | None, str]:
        """The installed command, parsed once and cached, or why it is not there.

        Nine cases read this file, and eight of them would report the same "not
        installed" sentence in eight slightly different wordings if each looked for
        itself.  Returning the reason alongside the parse keeps that sentence in
        one place, and keeps a missing binary a *failed* case rather than an
        exception the dispatcher has to turn into one.
        """
        if self._binary is not None:
            return self._binary, ""
        if self._binary_error:
            return None, self._binary_error
        path = self.outcome.binary
        if not path.is_file():
            self._binary_error = (
                f"no binary at {path}: `go install {CMD_PACKAGE}` did not produce "
                f"one, so nothing about the artifact can be checked"
            )
            return None, self._binary_error
        try:
            raw = path.read_bytes()
            self._binary = golib.ElfFile(raw, origin=str(path))
            self._binary_bytes = raw
        except Exception as exc:
            self._binary_error = f"{path} is not a readable ELF file: {exc}"
            return None, self._binary_error
        return self._binary, ""

    def _info(self) -> tuple[golib.GoBuildInfo | None, str]:
        """The decoded Go build record, shared by the settings cases.

        Decoded through check_binary_buildinfo's cache when that case has already
        run, and independently otherwise -- the case order is the catalog's, not
        something these methods should depend on.
        """
        if self._buildinfo is not None:
            return self._buildinfo, ""
        elf, why = self.binary()
        if elf is None:
            return None, why
        try:
            info = golib.find_buildinfo(elf)
        except golib.ElfError as exc:
            return None, f"build info present but undecodable: {exc}"
        if info is None:
            return None, "the binary carries no Go build info to read settings from"
        self._buildinfo = info
        return info, ""

    def go_files(self) -> list[Path]:
        """Every non-test Go source file in the submitted tree."""
        if self._go_files is None:
            self._go_files = sorted(
                p for p in self.repo.rglob("*.go")
                if not p.name.endswith("_test.go")
                and "vendor" not in p.parts
                and "testdata" not in p.parts
            )
        return self._go_files

    def api(self) -> dict | None:
        return self.outcome.api

    def api_for(self, package: str) -> tuple[dict | None, str]:
        """One package's dumped API, or a reason it is unavailable."""
        api = self.api()
        if api is None:
            return None, "the API dump did not run, so no interface can be read"
        entry = api.get(package)
        if entry is None:
            return None, f"package '{package}' does not exist in the submission"
        return entry, ""

    # -- the module --------------------------------------------------------

    def check_go_mod_parses(self) -> tuple[bool, str]:
        """go.mod exists and declares the contracted module path."""
        mod = self.go_mod
        if not mod.present:
            return False, "go.mod does not exist: the tree is not a Go module"
        if mod.error:
            return False, mod.error
        if not mod.module:
            return False, "go.mod declares no module path"
        if mod.module != self.expect.module_path:
            return False, (
                f"module path is '{mod.module}', contract requires "
                f"'{self.expect.module_path}'. Every import path in the probe and "
                f"in any downstream consumer is derived from this string, so a "
                f"different one is a different module"
            )
        return True, f"module {mod.module}"

    def check_go_mod_directive(self) -> tuple[bool, str]:
        """The go directive names a version inside the pinned range."""
        mod = self.go_mod
        if not mod.present:
            return False, "go.mod does not exist"
        if not mod.go_directive:
            return False, "go.mod has no `go` directive"
        match = re.match(r"^(\d+)\.(\d+)", mod.go_directive)
        if not match:
            return False, f"`go {mod.go_directive}` is not a version"
        got = (int(match.group(1)), int(match.group(2)))
        lo, hi = self.expect.go_min, self.expect.go_max
        if not lo <= got <= hi:
            return False, (
                f"`go {mod.go_directive}` is outside the contracted range "
                f"{lo[0]}.{lo[1]}..{hi[0]}.{hi[1]}. The floor is the language "
                f"version the port may assume; the ceiling keeps the module "
                f"buildable by a toolchain a consumer plausibly has"
            )
        return True, f"go {mod.go_directive}"

    def check_go_mod_no_requires(self) -> tuple[bool, str]:
        """The module closure is the standard library plus itself."""
        mod = self.go_mod
        if not mod.present:
            return False, "go.mod does not exist"
        # A `require` naming the module itself is meaningless but harmless, and a
        # toolchain line is not a dependency.  Everything else is a third-party
        # dependency, which the contract forbids.
        real = [r for r in mod.requires
                if not r.startswith(self.expect.module_path)]
        if real:
            return False, (
                f"go.mod requires {len(real)} module(s) outside the standard "
                f"library: {real[:6]}. The contract's closed world is the stdlib "
                f"plus this module; a dependency means the port outsourced part of "
                f"the implementation"
            )
        if mod.replaces:
            return False, (
                f"go.mod carries {len(mod.replaces)} replace directive(s): "
                f"{mod.replaces[:4]}. A replace changes what a build resolves "
                f"against, so a consumer building this module gets something other "
                f"than what was graded"
            )
        return True, "no requirements, no replacements"

    def check_go_sum_absent_or_empty(self) -> tuple[bool, str]:
        """Nothing to verify means no checksums.

        Read from the submitted tree, not from the build copy: `go build` may
        create go.sum as a side effect, and the question is what was submitted.
        """
        path = self.repo / "go.sum"
        if not path.exists():
            return True, "go.sum absent"
        size = path.stat().st_size
        if size == 0:
            return True, "go.sum present but empty"
        lines = [line for line in
                 path.read_text(encoding="utf-8", errors="replace").splitlines()
                 if line.strip()]
        return False, (
            f"go.sum has {len(lines)} entry/entries ({size} bytes): "
            f"{lines[:3]}. A checksum database entry exists only for a module that "
            f"was downloaded, so this is a dependency the module graph does not "
            f"admit to"
        )

    # -- the graded build --------------------------------------------------

    def check_build_all(self) -> tuple[bool, str]:
        return self._phase("build")

    def check_vet_all(self) -> tuple[bool, str]:
        return self._phase("vet")

    def check_test_all(self) -> tuple[bool, str]:
        """The submission's own tests must pass, if it has any.

        A module with no test files exits 0 here, so a port that wrote none is not
        penalized by this case -- the advisory `guard-tests-exist` is where that is
        noted.  What this catches is a port whose own tests fail, which is the
        submission testifying against itself.
        """
        return self._phase("test")

    def check_install_sqlformat(self) -> tuple[bool, str]:
        ok, detail = self._phase("install")
        if not ok:
            return False, detail
        path = self.outcome.binary
        if not path.is_file():
            return False, (
                f"go install succeeded but {path.name} is not at {path}. The "
                f"contract's install target is ./cmd/sqlformat, and GOBIN is where "
                f"go install puts what it builds"
            )
        return True, f"{path.name} installed ({path.stat().st_size} bytes)"


    def check_binary_is_elf(self) -> tuple[bool, str]:
        elf, why = self.binary()
        if elf is None:
            return False, why
        if elf.machine_name != "x86-64":
            return False, (
                f"binary targets {elf.machine_name}, not the image's x86-64: it "
                f"was cross-compiled elsewhere, not built here"
            )
        if elf.type_name not in ("EXEC", "DYN"):
            return False, f"ELF type is {elf.type_name}, not an executable"
        return True, f"ELF64 {elf.type_name} {elf.machine_name}"

    def check_binary_static(self) -> tuple[bool, str]:
        """No interpreter, no shared library, and no link line that wanted one.

        Go with CGO_ENABLED=0 links statically, which is the property a consumer
        actually gets: one file to copy, nothing to install alongside it.  A
        dynamic binary here means cgo was involved, and cgo is how a port would
        reach a C library -- including libpython.

        Three tags beyond is_static()'s two, because a binary can satisfy "nothing
        is loaded at run time" and still show that something was meant to be.
        DT_RPATH and DT_RUNPATH are search paths for libraries this file does not
        name: they only get emitted by an external link, so their presence says a C
        toolchain drove the final link even though the result happens to resolve
        nothing.  DT_SONAME says the file was produced as a shared object, and an
        installed `sqlformat` that is really a library is a different artefact than
        the one the contract asks for.  All three are read off the same dynamic
        table is_static() already walks, and all three are reported together --
        knowing a runpath points at /usr/lib/x86_64-linux-gnu is what turns "this
        is not static" into "this was linked against the system libraries".
        """
        elf, why = self.binary()
        if elf is None:
            return False, why
        static, reason = elf.is_static()
        if not static:
            return False, (
                f"binary is dynamically linked: {reason}. needed={elf.needed()} "
                f"interp={elf.interpreter()!r} runpath={elf.runpath()}"
            )
        runpath = elf.runpath()
        soname = elf.soname()
        residue = []
        if runpath:
            residue.append(f"DT_RPATH/DT_RUNPATH={':'.join(runpath)}")
        if soname:
            residue.append(f"DT_SONAME={soname}")
        if residue:
            return False, (
                f"binary has no PT_INTERP and no DT_NEEDED, so nothing is loaded "
                f"at run time, but it carries {' and '.join(residue)} -- tags a "
                f"pure-Go internal link does not emit. The final link went through "
                f"a C toolchain, which is the same route cgo takes to libpython."
            )
        return True, f"{reason}, no DT_RPATH/DT_RUNPATH, no DT_SONAME"

    def check_binary_buildinfo(self) -> tuple[bool, str]:
        """The binary carries a decodable Go build record, and both readers agree.

        The disagreement branch is not defensive noise.  `go version -m` reads the
        blob through the toolchain that wrote it; golib decodes it from the bytes.
        If they differ, the binary in GOBIN is not the binary the toolchain thinks
        it produced, which is exactly the shape a swapped or patched artifact takes.
        """
        elf, why = self.binary()
        if elf is None:
            return False, why
        try:
            info = golib.find_buildinfo(elf)
        except golib.ElfError as exc:
            return False, f"build info present but undecodable: {exc}"
        if info is None:
            return False, (
                "binary has no Go build info: it was not produced by `go build`, "
                "or the record was stripped out of it afterwards"
            )
        self._buildinfo = info
        note = ""
        result = self.outcome.extra.get("go-version-m")
        if result is not None and result.ok:
            text = result.text()
            for field_name, value in (("path", info.path), ("mod", info.module)):
                marker = f"\t{field_name}\t{value}"
                if value and marker not in text and f"{field_name}\t{value}" not in text:
                    return False, (
                        f"the two readers disagree: golib decoded {field_name}="
                        f"{value!r} but `go version -m` does not report it. "
                        f"go version -m said: {result.tail(lines=8)}"
                    )
            note = "; agrees with `go version -m`"
        return True, f"{info.summary()} (from {info.source}){note}"

    def check_binary_buildid(self) -> tuple[bool, str]:
        """`.note.go.buildid` is present and non-empty.

        Read as an ELF note, name "Go" and type 4, so a plausible string elsewhere
        in the file does not satisfy it.  The linker always writes this; its
        absence means the file was post-processed.
        """
        elf, why = self.binary()
        if elf is None:
            return False, why
        buildid = golib.read_buildid(elf)
        if not buildid:
            return False, (
                "no Go build ID note in the binary: `go build` always writes one, "
                "so it was stripped or the file was not linked by the Go linker"
            )
        return True, f"build id {buildid[:48]}{'...' if len(buildid) > 48 else ''}"

    def check_binary_trimpath(self) -> tuple[bool, str]:
        """-trimpath=true is recorded in the build settings.

        The graded install passes -trimpath, so this is not primarily a check on
        the submission's flags -- it confirms the recorded settings belong to the
        install that just ran, and it is the setting that makes the reproducibility
        case meaningful (without it the binary embeds the scratch path, which
        differs per run for reasons that are the verifier's fault, not the port's).
        """
        info, why = self._info()
        if info is None:
            return False, why
        value = info.trimpath()
        if value != "true":
            return False, (
                f"-trimpath is {value!r} in the build settings, expected 'true'. "
                f"settings={sorted(info.settings)}"
            )
        return True, "-trimpath=true"

    def check_binary_cgo_disabled(self) -> tuple[bool, str]:
        """CGO_ENABLED=0, and no cgo-related setting contradicts it.

        This is the binary-level statement of the same property the import closure
        checks in source: nothing in this program came from a C compiler.  It is
        the check a port most plausibly fails by accident, because linking a C
        library is the shortest path to reusing an existing implementation.
        """
        info, why = self._info()
        if info is None:
            return False, why
        value = info.cgo_enabled()
        if value != "0":
            return False, (
                f"CGO_ENABLED is {value!r} in the build settings, expected '0': "
                f"the binary was built with cgo available, so C code could be "
                f"linked into it"
            )
        offenders = {k: v for k, v in info.settings.items()
                     if k.startswith("CGO_") and k != "CGO_ENABLED" and v}
        if offenders:
            return False, (
                f"CGO_ENABLED=0 but cgo settings are non-empty: {offenders}"
            )
        return True, "CGO_ENABLED=0"

    def check_binary_module_path(self) -> tuple[bool, str]:
        """The binary's own record names the contracted module and command.

        This closes the loop between source and artifact.  go.mod claiming the
        module path proves nothing about the file in GOBIN; this reads the path out
        of the file itself, so a binary built from some other tree cannot pass by
        sitting next to a correct go.mod.
        """
        info, why = self._info()
        if info is None:
            return False, why
        want_mod = self.expect.module_path
        want_path = f"{want_mod}/{CMD_PACKAGE.lstrip('./')}"
        problems = []
        if info.module != want_mod:
            problems.append(f"main module is {info.module!r}, expected {want_mod!r}")
        if info.path != want_path:
            problems.append(f"main package is {info.path!r}, expected {want_path!r}")
        if info.deps:
            problems.append(
                f"binary records {len(info.deps)} module dependency/dependencies: "
                f"{info.deps[:4]}"
            )
        if problems:
            return False, "; ".join(problems)
        return True, f"{info.path} from {info.module}"

    def check_binary_no_cpython(self) -> tuple[bool, str]:
        """No trace of an embedded or dlopen-able CPython.

        The one cheat this case exists for: a port that keeps the Python
        implementation and drives it from Go, either by linking libpython or by
        shipping the interpreter and shelling out.  Both leave marks -- symbols and
        library names for the first, the interpreter's own name in the strings for
        the second -- so the scan looks at the whole file, not just the symbol
        table, and reports what it found rather than a verdict alone.
        """
        elf, why = self.binary()
        if elf is None:
            return False, why
        raw = self._binary_bytes
        assert raw is not None  # set together with the parsed ELF
        found = [m.decode("ascii") for m in CPYTHON_MARKERS if m in raw]
        needed = [lib for lib in elf.needed() if "python" in lib.lower()]
        undefined = [s for s in elf.undefined_dynamic()
                     if s.lower().startswith("py") or "python" in s.lower()]
        if found or needed or undefined:
            return False, (
                f"the binary carries CPython traces: markers={found} "
                f"needed={needed} undefined={undefined[:8]}. The Python "
                f"implementation is still doing the work"
            )
        return True, f"none of the {len(CPYTHON_MARKERS)} CPython markers present"

    def check_binary_reproducible(self) -> tuple[bool, str]:
        """Two installs from a cold cache produce the same bytes."""
        result = self.outcome.reinstall
        if result is None:
            return False, "the second install was never attempted"
        if not result.ok:
            return False, (
                f"the second install failed (rc={result.returncode}): "
                f"{result.tail()}. It ran with an empty build cache and an offline "
                f"module cache, so a build that needs either the first run's cache "
                f"or the network fails here"
            )
        first, second = self.outcome.binary, self.outcome.reinstalled_binary
        if not second.is_file():
            return False, f"the second install produced no binary at {second}"
        digest_a = vlib.sha256_file(first)
        digest_b = vlib.sha256_file(second)
        if digest_a != digest_b:
            return False, (
                f"the two installs differ: {digest_a[:16]} vs {digest_b[:16]} "
                f"({first.stat().st_size} vs {second.stat().st_size} bytes). "
                f"Something that varies between runs -- a timestamp, a hostname, "
                f"an untrimmed absolute path, map iteration order in a generator -- "
                f"is compiled into the binary"
            )
        return True, f"identical across two cold-cache builds ({digest_a[:16]})"

    # -- packages and layering ---------------------------------------------

    def check_package_set(self) -> tuple[bool, str]:
        """Exactly the ten contracted packages exist, each with its declared name.

        Directory layout is the part of a Go module a consumer types.  `import
        ".../sql"` is a path on disk; renaming the directory or folding two
        packages together breaks every downstream at compile time even when every
        function still works.  The package *clause* is checked too, because a
        directory named sql whose files say `package parser` is imported under a
        name nobody expects.
        """
        api = self.api()
        if api is None:
            return False, "the API dump did not run, so no package list can be read"
        want = set(self.expect.packages)
        got = set(api)
        missing = sorted(want - got)
        extra = sorted(got - want)
        problems = []
        if missing:
            problems.append(f"missing package(s): {missing}")
        if extra:
            problems.append(f"unexpected package(s): {extra}")
        wrong = []
        for name in sorted(want & got):
            declared = api[name].get("name", "")
            expected = self.expect.packages[name]["package_name"]
            if declared != expected:
                wrong.append(f"{name}: `package {declared}` should be `package "
                             f"{expected}`")
        if wrong:
            problems.append("; ".join(wrong))
        if problems:
            return False, ". ".join(problems)
        return True, f"all {len(want)} packages present with the contracted names"

    def check_package_cmd_main(self) -> tuple[bool, str]:
        """cmd/sqlformat is a main package with a main function.

        The console script is a release artifact: `sqlformat` on PATH is what a
        user who never writes Go gets from this project.  A port that publishes the
        library and drops the command has dropped a deliverable, and a cmd package
        that is not `package main` cannot be installed at all.
        """
        entry, why = self.api_for("cmd/sqlformat")
        if entry is None:
            return False, why
        if entry.get("name") != "main":
            return False, (
                f"cmd/sqlformat declares `package {entry.get('name')}`, "
                f"which go install cannot turn into a command"
            )
        # `main` is unexported, so it is not in the symbol dump; look for the
        # declaration in the package's own files instead.
        directory = self.repo / "cmd" / "sqlformat"
        pattern = re.compile(r"^func\s+main\s*\(\s*\)", re.MULTILINE)
        for path in sorted(directory.glob("*.go")):
            if path.name.endswith("_test.go"):
                continue
            if pattern.search(path.read_text(encoding="utf-8", errors="replace")):
                return True, f"package main with func main in {path.name}"
        return False, (
            f"cmd/sqlformat is package main but declares no `func main()` in "
            f"{sorted(p.name for p in directory.glob('*.go'))}"
        )

    def _standalone(self, name: str) -> tuple[bool, str]:
        """Grade one layering closure: it compiles alone, and imports only its layer.

        Two questions, one case, because either alone is answerable the wrong way.
        A build that fails means the layer cannot be consumed on its own.  A build
        that succeeds means very little by itself: Go compiles whatever the target
        transitively imports without complaint, so `go build ./sql ./tokens` is
        green even when sql imports the formatter, the lexer and the engine.  The
        closure read back from `go list -deps` is what makes the case about
        layering.
        """
        spec = self.expect.closures.get(name)
        if spec is None:
            return False, f"the contract has no standalone closure named '{name}'"
        result = self.outcome.standalone.get(name)
        if result is None:
            return False, (
                f"the '{name}' closure was never compiled (the graded build did "
                f"not get far enough to attempt it)"
            )
        if not result.ok:
            return False, (
                f"`go build {' '.join(spec['build'])}` failed (rc="
                f"{result.returncode}): {result.tail()}"
            )
        allowed = spec["allowed"]
        if not isinstance(allowed, list):
            # The root closure is allowed the whole module by contract; the build
            # succeeding is the whole property.
            return True, f"builds standalone ({allowed})"
        deps = self.outcome.standalone.get(f"{name}-deps")
        if deps is None or not deps.ok:
            return False, (
                f"the '{name}' closure built but its import closure could not be "
                f"read, so the layering half of the case is unverifiable"
            )
        prefix = self.expect.module_path
        internal = set()
        for line in deps.text().splitlines():
            line = line.strip()
            if not line or line == prefix:
                continue
            if line.startswith(prefix + "/"):
                internal.add(line[len(prefix) + 1:])
        strays = sorted(internal - set(allowed))
        if strays:
            return False, (
                f"'{name}' compiles alone but its import closure reaches "
                f"{strays}, outside the contracted layer {sorted(allowed)}. The "
                f"layer is not separable: a consumer importing it compiles the "
                f"packages it was supposed to be independent of"
            )
        return True, (
            f"builds standalone; internal closure {sorted(internal)} within "
            f"{sorted(allowed)}"
        )

    def check_package_core_standalone(self) -> tuple[bool, str]:
        return self._standalone("core")

    def check_package_model_standalone(self) -> tuple[bool, str]:
        return self._standalone("model")

    def check_package_keywords_standalone(self) -> tuple[bool, str]:
        return self._standalone("keywords")

    def check_no_vendor_dir(self) -> tuple[bool, str]:
        """No vendor directory.

        A vendor tree is how a module with dependencies builds offline -- which
        makes it the natural hiding place for one.  With a stdlib-only closure
        there is nothing to vendor, so its presence means either a dependency the
        module graph does not mention or a copy of somebody else's implementation
        checked in wholesale.
        """
        vendor = self.repo / "vendor"
        if not vendor.exists():
            return True, "no vendor/ directory"
        if vendor.is_file():
            return False, "vendor exists as a file, which shadows the directory name"
        entries = sorted(p.name for p in vendor.iterdir())
        go_files = sum(1 for _ in vendor.rglob("*.go"))
        return False, (
            f"vendor/ exists with {len(entries)} entry/entries and {go_files} Go "
            f"file(s): {entries[:8]}. The contracted closure is the standard "
            f"library alone, so there is nothing legitimate to vendor"
        )

    def check_import_closure_stdlib(self) -> tuple[bool, str]:
        """Everything the module builds imports only the stdlib and itself.

        `go list -deps ./...` prints the full transitive closure, one import path
        per line.  A path whose first segment contains a dot is a domain name and
        therefore not stdlib -- that is the same test the toolchain uses.  This is
        the source-level statement of the property go.mod asserts and the binary's
        empty dep list confirms; all three are checked because each can be true
        while another is false.
        """
        result = self.outcome.deps
        if result is None:
            return False, "go list -deps was never run"
        if not result.ok:
            return False, (
                f"go list -deps failed (rc={result.returncode}): {result.tail()}"
            )
        prefix = self.expect.module_path
        foreign = []
        for line in result.text().splitlines():
            path = line.strip()
            if not path or path == prefix or path.startswith(prefix + "/"):
                continue
            if not _is_stdlib(path):
                foreign.append(path)
        if foreign:
            unique = sorted(set(foreign))
            return False, (
                f"{len(unique)} non-stdlib package(s) in the import closure: "
                f"{unique[:8]}. The port is only allowed the standard library, "
                f"which is what makes the module installable with no network and "
                f"no dependency review"
            )
        total = len([l for l in result.text().splitlines() if l.strip()])
        return True, f"{total} packages in the closure, all stdlib or module-local"

    def check_module_closure_empty(self) -> tuple[bool, str]:
        """`go list -m all` names this module and nothing else.

        The module graph's own answer to the same question, which go.mod's text
        cannot give: a `require` can be absent from the file and still enter the
        graph through a workspace file or a toolchain-inserted upgrade.
        """
        result = self.outcome.mods
        if result is None:
            return False, "go list -m all was never run"
        if not result.ok:
            return False, (
                f"go list -m all failed (rc={result.returncode}): {result.tail()}"
            )
        lines = [l.strip() for l in result.text().splitlines() if l.strip()]
        others = [l for l in lines if not l.split()[0] == self.expect.module_path]
        if others:
            return False, (
                f"the module graph contains {len(others)} other module(s): "
                f"{others[:6]}"
            )
        if not lines:
            return False, "go list -m all printed nothing, which is not a module graph"
        return True, f"module graph is {lines[0]} alone"

    def _tier(self, tier: str) -> tuple[bool, str]:
        result = self.outcome.tiers.get(tier)
        if result is None:
            return False, (
                f"probe tier '{tier}' was never compiled: the graded build failed "
                f"before the probe could be built against the submission"
            )
        if not result.ok:
            imports = self.expect.probe_tiers[tier]["imports"]
            return False, (
                f"the '{tier}' probe does not compile against this submission "
                f"(rc={result.returncode}). It imports {imports}, so a missing or "
                f"differently-shaped exported symbol in any of those packages "
                f"fails it -- and every behavioral case in this tier is marked "
                f"unanswered as a result. Compiler output: {result.tail(lines=20)}"
            )
        path = self.outcome.tier_binary(tier)
        if not path.is_file():
            return False, (
                f"the '{tier}' probe compiled but no binary is at {path}"
            )
        return True, f"probe-{tier} built ({path.stat().st_size} bytes)"

    def check_probe_core_compiles(self) -> tuple[bool, str]:
        """The root-only probe compiles.

        This case is worth reading before the others when a submission scores zero
        behaviorally: the probe is verifier code, held fixed, and it imports the
        library exactly as the contract publishes it.  If it does not compile, the
        interface is not the contracted one and no behavior can be observed through
        it -- which is why the tiers are separate binaries, so one broken package
        does not cost the other three tiers' cases.
        """
        return self._tier("core")

    def check_probe_model_compiles(self) -> tuple[bool, str]:
        return self._tier("model")

    def check_probe_keywords_compiles(self) -> tuple[bool, str]:
        return self._tier("keywords")

    def check_probe_parts_compiles(self) -> tuple[bool, str]:
        return self._tier("parts")

    def check_consumer_compiles(self) -> tuple[bool, str]:
        """Every contracted symbol type-checks from outside the module.

        This case carries the whole published-interface question, and it is the
        only case that can. The four probe tiers cover the API they call, which
        leaves 40 of the contract's 155 symbols named by no tier at all. The
        retired alternative was a family of cases comparing an API dump against a
        checklist of signature text, and text comparison cannot see a declared
        type that is spelled correctly and composes wrongly: `NewGroup(Kind,
        []*Node) *Node` and a `KindWhere` that is not a `Kind` both read as
        conformant. The compiler sees both.

        The consumer is one generated file of `var _ T = pkg.Symbol` declarations,
        one per contracted symbol, with methods asserted through their method-
        expression type so the receiver is checked too. It is compiled and never
        run, so a successful build is the whole result.
        """
        result = self.outcome.consumer
        if result is None:
            return False, (
                "the conformance consumer was never compiled: the graded build "
                "failed before the published API could be type-checked from "
                "outside the module"
            )
        if not result.ok:
            return False, (
                f"the conformance consumer does not compile against this "
                f"submission (rc={result.returncode}). It declares one variable "
                f"per contracted symbol, so each error names a symbol whose type "
                f"differs from the contract -- including symbols no probe tier "
                f"calls, which no other case reaches. Compiler output: "
                f"{result.tail(lines=25)}"
            )
        return True, ("every contracted symbol type-checks from an outside "
                      "consumer, receivers and result tuples included")

    def check_install_inventory(self) -> tuple[bool, str]:
        """Every entry in the contract's install inventory resolves.

        Two kinds of entry, resolved against two different roots, because Go's
        install story is split: `go install` puts the command in GOBIN and knows
        nothing about anything else, so the man page has to be in the tree at the
        path a packager would copy from. The contract writes both under one
        inventory because both are things the release contains -- the pip sdist
        carried the man page and the console script came from the same install.
        """
        inventory = self.expect.install_inventory
        problems = []
        found = []
        for rel, spec in sorted(inventory.items()):
            kind = spec.get("kind", "file")
            if kind == "elf-executable":
                path = self.outcome.prefix / rel
                where = "the install prefix"
            else:
                path = self.repo / rel
                where = "the submitted tree"
            if not path.exists():
                problems.append(f"{rel} is missing from {where} ({path})")
                continue
            if not path.is_file():
                problems.append(f"{rel} exists but is not a regular file")
                continue
            size = path.stat().st_size
            if size == 0:
                problems.append(f"{rel} is empty")
                continue
            if kind == "elf-executable":
                head = path.open("rb").read(4)
                if head != golib.ELF_MAGIC:
                    problems.append(
                        f"{rel} is not an ELF file (starts with {head!r}); the "
                        f"contract forbids a script or wrapper here"
                    )
                    continue
                if not os.access(path, os.X_OK):
                    problems.append(f"{rel} is not executable")
                    continue
            found.append(f"{rel} ({size} bytes)")

        # The inventory is also a ceiling, not just a floor.  `go install ./...`
        # installs one command per main package, so a GOBIN holding anything but
        # sqlformat says the tree declares main packages the contract does not have
        # -- most often a helper the port shells out to at run time, which is how a
        # "static Go binary" ends up depending on a second program.  Only bin/ is
        # inventoried: bin2/ is the reinstall this module compares for
        # reproducibility and probe/ holds the verifier's own tier binaries, both
        # written by the harness rather than by the submission.
        declared = {rel for rel, spec in inventory.items()
                    if spec.get("kind") == "elf-executable"}
        gobin = self.outcome.prefix / "bin"
        extras = []
        if gobin.is_dir():
            for entry in sorted(gobin.iterdir()):
                rel = f"bin/{entry.name}"
                if rel in declared or not entry.is_file():
                    continue
                if os.access(entry, os.X_OK):
                    extras.append(f"{rel} ({entry.stat().st_size} bytes)")
        if extras:
            problems.append(
                f"the install prefix holds {len(extras)} executable(s) the "
                f"inventory does not declare: {', '.join(extras)}")

        if problems:
            return False, "; ".join(problems)
        return True, (f"{len(found)} inventory entry/entries present: {found}; "
                      f"no undeclared executables installed")

    def check_man_page_installed(self) -> tuple[bool, str]:
        """The man page survives and still documents the command's real options.

        The reference ships docs/sqlformat.1 and the contract accepts it at that
        path or at share/man/man1/sqlformat.1. What is graded beyond presence is
        that it still names the flags: a man page kept as a file but left describing
        a command that no longer exists is worse than deleting it, because a
        packager installs it and a user believes it.
        """
        candidates = [
            self.repo / "share" / "man" / "man1" / "sqlformat.1",
            self.repo / "docs" / "sqlformat.1",
        ]
        present = [p for p in candidates if p.is_file() and p.stat().st_size]
        if not present:
            return False, (
                f"no man page at any accepted path "
                f"({[str(p.relative_to(self.repo)) for p in candidates]})"
            )
        text = present[0].read_text(encoding="utf-8", errors="replace")
        # Roff escapes the dashes, so the comparison strips backslashes first.
        flat = text.replace("\\", "")
        wanted = ("--reindent", "--keywords", "--identifiers", "--strip",
                  "--indent_width", "--outfile", "--version")
        absent = [flag for flag in wanted if flag not in flat]
        if absent:
            return False, (
                f"{present[0].relative_to(self.repo)} no longer documents "
                f"{absent}: the page survived but stopped describing the command"
            )
        return True, (
            f"{present[0].relative_to(self.repo)} present ({len(text)} bytes), "
            f"documents all {len(wanted)} sampled options"
        )







