#!/usr/bin/env python3
"""Runs one behavioural module of lang01-cmark-c-to-rust.

The grading engine for this task -- the corpus generator, the case catalog, the
probe harness, the CMake driver, the ELF reader -- predates the modular suite and
is unchanged by it.  What changed is who calls it: instead of one process that
builds, measures and scores in a fixed order, each module is its own process that
asks this driver for one slice of the catalog.

The slices are not arbitrary.  ``catalog.py`` already tags every case with a
family, and the families already group the way a reviewer would group them: the
CommonMark corpus, the four writers, the entity tables, the streaming parser, the
iterator API.  MODULES below is that grouping made explicit, and the mapping is
checked to be total -- a family nobody claims is a case that would silently stop
being graded, so it fails the build instead.

What has to be shared is the build, because it costs minutes and eleven modules
need its output.  The `build` module runs first, installs both link
configurations into $SRB_SUITE_WORK, and writes build.json; everything after it
reads that file.  Nothing else crosses the process boundary.

Usage:
    driver.py --module conformance          # slice of the catalog
    driver.py --module build                # the shared build
    driver.py --module structure            # artifacts, ABI, packaging
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import build as buildmod  # noqa: E402
import audit  # noqa: E402
import oracle  # noqa: E402
import structure  # noqa: E402
import vlib  # noqa: E402
from vlib import Log  # noqa: E402

CONFIGS = ("shared", "static")

# Directories a submission may leave behind that must not be reused.  Rebuilding
# from source is the point: an installed tree or a stale target/ could have been
# produced by anything, including a build with network access or a compiler this
# container does not have.
DISCARD_DIRS = ("target", "build", "_build", "cmake-build", "cmake-build-debug",
                "cmake-build-release", "out", "install", "node_modules", ".git",
                "__pycache__", ".cargo")

# --------------------------------------------------------------------------- #
# Which catalog families each module owns.
#
# Every family in catalog.py appears exactly once.  `_check_total` proves it, so
# adding a family without claiming it is a build failure rather than a quiet loss
# of coverage.
# --------------------------------------------------------------------------- #
MODULES: dict[str, tuple[str, ...]] = {
    # A. the CommonMark specification corpus through the HTML writer
    "conformance":   ("conformance",),
    # B. every writer over the construct corpus, plus round-trips and file input
    "renderers":     ("render-html", "render-xml", "render-man", "render-latex",
                      "render-commonmark", "roundtrip", "parsefile"),
    # C. the generated tables: named entities and Unicode case folding
    "data-tables":   ("entities", "casefold"),
    # D. UTF-8 validation and replacement
    "encoding":      ("encoding",),
    # E. inputs designed to break a parser, and the wrapping widths
    "robustness":    ("pathological", "wrapping"),
    # F. the option flags, individually and in combination
    "options":       ("option-sourcepos", "option-hardbreaks", "option-nobreaks",
                      "option-safe", "option-unsafe", "option-normalize",
                      "option-smart", "option-validate_utf8", "option-combined"),
    # G. the incremental parser
    "streaming":     ("streaming",),
    # H. the AST: accessors, link handling, consolidation, invariants
    "tree":          ("tree-accessors", "tree-links", "tree-consolidate",
                      "tree-check"),
    # I. the iterator API
    "iterators":     ("iterator-edit", "iterator-reset"),
    # J. the pluggable allocator
    "memory":        ("allocator", "allocator-root"),
    # K. building and mutating documents programmatically, and the ABI constants
    "api":           ("api-construct", "api-empty", "api-setters", "api-mutate",
                      "api-mutate-doc", "api-lifecycle", "api-errors",
                      "abi-constants"),
    # L. the installed executable
    "cli":           ("cli",),
}

#: Families the modular suite does not slice by family.
#:
#: `structure` is selected by case *kind* rather than family, because a structural
#: case is not a document being rendered -- it is a question about the installed
#: tree.  `audit` is not graded here at all: it is stage 1's, where a model
#: reads the two trees instead of a regex counting `#include` lines.
KIND_MODULES = {"structure": "struct", "provenance": "guard"}
STAGE_ONE_FAMILIES = ()

# --------------------------------------------------------------------------- #
# The mechanical half of the audit question.
#
# Two kinds of claim, and the modular suite exists to keep them apart:
#
#   * A gate that *observes* something is cheap to trust.  Whether the shimmed
#     compiler was ever invoked is a line in a ledger; whether the shipped .so
#     carries rustc's producer metadata is a note in the ELF; whether the CLI
#     answers differently under a hostile environment is two runs and a diff.
#     None of these is a regex guessing at intent, and none can be argued with,
#     so they are here -- measured, because "the .pc prefix is wrong" and "there
#     is no Rust in this repository" are both unpaid and should not read the same
#     in the report.
#
#   * A gate that *judges* is where a pile of patterns would be doing semantic
#     work it cannot do.  "Is the C really gone, or was it transliterated line for
#     line into unsafe Rust that does the same pointer arithmetic?" is not a
#     question about file extensions.  Those are stage 1's, where a model reads
#     both trees and can tell a port from a transliteration.
#
# The line between the two lists is not "mechanical vs semantic" -- it is whether
# the gate has an *artifact* to point at.  This module runs after both link
# configurations have been configured, built and installed, and everything it is
# entitled to assert is a property of that install: a symbol imported by the shipped
# ELF, a count of libraries in a prefix, six renderings of a document composed from
# random bytes.
#
# So a claim about the *repository* is not this module's to make.  Walking the tree
# for `.o` files, grepping build files for a fetch, hashing COPYING, hashing the
# conformance data -- grading any of those here would put "a stale object file is
# checked in" and "the library does not reproduce the reference on unseen input" on
# the same footing.  The first two are stage 1's, with a read-only scan doing the
# reading; the last two are the scan's alone, because a licence hash is not a
# verdict about whether the port works.
#
# Three gates are narrowed to their artifact half for the same reason.  `no-dlopen`,
# `no-exec-helpers` and `single-implementation` are an imported `dlopen`, an imported
# `execve`, and two libraries in one install prefix.  What they are deliberately not
# is a grep: `dlopen` appears in a comment saying the port does not use it,
# `std::process::Command` in a build script, and a cargo feature named `ffi` is what
# an honest port calls the module holding its `extern "C"` layer.  Those readings
# are advisory findings in stage 1.
#
# The two advisory gates here (version, ABI width) keep their place because both are
# measured on the install: four published version surfaces against a reference
# build, and the exported symbol table against the pinned set of 70.
# --------------------------------------------------------------------------- #
PROVENANCE_GATES: tuple[str, ...] = (
    "compiler-shim-clean",      # the shimmed compilers were never asked to compile
    "no-compile-syscalls",      # no cc1/cc1plus/as ran by another path
    "elf-rust-provenance",      # the shipped objects say rustc built them
    "no-c-provenance",          # no C translation unit recorded in the artifacts
    "needed-whitelist",         # no dependency on an unexpected shared library
    "no-dlopen",                # the shipped ELF cannot reach dlopen
    "no-exec-helpers",          # the shipped ELF cannot spawn
    "no-env-dispatch",          # the answer does not change with the environment
    "single-implementation",    # one library per install prefix
    "no-corpus-answers",        # novel documents render the same as the oracle's
    "version-unchanged",        # still 0.31.1 on every installed surface
    "no-abi-widening",          # the exported surface did not grow
)

#: Of those twelve, the four whose subject is *which toolchain produced the
#: artifact* rather than *what the artifact does*.  Measured, reported, and worth
#: zero.
# : : The reason is the same one that decides every weight in this stage.  Each :
# expectation here was recorded from State A -- the C implementation -- and State A :
# is what a submission is compared against.  These four are the only ones State A :
# cannot satisfy, and it cannot satisfy them by construction: it compiles C, so the :
# shim's ledger has entries, cc1 and as run, the shipped ELF carries no rustc :
# producer metadata, and the archive records C translation units.  A stage that :
# charges for a check its own oracle fails is not measuring whether a submission :
# preserved State A's behaviour; it is asking whether the rewrite happened, which is :
# a different question.  : : Stage 1 already asks it, three times, all required:
# `rust-is-primary` ("the library : target's artifact comes from the Rust build"),
# `no-c-sources` and `no-c-in-build`.  : A required gate failure scores the whole
# submission zero before this image runs, so : moving the charge there is harsher than
# leaving it here, not softer.  : : They keep running because the evidence is not
# reproducible any other way: a : reviewer reading source cannot see that `cc1`
# executed during the graded build, and : the ledger can.  A stage-1 finding of "this
# still looks like C" is confirmed or : refuted by opening this module's report.
RECORDED_ONLY_GATES: frozenset[str] = frozenset({
    "compiler-shim-clean",
    "no-compile-syscalls",
    "elf-rust-provenance",
    "no-c-provenance",
})

# --------------------------------------------------------------------------- #
# The compiler shim's mode, which is the same argument one level down.
#
# Those four gates being worth zero says "which toolchain produced the artifact is
# not this stage's charge".  A shim that *refuses* a repository compile contradicts
# that in the strongest available way: the build module is scored, so a refused
# compile is not a zero on one weightless check but nothing paid for the entire
# stage, and the 4,122 behavioural cases never get a library to ask.  Measured on
# the untouched C library: rate 0.102, `conformance` 0/691 and `cli` 0/253 -- the
# implementation every one of those expectations was recorded from, scoring nothing
# against its own corpus.  That number is about the shim, not about cmark.
#
# So the graded mode is `record`.  The ledger still receives every compile, and
# `compiler-shim-clean` still fails on it; what changes is that the failure costs
# its own weight -- zero, here -- instead of the stage.  The charge lives in stage 1,
# where `no-c-sources`, `no-c-in-build` and `rust-is-primary` are required and a
# failure zeroes the submission before this image runs.  A tree that still needs cc
# therefore loses everything; it just loses it in the stage that asked.
#
# `enforce` is kept because the ledger has one blind spot: a build that reaches a
# compiler under a name SHIM_TOOLS does not shadow leaves nothing to read, and
# absence of evidence reads identically to a clean Rust build.  Re-running under
# `enforce` asks "does this tree still need a C compiler" directly.  `off` installs
# no shim at all, for exercising the build path itself.  Both are diagnostics, both
# annotate the result, and neither is what a grading run takes.
# --------------------------------------------------------------------------- #
SHIM_RECORD = "record"
SHIM_ENFORCE = "enforce"
SHIM_OFF = "off"
SHIM_MODES = (SHIM_RECORD, SHIM_ENFORCE, SHIM_OFF)


def _shim_mode() -> str:
    """SRB_SHIM, validated.  Unset is the graded mode."""
    value = (os.environ.get("SRB_SHIM") or SHIM_RECORD).strip().lower()
    if value not in SHIM_MODES:
        raise SystemExit(
            f"SRB_SHIM={value!r} is not one of {', '.join(SHIM_MODES)}. The modes "
            f"differ in whether a build that compiles C succeeds, so this is not a "
            f"value to guess at.")
    return value

#: Gates stage 1 owns, because answering them means reading code.
#:
#: `no-corpus-answers` is deliberately not here even though "did it hard-code the
#: answers" sounds like the most semantic question of the lot.  Its implementation
#: is not a pattern pile: it composes documents at grading time from
#: `os.urandom(16)` and diffs every output format against the oracle byte for
#: byte, so it asks a submission for output nobody -- including whoever wrote this
#: task -- could have stored in advance.  A model reading source for lookup tables
#: is strictly the weaker instrument, so the measurement stays and stage 1 is free
#: to raise anything it notices under `rust_is_the_implementation`.
#:
#: The last two moved for the other reason: not because they need judgement, but
#: because they had no artifact to measure and this module is only entitled to assert
#: about artifacts.  `no-prebuilt-objects` and `no-network` are folded into stage 1's
#: `no-embedded-reference` and `no-c-in-build`.  Both are fed by the read-only scan,
#: which walks the tree, hashes what State A shipped, and sniffs the first four bytes
#: of anything that might be an object file.
#:
#: There were four.  `license-preserved` and `no-test-mutation` became stage 1 gates
#: in their own right and were advisory there -- `required = false`, so outside the
#: conjunction the stage 1 verdict is built from.  That standing is the whole reason
#: they are now gone: a vote no score can read is one the submission still pays a
#: model to cast.  Both are deleted, here and in the catalog.  The scan that fed
#: them is untouched and still hashes COPYING and the four conformance files, so the
#: measurement survives in the transcript and only the unreadable vote is missing.
SEMANTIC_GATES: tuple[str, ...] = (
    # Eight stage 1 gates of the same name.
    "no-c-sources", "no-foreign-headers", "no-c-in-build", "rust-present",
    "rust-is-primary", "no-embedded-reference", "no-verifier-awareness",
    "default-path",
    # Two folded into a stage 1 gate that already asked the same question rather
    # than duplicated as gates of their own: a duplicate gate would put the same
    # question to the reviewer twice and count the vote twice.
    "no-prebuilt-objects",      # -> no-embedded-reference, which already asks
                                #    about a checked-in .so/.a/.o/.rlib
    "no-network",               # -> no-c-in-build, which already asks what the
                                #    build fetches at configure or build time
)


def _check_total(catalog: dict) -> None:
    """Every family in the catalog is claimed by exactly one module."""
    present = {case["family"] for case in catalog["cases"]}
    claimed: dict[str, str] = {}
    for module, families in MODULES.items():
        for family in families:
            if family in claimed:
                raise SystemExit(
                    f"family {family!r} is claimed by both {claimed[family]!r} "
                    f"and {module!r}; a case cannot be graded twice")
            claimed[family] = module
    # Families reached by kind rather than by name, plus the ones stage 1 took.
    kind_families = {
        case["family"] for case in catalog["cases"]
        if case["kind"] in KIND_MODULES.values()
    }
    accounted = set(claimed) | kind_families | set(STAGE_ONE_FAMILIES)
    orphans = sorted(present - accounted)
    if orphans:
        raise SystemExit(
            f"catalog families claimed by no module: {orphans}. Every case has to "
            f"belong to a module or it stops being graded without anything "
            f"failing -- add it to MODULES in driver.py.")
    unknown = sorted(set(claimed) - present)
    if unknown:
        raise SystemExit(
            f"MODULES claims families the catalog does not contain: {unknown}")


# --------------------------------------------------------------------------- #
# The stage-result envelope every module writes
# --------------------------------------------------------------------------- #


class Emitter:
    """Accumulates checks and writes the JSON the behavioural runner reads."""

    def __init__(self, module: str) -> None:
        self.module = module
        self.checks: list[dict[str, Any]] = []
        self.notes: list[str] = []
        self.metadata: dict[str, Any] = {}
        self.started = time.time()

    def add(self, check_id: str, verdict: str, summary: str = "", *,
            weight: float = 1.0, detail: str = "", **extra: Any) -> None:
        entry: dict[str, Any] = {
            "id": check_id,
            "unit": self.module,
            "verdict": verdict,
            "summary": summary,
            "weight": weight,
        }
        if detail:
            entry["detail"] = detail
        if extra:
            entry["metadata"] = extra
        self.checks.append(entry)

    def write(self, path: Path) -> int:
        payload = {
            "schema": "swerefactor.stage-result/1",
            "stage": "behavioural",
            "unit": self.module,
            "duration_sec": round(time.time() - self.started, 3),
            "checks": self.checks,
            "notes": self.notes,
            "metadata": self.metadata,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        failed = sum(1 for c in self.checks if c["verdict"] not in ("pass", "skip"))
        print(f"[{self.module}] {len(self.checks) - failed}/{len(self.checks)} "
              f"passed, wrote {path}")
        return 1 if failed else 0


# --------------------------------------------------------------------------- #
# Frozen assets
# --------------------------------------------------------------------------- #


class Assets:
    """The frozen grading inputs, verified to be the ones the image was built with.

    A mismatch is a broken verifier rather than a failing submission, so it
    aborts.  Scoring a submission against expectations that changed after they
    were frozen produces a number that means nothing, and a zero is worse than an
    error because it looks like a verdict.
    """

    def __init__(self, root: Path, log: Log) -> None:
        self.root = root
        self.log = log
        self.manifest = vlib.read_json(root / "verifier-manifest.json")
        self.contract = vlib.read_json(root / "source-contract.json")
        self.catalog = vlib.read_json(root / "catalog.json")
        self.corpus_meta = vlib.read_json(root / "corpus" / "corpus.json")
        self.corpus_dir = root / "corpus" / "docs"
        self.baseline = root / "baseline"
        self.store = oracle.ExpectationStore(
            root / "oracle" / "expectations.json",
            root / "oracle" / "expectations.bin",
        )
        digest = self.store.load()
        want = self.manifest["digests"]
        got = {"catalog": self.catalog["digest"],
               "corpus": self.corpus_meta["digest"],
               "oracle": digest}
        for key, expected in sorted(want.items()):
            if got.get(key) != expected:
                raise SystemExit(
                    f"verifier asset mismatch for {key}: image recorded "
                    f"{expected}, found {got.get(key)}. The grading inputs are "
                    f"not the frozen ones; refusing to score.")
        if self.store.meta.get("catalog_digest") != self.catalog["digest"]:
            raise SystemExit("the expectations were frozen against a different "
                             "catalog; refusing to score")
        self.reference: dict[str, Path] = {}
        for config in CONFIGS:
            prefix = root / "reference" / f"ref-install-{config}"
            if not (prefix / "bin" / "cmark").is_file():
                raise SystemExit(f"reference install missing at {prefix}")
            self.reference[config] = prefix

    def slice(self, families: tuple[str, ...] = (), kind: str = "") -> dict:
        """A catalog holding only the cases one module owns.

        The same shape as the whole catalog, so oracle.collect and the graders
        take it unmodified -- which is what keeps the module split from being a
        rewrite of the engine.
        """
        if kind:
            cases = [c for c in self.catalog["cases"] if c["kind"] == kind]
        else:
            wanted = set(families)
            cases = [c for c in self.catalog["cases"]
                     if c["family"] in wanted and c["kind"] in ("probe", "cli")]
        return {**self.catalog, "cases": cases}


# --------------------------------------------------------------------------- #
# The shared build
# --------------------------------------------------------------------------- #

BUILD_STATE = "build.json"


def snapshot(submitted: Path, dest: Path, log: Log) -> dict:
    """Copy the submission, then work only on the copy.

    The record taken here is of the tree *as submitted*: a .o deleted by the
    build would otherwise disappear from the evidence stage 1 reads.
    """
    if not submitted.is_dir():
        raise SystemExit(f"no submission at {submitted}")
    if dest.exists():
        shutil.rmtree(dest)
    files = vlib.copy_tree(submitted, dest)
    inventory = audit.walk_source(dest)
    by_suffix: dict[str, int] = {}
    for path in inventory:
        suffix = path.suffix.lower() or "<none>"
        by_suffix[suffix] = by_suffix.get(suffix, 0) + 1
    log.write(f"snapshot: {files} file(s) copied, {len(inventory)} source file(s)")
    for name in DISCARD_DIRS:
        for stale in dest.rglob(name):
            if stale.is_dir():
                shutil.rmtree(stale, ignore_errors=True)
    return {"files": files, "source_files": len(inventory), "by_suffix": by_suffix}


def run_build(assets: Assets, work: Path, submitted: Path, emit: Emitter,
              log: Log) -> int:
    """Build and install both link configurations, and publish where they went.

    Both are graded because they fail differently: a Rust cdylib and a Rust
    staticlib have different symbol visibility and different link requirements,
    and a submission that only ever tried one usually cannot install the other.
    """
    repo = work / "repo"
    emit.metadata["snapshot"] = snapshot(submitted, repo, log)

    shim_dir = work / "shim"
    shim_mode = _shim_mode()
    if shim_mode != SHIM_OFF:
        buildmod.install_shim(HERE / "shim" / "ccshim.py", shim_dir, log)
        log.write(f"shim: mode={shim_mode}")
    else:
        # Diagnostic only.  With no shim on PATH the ledger stays empty, and an
        # empty ledger reads as "the compilers were never invoked" -- which is
        # indistinguishable from a clean Rust build.  A run that takes this path
        # cannot be used to answer `compiler-shim-clean`, so it says so.
        shim_dir.mkdir(parents=True, exist_ok=True)
        emit.notes.append(
            "DIAGNOSTIC RUN: SRB_SHIM=off, so no compiler shim was installed and "
            "the ledger is empty. compiler-shim-clean cannot be answered from this "
            "run; it is not a valid grading result.")
        log.write("shim not installed (SRB_SHIM=off): diagnostic mode")
    if shim_mode == SHIM_ENFORCE:
        emit.notes.append(
            "DIAGNOSTIC RUN: SRB_SHIM=enforce, so a repository C compile failed "
            "the build instead of being recorded. The graded path is 'record'; a "
            "score from this run understates any submission that still compiles C, "
            "because the behavioural cases had no library to ask.")

    state: dict[str, Any] = {"repo": str(repo), "configs": {}, "shim": str(shim_dir),
                             "shim_mode": shim_mode}
    scratch = work / "scratch"
    for config in CONFIGS:
        builder = buildmod.Builder(repo, work, shim_dir, log, config=config,
                                   shim_mode=shim_mode)
        outcome = builder.run_all()
        if outcome.installed:
            # The supplementary probes only mean anything once a normal build has
            # succeeded; on a broken tree they produce noise.
            builder.probe_rebuild()
            builder.probe_reconfigure()
            builder.probe_cache()
            builder.probe_generator(scratch, "Unix Makefiles")
            builder.probe_testing_off(scratch)
            builder.probe_insource(scratch)
        for step, ok in (("configure", outcome.configured),
                         ("compile", outcome.built),
                         ("install", outcome.installed)):
            emit.add(f"build/{config}/{step}", "pass" if ok else "fail",
                     f"cmake {step} of the {config} configuration",
                     detail="" if ok else _step_detail(outcome, step))
        state["configs"][config] = {
            "prefix": str(outcome.prefix),
            "build_dir": str(outcome.build_dir),
            "installed": bool(outcome.installed),
            # persist() writes the streams to disk and records their paths, so the
            # structure module reads the same logs this one saw rather than a
            # clipped copy of them.
            "outcome": outcome.persist(work / "state"),
        }
        log.write(f"build[{config}]: configured={outcome.configured} "
                  f"built={outcome.built} installed={outcome.installed}")

    # The probe is compiled against the submission's own installed header and
    # library, so a behavioural case that fails later is a behavioural difference
    # rather than a linking problem -- the linking problem fails here instead.
    struct_dir = scratch / "struct"
    probe_ok = False
    if state["configs"]["shared"]["installed"]:
        struct_dir.mkdir(parents=True, exist_ok=True)
        # The same path structure.check_consumer_probe uses, so the structure
        # module finds this binary where it expects to and does not rebuild it.
        result = buildmod.compile_probe(
            HERE / "probe" / "probe.c",
            Path(state["configs"]["shared"]["prefix"]),
            structure.probe_binary_path(struct_dir, "shared"),
            log, label="shared")
        probe_ok = bool(result.ok)
        emit.add("build/probe", "pass" if probe_ok else "fail",
                 "the API probe compiles and links against the installed library",
                 detail="" if probe_ok else result.tail(lines=40, limit=2000))
    else:
        emit.add("build/probe", "fail",
                 "the API probe could not be built: the shared configuration did "
                 "not install")
    state["probe"] = str(structure.probe_binary_path(struct_dir, "shared"))
    state["probe_ok"] = probe_ok
    state["struct_dir"] = str(struct_dir)

    (work / BUILD_STATE).write_text(json.dumps(state, indent=2) + "\n",
                                    encoding="utf-8")
    return 0


def _step_detail(outcome: buildmod.BuildOutcome, step: str) -> str:
    result = getattr(outcome, step, None)
    if result is None:
        return "the step did not run because an earlier one failed"
    return result.tail(lines=40, limit=2000)


def read_build(work: Path) -> dict:
    path = work / BUILD_STATE
    if not path.is_file():
        raise SystemExit(
            f"{path} is missing: the build module has to run before this one. It "
            f"is declared first and required in suite.toml, so reaching here "
            f"means the suite ran out of order.")
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# The graded slices
# --------------------------------------------------------------------------- #


def run_structure(assets: Assets, work: Path, emit: Emitter, log: Log) -> int:
    state = read_build(work)
    outcomes = _rehydrate(state, log)
    ref = structure.build_ref_info(
        assets.reference["shared"], assets.reference["static"],
        work / "scratch" / "reference", log)
    evaluator = structure.StructureEvaluator(
        Path(state["repo"]), outcomes, ref, assets.contract,
        Path(state["struct_dir"]), HERE / "probe" / "probe.c", log)
    cases = assets.slice(kind="struct")["cases"]
    for case in evaluator.evaluate(cases):
        emit.add(case.case_id, "pass" if case.passed else "fail",
                 case.detail or case.case_id, weight=case.weight,
                 detail=getattr(case, "diff", "") or "")
    return 0


def run_provenance(assets: Assets, work: Path, emit: Emitter, log: Log) -> int:
    """The audit gates that have a built artifact to point at.

    Each is worth its own weight.  A submission that ships a library importing
    `dlopen` and one that merely forgot to bump a version are both defects, but they
    are not the same defect.

    Every gate left here measures the *install*: a symbol the shipped ELF imports,
    the set of libraries a prefix received, six renderings of a document composed at
    grading time from random bytes.  The auditor is not given the submitted tree at
    all, which is the mechanical statement of that -- a gate here cannot assert about
    the repository because it cannot see the repository.  It still gets `baseline`,
    which is State A, because `no-c-provenance` needs the *names* of State A's C
    files to look for them in the shipped symbol table.
    """
    state = read_build(work)
    outcomes = _rehydrate(state, log)
    auditor = audit.IntegrityAuditor(
        outcomes, assets.baseline, assets.reference,
        assets.contract, work / "scratch" / "audit", log)

    declared = {case["check"]: case for case in assets.catalog["cases"]
                if case["kind"] == "guard"}
    unknown = sorted(set(PROVENANCE_GATES) - set(declared))
    if unknown:
        raise SystemExit(f"PROVENANCE_GATES names checks the catalog does not "
                         f"declare: {unknown}")
    # Every guard the catalog declares is either measured here or answered by
    # stage 1.  A guard in neither list would silently stop being asked.
    orphans = sorted(set(declared) - set(PROVENANCE_GATES) - set(SEMANTIC_GATES))
    if orphans:
        raise SystemExit(
            f"guard checks claimed by neither this module nor stage 1: {orphans}. "
            f"Add each to PROVENANCE_GATES or to SEMANTIC_GATES and the stage 1 "
            f"prompt, or it stops being graded without anything failing.")

    cases = [declared[check] for check in PROVENANCE_GATES]
    for outcome in auditor.evaluate(cases):
        recorded_only = outcome.check in RECORDED_ONLY_GATES
        note = declared[outcome.check].get("note") or outcome.check
        emit.add(outcome.gate_id, "pass" if outcome.passed else "fail",
                 note + (" [recorded, not scored: stage 1 owns this]"
                         if recorded_only else ""),
                 weight=0.0 if recorded_only else 1.0, detail=outcome.detail,
                 evidence=list(outcome.evidence or [])[:12], check=outcome.check)
    emit.metadata["deferred_to_stage_1"] = list(SEMANTIC_GATES)
    emit.metadata["recorded_not_scored"] = sorted(RECORDED_ONLY_GATES)
    return 0


def run_slice(assets: Assets, work: Path, module: str, emit: Emitter,
              log: Log) -> int:
    """Run one family group's cases and compare against the frozen expectations."""
    state = read_build(work)
    catalog = assets.slice(families=MODULES[module])
    emit.metadata["cases"] = len(catalog["cases"])
    if not catalog["cases"]:
        raise SystemExit(f"module {module!r} selected no cases; the family names "
                         f"in MODULES no longer match the catalog")

    probe = Path(state["probe"])
    cli = Path(state["configs"]["shared"]["prefix"]) / "bin" / "cmark"
    if not state.get("probe_ok") or not probe.is_file() or not cli.is_file():
        # Nothing is measurable, and every case is recorded as failed rather than
        # skipped: a submission that produced no library has not passed these
        # behaviours, it has prevented them from being asked about.
        missing = "probe" if not probe.is_file() else "cmark"
        emit.notes.append(f"no {missing} to run: every case is recorded as failed "
                          f"because nothing was measurable")
        for case in catalog["cases"]:
            emit.add(case["id"], "fail",
                     f"not run: the submission produced no {missing}",
                     weight=float(case.get("weight", 1.0)))
        return 1

    env = vlib.base_env(
        LD_LIBRARY_PATH=str(Path(state["configs"]["shared"]["prefix"]) / "lib"))
    records, crashes = oracle.collect(catalog, probe, cli, assets.corpus_dir, log,
                                      label=f"submission/{module}", env=env)
    if crashes:
        emit.metadata["crashes"] = crashes[:16]
        emit.notes.append(f"the probe crashed on {len(crashes)} case(s)")
    _grade(catalog["cases"], records, assets.store, emit)
    return 0


def _grade(cases: list[dict], records: dict[str, tuple[str, bytes]],
           store: oracle.ExpectationStore, emit: Emitter) -> None:
    """Compare each case's records against the frozen expectations.

    A case with several assertions passes only when every one matches: the same
    behaviour probed at four wrap widths is one claim about the renderer, and
    three-quarters of a claim is not three-quarters of a pass.
    """
    for case in cases:
        if case["kind"] == "probe":
            keys = [f"probe/{row[0]}" for row in case.get("asserts", [])]
        else:
            keys = [f"cli/{case['id']}"]
        failures: list[str] = []
        diff = ""
        for key in keys:
            expected = store.get(key)
            if expected is None:
                failures.append(f"{key}: no expectation was frozen for this case")
                continue
            actual = records.get(key)
            if actual is None:
                failures.append(f"{key}: the submission produced no result")
                continue
            status, payload = actual
            if status != expected.status:
                failures.append(
                    f"{key}: status {status!r}, reference {expected.status!r}")
                continue
            if vlib.sha256_bytes(payload) == expected.digest:
                continue
            failures.append(f"{key}: output differs")
            if not diff:
                diff = vlib.unified_diff(store.payload(key), payload, limit=24)
        summary = case.get("note") or case["id"]
        detail = ""
        if failures:
            detail = "; ".join(failures[:6])
            if len(failures) > 6:
                detail += f" (+{len(failures) - 6} more)"
        emit.add(case["id"], "pass" if not failures else "fail", summary,
                 weight=float(case.get("weight", 1.0)),
                 detail=(detail + ("\n" + diff if diff else "")).strip(),
                 family=case["family"], asserts=len(keys))


def _rehydrate(state: dict, log: Log) -> dict[str, buildmod.BuildOutcome]:
    """Rebuild the BuildOutcome objects the structure evaluator expects.

    The build ran in another process, so what survives is what it wrote down.
    structure.py reads more than the booleans -- it greps the compile log, checks
    the return code of the in-source probe, opens CMakeCache.txt -- so the streams
    are restored from disk in full rather than from the clipped report.
    """
    outcomes: dict[str, buildmod.BuildOutcome] = {}
    for config, info in state["configs"].items():
        outcomes[config] = buildmod.BuildOutcome.restore(info["outcome"])
    return outcomes


def _self_check(assets_root: Path) -> int:
    """Prove the module map covers the frozen catalog, at image build time.

    Everything here is also checked on the first module of a real run, which is
    exactly the problem: by then six stage-3 rounds have been budgeted and the
    thing that went wrong is a typo in a family name.  A case family that no
    module claims does not fail anything at grading time -- it silently stops
    being graded -- so this is the check most worth moving earlier.
    """
    catalog = vlib.read_json(assets_root / "catalog.json")
    _check_total(catalog)

    declared = {case["check"] for case in catalog["cases"]
                if case["kind"] == "guard"}
    for name, gates in (("PROVENANCE_GATES", PROVENANCE_GATES),
                        ("SEMANTIC_GATES", SEMANTIC_GATES)):
        unknown = sorted(set(gates) - declared)
        if unknown:
            raise SystemExit(f"{name} names checks the catalog does not "
                             f"declare: {unknown}")
    orphans = sorted(declared - set(PROVENANCE_GATES) - set(SEMANTIC_GATES))
    if orphans:
        raise SystemExit(f"guard checks claimed by neither the provenance module "
                         f"nor stage 1: {orphans}")
    overlap = sorted(set(PROVENANCE_GATES) & set(SEMANTIC_GATES))
    if overlap:
        raise SystemExit(f"guard checks claimed by both: {overlap}; a gate that "
                         f"is measured and judged is scored twice")
    stray = sorted(RECORDED_ONLY_GATES - set(PROVENANCE_GATES))
    if stray:
        raise SystemExit(f"RECORDED_ONLY_GATES names gates this module does not "
                         f"measure: {stray}; a gate cannot be un-scored here and "
                         f"absent here at the same time")

    counts: dict[str, int] = {}
    for case in catalog["cases"]:
        if case["kind"] == "guard":
            continue
        for module, families in MODULES.items():
            if case["family"] in families:
                counts[module] = counts.get(module, 0) + 1
                break
        else:
            if case["kind"] in KIND_MODULES.values():
                inverse = {v: k for k, v in KIND_MODULES.items()}
                key = inverse[case["kind"]]
                counts[key] = counts.get(key, 0) + 1
    # The catalog's own `behavioural` count excludes both `struct` and `guard`
    # cases; this total excludes only `guard`, because the structure module
    # grades its 46 the same way every other module grades its own.
    graded = sum(counts.values())
    behavioural = catalog["counts"]["behavioural"]
    structural = graded - behavioural
    scored_gates = len(PROVENANCE_GATES) - len(RECORDED_ONLY_GATES)
    print(f"module map ok: {len(MODULES) + len(KIND_MODULES)} case modules, "
          f"{graded} graded cases ({behavioural} behavioural + {structural} "
          f"structural), {len(PROVENANCE_GATES)} measured gates "
          f"({scored_gates} scored + {len(RECORDED_ONLY_GATES)} recorded only), "
          f"{len(SEMANTIC_GATES)} deferred to stage 1")
    for module in sorted(counts):
        print(f"  {module:<14} {counts[module]:>5} cases")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--module")
    parser.add_argument("--self-check", action="store_true",
                        help="check the module map against the frozen catalog "
                             "and exit; used at image build time")
    parser.add_argument("--assets", type=Path,
                        default=Path(os.environ.get("SWEREFACTOR_ASSETS",
                                                    "/opt/assets")))
    parser.add_argument("--repo", type=Path,
                        default=Path(os.environ.get("SRB_REPO", "/workspace/repo")))
    parser.add_argument("--work", type=Path,
                        default=Path(os.environ.get("SRB_SUITE_WORK", "/tmp/suite")))
    parser.add_argument("--result", type=Path,
                        default=Path(os.environ.get("SRB_RESULT", "result.json")))
    parser.add_argument("--log", type=Path,
                        default=Path(os.environ.get("SRB_WORK", "/tmp"))
                        / "module.log")
    args = parser.parse_args(argv)

    if args.self_check:
        return _self_check(args.assets)
    if not args.module:
        parser.error("--module is required (or --self-check)")

    module = args.module
    known = set(MODULES) | set(KIND_MODULES) | {"build"}
    if module not in known:
        parser.error(f"unknown module {module!r}; known: {', '.join(sorted(known))}")

    args.work.mkdir(parents=True, exist_ok=True)
    args.log.parent.mkdir(parents=True, exist_ok=True)
    log = Log(args.log)
    emit = Emitter(module)
    try:
        assets = Assets(args.assets, log)
        _check_total(assets.catalog)
        if module == "build":
            rc = run_build(assets, args.work, args.repo, emit, log)
        elif module == "structure":
            rc = run_structure(assets, args.work, emit, log)
        elif module == "provenance":
            rc = run_provenance(assets, args.work, emit, log)
        else:
            rc = run_slice(assets, args.work, module, emit, log)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        # A module that dies writes what it has.  The runner treats an empty
        # result as an error and charges the module its weight, which is right --
        # but a partial result plus the traceback is what makes it debuggable.
        import traceback
        emit.notes.append(f"{type(exc).__name__}: {exc}")
        emit.metadata["traceback"] = traceback.format_exc()[-4000:]
        emit.write(args.result)
        log.write(f"module {module} raised: {exc}")
        return 1
    emit.write(args.result)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
