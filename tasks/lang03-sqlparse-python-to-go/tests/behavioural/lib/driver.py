#!/usr/bin/env python3
"""Runs one behavioural module of lang03-sqlparse-python-to-go.

The grading engine for this task -- the document generator, the case catalog, the
four-tier probe, the Go build wrapper, the ELF reader -- is unchanged by the
modular suite.  What changed is who calls it: instead of one process that builds,
measures and scores in a fixed order, each module is its own process that asks
this driver for one slice of the catalog.

The slices are not arbitrary.  ``catalog.py`` already tags every case with a
family, and the families already group the way a reviewer would group them: the
lexer, the token types, the grouping pass, statement splitting, the formatter, the
keyword tables, the filter stack, the public API, the command line.  MODULES below
is that grouping made explicit, and the mapping is checked to be total -- a family
nobody claims is a case that would silently stop being graded, so it fails the
image build instead.

What has to be shared is the build, because it is minutes of Go compilation and
sixteen modules need its output.  The `build` module runs first, builds and
installs into $SRB_SUITE_WORK, compiles the four probe tiers and the conformance
consumer, and writes build.json; everything after it reads that file.  Nothing
else crosses the process boundary.

Usage:
    driver.py --module build                # the shared build, first and required
    driver.py --module lexer                # one slice of the catalog
    driver.py --module structure            # what the build produced
    driver.py --module provenance           # was the answer computed
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
# `python3 -I` implies -P, which drops the script's own directory from sys.path.
# run-module.sh runs this file that way, so the sibling imports below need the
# directory put back explicitly.  freeze.py does the same thing for the same
# reason.
sys.path.insert(0, str(HERE))

import build as buildmod  # noqa: E402
import executor  # noqa: E402
import golib  # noqa: E402
import oracle  # noqa: E402
import provenance as provenancemod  # noqa: E402
import structure  # noqa: E402
import vlib  # noqa: E402
from vlib import Log  # noqa: E402

TASK = "lang03-sqlparse-python-to-go"
MANIFEST_SCHEMA = "swerefactor-verifier-manifest-v1"

# Directories a submission may leave behind that must not be reused.  Rebuilding
# from source is the point: a populated bin/ or a stale build cache could have been
# produced by anything, including a build with network access or a toolchain this
# container does not have.  `.git` goes too -- the graded build has no use for
# history, and a repository with a packfile in it is megabytes copied per module.
DISCARD_DIRS = ("bin", "build", "_build", "out", "dist", "install", "vendor-cache",
                "node_modules", ".git", "__pycache__", ".cache", ".gocache",
                ".venv", "venv")

# --------------------------------------------------------------------------- #
# Which catalog families each module owns.
#
# Every family in catalog.py appears exactly once.  `_check_total` proves it, so
# adding a family without claiming it is an image-build failure rather than a
# quiet loss of coverage.
#
# The grouping follows what each family actually probes, which is not always what
# its name suggests -- `kwdialect` is declared in the keywords section of the
# catalog but answers from the `parts` tier, because the keywords tier's import
# closure has no lexer in it, and `validate` sits in `parts` because both probe
# halves call the validator directly.  Tier membership is the probe's business and
# does not constrain this map: a module may span tiers, and several do.
# --------------------------------------------------------------------------- #
MODULES: dict[str, tuple[str, ...]] = {
    # A. the lexer: what it emits, and what state it carries between statements
    "lexer":        ("lex", "lexstate"),
    # B. tokens: the stream, and the ttype hierarchy each token is tagged with
    "tokens":       ("tokenize", "ttype"),
    # C. the grouping pass that turns a flat stream into a tree
    "grouping":     ("tree",),
    # D. statements: the objects, and the two splitting entry points
    "statements":   ("stmt", "split", "splitstream"),
    # E. parse then concatenate reproduces the input, over the whole document set
    "roundtrip":    ("roundtrip",),
    # F. the formatter, deep: every preset against the format-target documents
    "format":       ("format",),
    # G. the formatter, broad: two presets against every document there is
    "format-wide":  ("format-wide",),
    # H. encodings, and the exception each malformed input raises
    "encoding":     ("encoding", "error-type"),
    # I. the nine keyword tables, the per-word lookups, and the dialect regexes
    "keywords":     ("kwtable", "keyword", "kwdialect"),
    # J. the filter stack and the individual filters
    "filters":      ("filter", "stack"),
    # K. the published API: helpers, validation, and the option keys it accepts
    "api":          ("api", "util", "validate", "optkeys"),
    # L. the installed sqlformat command
    "cli":          ("cli",),
}

#: Families the modular suite does not slice by family.
#:
#: `structure` is selected by case *kind* rather than family, because a structural
#: case is not a document being parsed -- it is a question about what the build
#: produced.  `provenance` likewise: those cases ask whether an answer was
#: computed, which is a question about the run rather than about a document.
#:
#: lang03's catalog declares no `guard` kind, because the audit questions here
#: are stage 1's: there is nothing to divide between a measured half and a judged
#: half, and so no guard-kind reconciliation for this file to perform.  The
#: equivalent check for stage 1 is its own suite's, between scan.toml and
#: prompt.txt, and it cannot be made from inside this image.
KIND_MODULES = {"structure": "struct", "provenance": "provenance"}
STAGE_ONE_FAMILIES = ()

BUILD_STATE = "build.json"


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


def probe_label(scope: str) -> str:
    """The log label for the probe session answering `scope`'s cases.

    One function rather than a format string at each call site, so that
    `_check_probe_labels` can test the labels the driver really builds instead of
    a list of labels somebody wrote down next to it.
    """
    return f"submission/{scope}"


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
        if verdict == "skip" and summary:
            # A case's summary is written as the finding it would make -- "go.mod
            # exists, declares the contract module path", "the graded build never
            # invoked a Python interpreter".  Left alone on a skipped case it reads
            # as an assertion about this submission, and the reader has to notice
            # the verdict column to know it is not one.  The marker goes here
            # rather than at each call site so every module gets the same rule.
            summary = f"not asked: {summary}"
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

    Every module constructs one of these, so the digest check runs per module
    rather than once per suite.  That is deliberate: the modules are separate
    processes and nothing guarantees they see the same filesystem state, so the
    cheap recomputation is what makes each module's result independently sound.
    """

    def __init__(self, root: Path, log: Log) -> None:
        self.root = root
        self.log = log
        self.manifest = vlib.read_json(root / "manifest.json")
        if self.manifest.get("schema") != MANIFEST_SCHEMA:
            raise SystemExit(
                f"verifier corrupted: manifest schema is "
                f"{self.manifest.get('schema')!r}, expected {MANIFEST_SCHEMA!r}")
        if self.manifest.get("task") != TASK:
            raise SystemExit(
                f"verifier corrupted: manifest is for "
                f"{self.manifest.get('task')!r}, this is {TASK!r}")

        # `assets/` is the staged subtree freeze.py wrote: the probe halves, the
        # API dumper, the interpreter shim, the contract, and State A.  The frozen
        # answers and the document set sit beside it.
        self.staged = root / "assets"
        self.contract = vlib.read_json(self.staged / "source-contract.json")
        self.catalog = vlib.read_json(root / "catalog.json")
        self.docs_meta = vlib.read_json(root / "documents.json")
        self.docs_root = root
        self.docs_dir = root / "docs"
        self.spec_json = root / "spec.json"
        self.baseline = self.staged / "baseline"
        self.probe_src = self.staged / "probe"
        self.apidump_src = self.staged / "apidump"
        self.shim_src = self.staged / "shim" / "pyshim.py"

        self.store = oracle.ExpectationStore(
            root / "expectations.json", root / "expectations.bin")
        store_digest = self.store.load()

        # Every digest the manifest records, recomputed.  Collected as pairs rather
        # than checked one at a time so a corrupted image reports everything that
        # moved, not just the first thing.
        found = {
            "catalog_digest": self.catalog["digest"],
            "documents_digest": self.docs_meta["digest"],
            "expectations_digest": store_digest,
            "spec_digest": vlib.sha256_file(self.spec_json),
            "keywords_digest": vlib.sha256_file(root / "keywords.json"),
            "contract_digest": vlib.sha256_file(
                self.staged / "source-contract.json"),
        }
        drift = [f"{key}: image recorded {self.manifest.get(key)}, found {value}"
                 for key, value in sorted(found.items())
                 if self.manifest.get(key) != value]
        for rel, want in sorted(self.manifest.get("assets", {}).items()):
            path = self.staged / rel
            if not path.is_file():
                drift.append(f"assets/{rel}: recorded but absent from the image")
            elif vlib.sha256_file(path) != want:
                drift.append(f"assets/{rel}: content differs from the frozen digest")
        if drift:
            raise SystemExit(
                "verifier corrupted: the grading inputs are not the frozen ones, "
                "so nothing graded against them would mean anything:\n  "
                + "\n  ".join(drift[:20])
                + (f"\n  (+{len(drift) - 20} more)" if len(drift) > 20 else ""))

        # The store and the catalog have to be from the same freeze.  They carry
        # separate digests and are loaded separately, so a half-updated image would
        # otherwise grade this catalog's cases against the previous one's answers.
        if self.store.meta.get("catalog_digest") != self.catalog["digest"]:
            raise SystemExit(
                "verifier corrupted: the expectations were frozen against a "
                f"different catalog ({self.store.meta.get('catalog_digest')} vs "
                f"{self.catalog['digest']})")
        if self.store.meta.get("documents_digest") != self.docs_meta["digest"]:
            raise SystemExit(
                "verifier corrupted: the expectations were frozen against a "
                "different document set")

        self.expect = structure.Expectations.load(self.contract)
        log.write(
            f"assets OK: {self.catalog['counts']['total']} cases, "
            f"{self.docs_meta['count']} documents, "
            f"{len(self.store.entries)} expectations, reference sqlparse "
            f"{self.store.meta.get('reference_version')}")

    def slice(self, families: tuple[str, ...] = (), kind: str = "") -> dict:
        """A catalog holding only the cases one module owns.

        The same shape as the whole catalog, so the case selectors in oracle.py
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


def snapshot(submitted: Path, dest: Path, log: Log) -> dict:
    """Copy the submission, then never touch the original again.

    Two reasons.  The copy is what gets built, so the graded build cannot be
    contaminated by whatever the agent's container left in place -- the discarded
    directories are rebuilt from source or not used at all.  And `GOFLAGS=-mod=mod`
    is in the published build contract, which means the toolchain may rewrite
    go.mod and create go.sum as a side effect of building; the tree as submitted
    has to survive that, because stage 1 reads it.

    The record is taken *before* the discards, so a build directory the submission
    shipped still appears in the inventory it is counted in.
    """
    if not submitted.is_dir():
        raise SystemExit(f"no submission at {submitted}")
    if dest.exists():
        shutil.rmtree(dest)
    copied = vlib.copy_tree(submitted, dest)

    inventory = [p for p in sorted(dest.rglob("*")) if p.is_file()]
    by_suffix: dict[str, int] = {}
    total_bytes = 0
    for path in inventory:
        key = path.suffix.lower() or "<none>"
        by_suffix[key] = by_suffix.get(key, 0) + 1
        try:
            total_bytes += path.stat().st_size
        except OSError:
            pass

    discarded: list[str] = []
    for current, dirs, _ in os.walk(dest, topdown=True):
        for name in list(dirs):
            if name in DISCARD_DIRS:
                victim = Path(current) / name
                discarded.append(str(victim.relative_to(dest)))
                shutil.rmtree(victim, ignore_errors=True)
                dirs.remove(name)

    go_files = [p for p in inventory if p.suffix == ".go"]
    py_files = [p for p in inventory if p.suffix == ".py"]
    # Non-test Go only, and through the same counter the contracted floor is
    # enforced with, so this number and that verdict cannot disagree.
    logic, _, _, _ = golib.count_logic_lines(
        [p for p in go_files if not p.name.endswith("_test.go")])
    record = {
        "files_copied": copied,
        "files": len(inventory),
        "bytes": total_bytes,
        "by_suffix": dict(sorted(by_suffix.items())),
        "discarded_dirs": sorted(discarded),
        "go_files": len(go_files),
        "py_files": len(py_files),
        "go_logic_lines": logic,
    }
    log.write(
        f"snapshot: {copied} file(s) copied, {len(go_files)} .go "
        f"({logic} logic lines), {len(py_files)} .py, "
        f"discarded {len(discarded)} build dir(s)")
    return record


def run_build(assets: Assets, work: Path, submitted: Path, emit: Emitter,
              log: Log) -> int:
    """Build, vet, test, install, and compile everything that links against it.

    The shim is installed first and is the reason this is not just `go build`.
    Every interpreter name a build might reach for resolves to a program that
    refuses and records the attempt, so a submission that shells out to Python
    during its build fails here with a ledger entry rather than succeeding quietly
    on a machine that happens to have an interpreter.

    The compilations after `run_all` are separate closures on purpose: a probe tier
    that does not build costs its own cases and nothing else, and the standalone
    closures are a property of the submission's package layering that a
    whole-module build cannot show.

    Six checks are emitted here and they are the module's own weight.  Everything
    else this function produces is read by later modules, which is why the streams
    are persisted rather than summarised.

    Two builders, chosen by what the tree is rather than by a flag.  A Go module
    takes the path above, unchanged.  A tree with no go.mod and an importable
    sqlparse package is the pre-migration implementation, and it takes the path in
    `_build_python` -- because State A is the oracle every frozen expectation in
    this suite was computed from, and a stage that cannot run its own oracle has no
    zero point to read a submission's shortfall against.  See PyBuilder.
    """
    repo = work / "repo"
    emit.metadata["snapshot"] = snapshot(submitted, repo, log)

    language = buildmod.detect_language(repo)
    emit.metadata["language"] = language
    if language != "go":
        return _build_python(assets, work, repo, emit, log)

    shim_dir = work / "shim"
    if os.environ.get("SRB_SHIM", "enforce") == "enforce":
        buildmod.install_shim(assets.shim_src, shim_dir, log)
    else:
        # Diagnostic only, and only for a Go tree: this is past the language
        # branch, so a pre-migration tree never reaches it -- `_build_python`
        # installs no shim at all, whatever SRB_SHIM says.  What `off` is for is
        # reading a Go build that failed: run it twice, and the difference between
        # the two says whether the shim is what rejected it or the build is simply
        # broken.  Grading runs never take this branch.
        shim_dir.mkdir(parents=True, exist_ok=True)
        emit.notes.append(
            "DIAGNOSTIC RUN: the interpreter shim was disabled, so this Go build "
            "was permitted to run Python and every result that depends on it is "
            "void. This is not a valid grading result.")
        log.write("shim disabled (SRB_SHIM=off): diagnostic run on a go tree")

    # tests_dir is where the Builder finds probe/, apidump/ and the contract it
    # generates the conformance consumer from.  In this image those are the staged
    # copies under assets/, whose digests Assets just verified.
    builder = buildmod.Builder(repo, work, shim_dir, log,
                               tests_dir=assets.staged)
    outcome = builder.run_all()
    builder.standalone_all(
        assets.contract["go_contract"]["standalone_closures"]["closures"])
    builder.compile_tiers()
    builder.compile_consumer()
    builder.dump_api()

    for step, ok, label in (
        ("build", outcome.built, "go build ./... succeeds"),
        ("vet", outcome.vet is not None and outcome.vet.ok,
         "go vet ./... succeeds"),
        ("install", outcome.installed,
         "go install produces the sqlformat command"),
    ):
        emit.add(f"build/{step}", "pass" if ok else "fail", label,
                 detail="" if ok else _step_detail(outcome, step))

    tiers_ok = sorted(t for t in buildmod.PROBE_TIERS if outcome.tier_ok(t))
    emit.add("build/probe-tiers",
             "pass" if len(tiers_ok) == len(buildmod.PROBE_TIERS) else "fail",
             f"all {len(buildmod.PROBE_TIERS)} probe tiers link against the "
             f"submission",
             detail="" if len(tiers_ok) == len(buildmod.PROBE_TIERS) else
             "; ".join(
                 f"{tier}: {outcome.tiers[tier].tail(lines=8)}"
                 if tier in outcome.tiers else f"{tier}: not attempted"
                 for tier in buildmod.PROBE_TIERS if tier not in tiers_ok),
             tiers_built=tiers_ok)

    consumer_ok = outcome.consumer is not None and outcome.consumer.ok
    emit.add("build/consumer", "pass" if consumer_ok else "fail",
             "the conformance consumer compiles against the published API",
             detail="" if consumer_ok else _step_detail(outcome, "consumer"))

    api_ok = outcome.api is not None
    emit.add("build/apidump", "pass" if api_ok else "fail",
             "the submission's public interface could be read",
             detail="" if api_ok else
             "the API dump did not run or produced nothing; the cases that read "
             "the published interface cannot be measured")

    state = {
        "repo": str(repo),
        "shim": str(shim_dir),
        "tiers_built": tiers_ok,
        # persist() writes the streams to disk and records their paths, so later
        # modules read the same logs this one saw rather than a clipped copy.
        # structure.py searches these streams for specific text -- a go directive
        # in a build error, a vet diagnostic naming a package, `go version -m`
        # output -- and a needle elided from a clipped log reads like a clean build.
        "outcome": outcome.persist(work / "state"),
    }
    (work / BUILD_STATE).write_text(json.dumps(state, indent=2) + "\n",
                                    encoding="utf-8")
    log.write(f"build: built={outcome.built} installed={outcome.installed} "
              f"tiers={', '.join(tiers_ok) if tiers_ok else 'none'}")
    return 0


#: The build checks a Python tree cannot be asked, and why each is stage 1's
#: question instead of this stage's.  Emitted as `skip`, which leaves the case out
#: of the denominator rather than failing it -- result.py's `is_scored` is what
#: makes that distinction, and it exists because a skip is the suite's decision
#: about what it can measure, not the submission's about what it did.
PY_BUILD_SKIPS = {
    "vet": "go vet is a Go analyser with no cross-language analogue; a Python "
           "tree has nothing for it to read",
    "consumer": "the conformance consumer is Go source that type-checks against "
                "the contracted packages, so it can only be compiled against a "
                "Go module",
    "apidump": "the API dumper reads Go declarations out of Go source; run "
               "against a Python tree it reports an empty interface, which would "
               "be a pass that measured nothing",
}


def _build_python(assets: Assets, work: Path, repo: Path, emit: Emitter,
                  log: Log) -> int:
    """Build the pre-migration tree, and emit the same six checks the Go path does.

    Same ids, same weights, same module: what changes is which of them this tree can
    answer.  Three are real questions about any implementation -- its source builds,
    it installs a working `sqlformat`, and its library API answers the probe on all
    four tiers -- and those are scored.  Three name Go tooling, and those are
    skipped with the reason recorded.

    The shim is not installed on this path and the notes say so.  Its subject is a
    Go build that reaches for an interpreter; here the interpreter *is* the
    implementation, and a ledger full of entries would be reporting that Python is
    Python.  The question the shim stands in for -- may this tree ship at all -- is
    `no-interpreter-dependency` and `no-python-implementation` in stage 1, both
    required, both asked of the tree rather than of one build's ledger, and either
    failing scores the submission zero before this image runs.  So full marks here
    are not a passing transmutation and the notes say that too.
    """
    emit.notes.append(
        "PRE-MIGRATION TREE: no go.mod and an importable sqlparse package, so this "
        "was measured as the Python implementation the task starts from. Stage 2 "
        "measures behaviour, and this tree is the oracle every expectation here was "
        "frozen from, so a full score is the expected result and is not evidence of "
        "a completed port. Whether a Python tree may be submitted at all is decided "
        "in stage 1 by no-python-implementation, no-interpreter-dependency, "
        "go-present and go-is-primary -- four required gates, each of which this "
        "tree fails, and a required gate failing scores the submission zero.")
    emit.notes.append(
        "The interpreter shim was not installed on this path: it exists to catch a "
        "Go build that reaches for Python, and a Python build reaching for Python "
        "is the implementation running, not a finding.")
    log.write("build: pre-migration tree (python); the shim does not apply")

    builder = buildmod.PyBuilder(repo, work, log,
                                 probe_py=assets.probe_src / "probe.py")
    outcome = builder.run_all()

    for step, ok, label in (
        ("build", outcome.built,
         "the submission's source compiles and its package imports"),
        ("install", outcome.installed,
         "the build produces a working sqlformat command"),
    ):
        emit.add(f"build/{step}", "pass" if ok else "fail", label,
                 detail="" if ok else _step_detail(outcome, step))

    tiers_ok = sorted(t for t in buildmod.PROBE_TIERS if outcome.tier_ok(t))
    all_ok = len(tiers_ok) == len(buildmod.PROBE_TIERS)
    emit.add("build/probe-tiers", "pass" if all_ok else "fail",
             f"all {len(buildmod.PROBE_TIERS)} probe tiers answer against the "
             f"submission",
             detail="" if all_ok else "; ".join(
                 f"{tier}: {outcome.tiers[tier].tail(lines=8)}"
                 if tier in outcome.tiers else f"{tier}: not attempted"
                 for tier in buildmod.PROBE_TIERS if tier not in tiers_ok),
             tiers_built=tiers_ok)

    for step, reason in sorted(PY_BUILD_SKIPS.items()):
        # No "not measurable against a pre-migration tree" preamble: `Emitter.add`
        # prefixes every skipped summary with "not asked", so saying it here too
        # produced "not asked: not measurable ...: go vet is a Go analyser".  The
        # reason is what this call site knows and the marker is the emitter's.
        emit.add(f"build/{step}", "skip", reason)

    state = {
        "repo": str(repo),
        # No shim directory: nothing was installed, and a path here would name a
        # directory that does not exist.  provenance.py reads the ledger through
        # outcome.shim_log, which is absent for the same reason.
        "shim": "",
        "tiers_built": tiers_ok,
        "outcome": outcome.persist(work / "state"),
    }
    (work / BUILD_STATE).write_text(json.dumps(state, indent=2) + "\n",
                                    encoding="utf-8")
    log.write(f"build: built={outcome.built} installed={outcome.installed} "
              f"tiers={', '.join(tiers_ok) if tiers_ok else 'none'}")
    return 0


def _step_detail(outcome: buildmod.BuildOutcome, step: str) -> str:
    result = getattr(outcome, step, None)
    if result is None:
        return "the step did not run because an earlier one failed"
    return result.tail(lines=40, limit=2000)


def read_build(work: Path) -> tuple[dict, buildmod.BuildOutcome]:
    """The shared build's state, and the outcome restored from it."""
    path = work / BUILD_STATE
    if not path.is_file():
        raise SystemExit(
            f"{path} is missing: the build module has to run before this one. It "
            f"is declared first and required in suite.toml, so reaching here "
            f"means the suite ran out of order.")
    state = json.loads(path.read_text(encoding="utf-8"))
    return state, buildmod.BuildOutcome.restore(state["outcome"])


# --------------------------------------------------------------------------- #
# The graded slices
# --------------------------------------------------------------------------- #


def run_structure(assets: Assets, work: Path, emit: Emitter, log: Log) -> int:
    """The cases that ask what the build produced.

    Every one is answered by opening a file the build wrote or running the artefact
    it installed: does the binary parse as ELF, is it static, does its buildinfo
    record -trimpath, does a second build come out byte-identical, does the install
    tree match the contract.  Nothing here reads the submission's source and
    compares it against a description -- that is stage 1's, where a reader can say
    *why* something looks wrong instead of matching a pattern.
    """
    state, outcome = read_build(work)
    checker = structure.Structure(
        Path(state["repo"]), outcome, assets.expect, log,
        pristine=assets.baseline)
    cases = assets.slice(kind="struct")["cases"]
    if not cases:
        raise SystemExit("the catalog declares no struct cases; the structure "
                         "module would grade nothing")
    emit.metadata["language"] = outcome.language
    skipped = 0
    for case in checker.evaluate(cases):
        if case.skipped:
            skipped += 1
            emit.add(case.case_id, "skip",
                     assets_note(assets, case.case_id) or case.case_id,
                     weight=case.weight, detail=case.skip_reason,
                     family=case.family)
            continue
        emit.add(case.case_id, "pass" if case.passed else "fail",
                 assets_note(assets, case.case_id) or case.case_id,
                 weight=case.weight, detail=case.detail,
                 family=case.family)
    if skipped:
        # Said in the notes as well as per case: a reader looking at a module with
        # four scored cases where the catalog declares thirty-two should find the
        # reason in the module's own summary, not only by opening each case.
        emit.notes.append(
            f"{skipped} of {len(cases)} structural case(s) were skipped as not "
            f"applicable to a {outcome.language} tree -- they read Go artifacts "
            f"(a go.mod, an ELF header, a compiled package) that this tree does "
            f"not produce. A skipped case stays in the denominator and scores 0, "
            f"so this module reads {len(cases) - skipped}/{len(cases)}: the label "
            f"records why the answer is missing, not a discount for missing it. "
            f"See structure.PY_STRUCT_SKIP_GROUPS for the reason per case.")
    return 0


def run_provenance(assets: Assets, work: Path, emit: Emitter, log: Log) -> int:
    """Was the answer computed, or looked up.

    Two families.  `shim` opens the ledger the graded build wrote.  `fresh` composes
    SQL from a seed drawn now, puts it to the submission's own binary, and asserts
    the round trip is lossless -- the one family in this stage not graded against a
    frozen expectation, and the reason a submission cannot pass this module from a
    recorded table.

    The runner is built here rather than inside provenance.py.  It is the same
    resolver and the same ProbeDriver every other module uses, pointed at a
    documents directory the caller chooses; passing it in is what stops the
    provenance module from owning a second copy of the probe protocol, which would
    be a second thing that can disagree with the reference.
    """
    state, outcome = read_build(work)
    scratch = work / "scratch" / "provenance"
    env = vlib.base_env()

    def runner(docs_dir: Path, docsmeta: Path, cases: list[dict]):
        """Answer `cases` against `docs_dir` using the submission's own tiers."""
        probework = scratch / "probework"
        probework.mkdir(parents=True, exist_ok=True)
        driver = executor.ProbeDriver(
            executor.submission_argv(
                outcome.tier_bin_dir, docs_dir, docsmeta, assets.spec_json),
            cwd=probework, env=env, log=log,
            label=probe_label("fresh"), progress_every=100,
        )
        return driver.run(cases)

    auditor = provenancemod.Provenance(
        Path(state["repo"]), outcome, scratch, log,
        runner=runner, docs_root=assets.docs_root)
    cases = assets.slice(kind="provenance")["cases"]
    if not cases:
        raise SystemExit("the catalog declares no provenance cases; the "
                         "provenance module would grade nothing")
    skipped = 0
    for case in auditor.evaluate(cases):
        if case.skipped:
            skipped += 1
        emit.add(case.case_id,
                 "skip" if case.skipped else ("pass" if case.passed else "fail"),
                 assets_note(assets, case.case_id) or case.case_id,
                 weight=case.weight, detail=case.detail,
                 family=case.family)
    if skipped:
        # The 40 `fresh` cases carry this module's whole weight and none of them is
        # skippable, so the module still has scored checks; see Provenance.PY_PROV_SKIP.
        emit.notes.append(
            f"{skipped} of {len(cases)} provenance case(s) read scaffolding this "
            f"build does not install and were skipped rather than answered "
            f"vacuously; see provenance.Provenance.PY_PROV_SKIP")
    if auditor.seed is not None:
        # Recorded so a failure is reproducible: the draw is the whole point of the
        # family, and without the seed a report says a submission failed on
        # documents nobody can produce again.
        emit.metadata["fresh_seed"] = str(auditor.seed)
    return 0


def run_slice(assets: Assets, work: Path, module: str, emit: Emitter,
              log: Log) -> int:
    """Run one family group's cases and compare against the frozen expectations.

    Two runners, and the difference matters for how a failure reads.  The probe
    tiers are four binaries compiled against the submission's own packages; a tier
    that did not compile takes only its own cases down, and executor's resolver
    turns that into marked cases rather than a crash.  The CLI runner drives the
    installed `sqlformat`, which is the release artefact -- if it is missing, those
    cases are unmeasurable for a reason already reported as its own structural
    case, and are marked rather than silently dropped.

    Marked, not dropped, is the whole point.  A dropped case shrinks the
    denominator, so a submission that builds nothing would divide zero by zero and
    land wherever the fallback put it.  Every case in the slice gets a verdict on
    every run.
    """
    state, outcome = read_build(work)
    catalog = assets.slice(families=MODULES[module])
    cases = catalog["cases"]
    emit.metadata["cases"] = len(cases)
    if not cases:
        raise SystemExit(f"module {module!r} selected no cases; the family names "
                         f"in MODULES no longer match the catalog")

    probe_cases = oracle.probe_cases_for(catalog)
    cli_cases = oracle.cli_cases_for(catalog)
    records: dict[str, tuple[str, bytes]] = {}
    env = vlib.base_env()

    if probe_cases:
        tiers_ok = sorted(t for t in buildmod.PROBE_TIERS if outcome.tier_ok(t))
        if tiers_ok:
            probework = work / "scratch" / module / "probework"
            probework.mkdir(parents=True, exist_ok=True)
            driver = executor.ProbeDriver(
                executor.submission_argv(
                    outcome.tier_bin_dir, assets.docs_dir,
                    assets.docs_root / "documents.json", assets.spec_json),
                cwd=probework, env=env, log=log,
                label=probe_label(module), progress_every=500,
            )
            answers = driver.run(probe_cases)
            for case_id, record in answers.items():
                records[f"probe/{case_id}"] = (record.status, record.payload)
            if driver.crashes:
                emit.metadata["crashes"] = driver.crashes[:16]
                emit.notes.append(
                    f"the probe crashed on {len(driver.crashes)} case(s)")
            summary = executor.ProbeSummary.of(answers, driver.tier_notes)
            emit.metadata["probe"] = {
                "total": summary.total,
                "answered": summary.answered,
                "by_status": summary.by_status,
                "tier_notes": summary.tier_notes,
                "tiers_built": tiers_ok,
            }
        else:
            emit.notes.append(
                "no probe tier compiled, so no case in this module could be "
                "measured through the library API; each is recorded as failed")
            log.write("probe: no tier binaries, skipping")

    if cli_cases:
        binary = outcome.binary
        if binary.is_file():
            runner = executor.CliRunner(
                binary, assets.docs_dir, log, label="submission",
                work_dir=work / "scratch" / module / "cliwork", env=env,
            )
            for case in cli_cases:
                result = runner.run_case(case)
                status = "timeout" if result.timed_out else f"rc:{result.returncode}"
                records[f"cli/{case['id']}"] = (
                    status, runner.signature(case, result))
        else:
            emit.notes.append(
                "the submission installed no sqlformat command, so every CLI case "
                "in this module is recorded as failed")
            log.write("cli: no installed binary, skipping")

    _grade(cases, records, assets.store, emit)
    return 0


def _grade(cases: list[dict], records: dict[str, tuple[str, bytes]],
           store: oracle.ExpectationStore, emit: Emitter) -> None:
    """Compare each case's record against the frozen expectation.

    One case is one wire request, so there is one key per case and no partial credit
    to apportion.  Status is compared before payload, and deliberately: `err` is a
    graded answer here -- "this input raises" is behaviour the port has to reproduce
    -- so a submission that returns a clean result where the reference raised has
    failed the case, and saying "status ok, reference err" is a more useful failure
    than a diff between an answer and an exception.

    `crash:*`, `timeout` and `defect` never match anything, because the reference
    produced none of them: oracle.freeze aborts the image build if it does.  A
    submission that produces one fails the case and keeps its note.
    """
    missing_expectation = 0
    for case in cases:
        key = (f"probe/{case['id']}" if case["kind"] == "probe"
               else f"cli/{case['id']}")
        expected = store.get(key)
        detail = ""
        diff = ""
        passed = False
        if expected is None:
            # A catalog case with no frozen answer cannot be graded either way. It
            # is counted and reported rather than passed, and the count goes in the
            # notes: at any nonzero number this is a verifier defect.
            missing_expectation += 1
            detail = "no expectation was frozen for this case"
        elif key not in records:
            detail = "not run: the submission produced no result"
        else:
            status, payload = records[key]
            if status != expected.status:
                detail = f"status {status!r}, reference {expected.status!r}"
                if status.startswith("crash:") or status == "timeout":
                    detail += " (the process did not answer)"
                elif expected.status == "err" and status == "ok":
                    detail += " (the reference raises on this input)"
                elif expected.status == "ok" and status == "err":
                    detail += (f" (the reference returns a result; this raised "
                               f"{payload[:120]!r})")
                    diff = vlib.unified_diff(store.payload(key), payload, limit=8)
            elif vlib.sha256_bytes(payload) == expected.digest:
                passed = True
            else:
                want = store.payload(key)
                detail = (f"output differs ({len(want)} reference bytes, "
                          f"{len(payload)} submission bytes)")
                diff = vlib.unified_diff(want, payload, limit=24)
        emit.add(case["id"], "pass" if passed else "fail",
                 case.get("note") or case["id"],
                 weight=float(case.get("weight", 1.0)),
                 detail=(detail + ("\n" + diff if diff else "")).strip(),
                 family=case["family"])
    if missing_expectation:
        emit.notes.append(
            f"verifier defect: {missing_expectation} case(s) in this module have "
            f"no frozen expectation and could not be graded; they are counted as "
            f"failures, which understates the submission")


def assets_note(assets: Assets, case_id: str) -> str:
    """The catalog's own note for a case, used as the check summary."""
    index = getattr(assets, "_note_index", None)
    if index is None:
        index = {case["id"]: case.get("note") or ""
                 for case in assets.catalog["cases"]}
        assets._note_index = index  # noqa: SLF001
    return index.get(case_id, "")


# --------------------------------------------------------------------------- #
# Image-build-time self-check
# --------------------------------------------------------------------------- #


def _check_probe_labels() -> None:
    """Every label the driver builds must yield a stderr file that opens.

    Cheap here, and expensive nowhere else.  A ProbeSession's stderr path is
    interpolated from its label; a label containing a `/` therefore names a path
    inside a directory nothing creates, `open` raises ENOENT, `run_tier` reports
    that as the submission failing to start, and every probe case in every module
    is recorded as a crash.  The shape of that is a stage nobody can pass:
    measured on State A, the smallest probe module goes 0/391, which is a failing
    scored check, so no submission -- a finished port included -- is paid at all.

    Opened rather than pattern-matched.  A regex saying "no slash in the label"
    would have to be kept in step with how the path is built, and it is the build
    that has to hold, not the spelling.  Both scopes are covered: `run_slice`
    labels with a module name and `run_provenance` with "fresh".  Every module
    name is tried rather than only the ones that own probe cases, because a list
    of "modules with probe cases" is a claim that goes stale the next time the
    catalog moves a family.
    """
    scopes = [*MODULES, *KIND_MODULES, "fresh"]
    for scope in scopes:
        label = probe_label(scope)
        path = executor.ProbeSession.stderr_path_for(label, os.getpid(), 0)
        if path.parent != Path("/tmp"):
            raise SystemExit(
                f"probe stderr for label {label!r} would land at {path}, outside "
                f"/tmp: the label reached the path as a directory component")
        try:
            with open(path, "wb"):
                pass
        except OSError as exc:
            raise SystemExit(
                f"probe stderr for label {label!r} cannot be opened at {path}: "
                f"{exc}. A session with this label would fail to start and every "
                f"case routed to it would be recorded as a crash.") from exc
        path.unlink(missing_ok=True)
    print(f"probe labels ok: {len(scopes)} label(s) yield an openable stderr path")


def _self_check(assets_root: Path) -> int:
    """Prove the module map covers the frozen catalog, at image build time.

    Everything here is also checked on the first module of a real run, which is
    exactly the problem: by then a build has been paid for and six verification
    rounds have been budgeted, and the thing that went wrong is a typo in a family
    name.  A family no module claims does not fail anything at grading time -- it
    silently stops being graded -- so this is the check most worth moving earlier.

    The counts printed are derived from the catalog on the spot rather than
    restated from a constant.  A number written here would be a number that can
    disagree with the catalog it claims to describe, and the disagreement would be
    invisible: both would be internally consistent.
    """
    catalog = vlib.read_json(assets_root / "catalog.json")
    _check_total(catalog)
    _check_probe_labels()

    # No guard-kind reconciliation: this catalog declares no guard cases, and a
    # future one that did would be caught here rather than grading them nowhere.
    guards = sorted({case["family"] for case in catalog["cases"]
                     if case["kind"] == "guard"})
    if guards:
        raise SystemExit(
            f"the catalog declares guard-kind cases in {guards}, which this stage "
            f"has no module for -- lang03's audit gates belong to stage 1. "
            f"Either give them a module here or move them to the scan suite; as "
            f"they are, they would be counted and never run.")

    counts: dict[str, int] = {}
    inverse = {kind: name for name, kind in KIND_MODULES.items()}
    for case in catalog["cases"]:
        for module, families in MODULES.items():
            if case["family"] in families:
                counts[module] = counts.get(module, 0) + 1
                break
        else:
            key = inverse.get(case["kind"])
            if key:
                counts[key] = counts.get(key, 0) + 1

    graded = sum(counts.values())
    total = catalog["counts"]["total"]
    if graded != total:
        raise SystemExit(
            f"the module map grades {graded} of the catalog's {total} case(s); "
            f"_check_total passed, so the gap is a case whose kind no module "
            f"selects rather than a family nobody claims")
    behavioural = catalog["counts"]["behavioural"]
    print(f"module map ok: {len(MODULES) + len(KIND_MODULES)} case modules, "
          f"{graded} graded cases ({behavioural} behavioural + "
          f"{graded - behavioural} structural and provenance)")
    for module in sorted(counts):
        print(f"  {module:<14} {counts[module]:>5} cases")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--module")
    parser.add_argument("--self-check", action="store_true",
                        help="check the module map against the frozen catalog "
                             "and exit; used at image build time")
    # /opt/assets, not /opt/swerefactor/assets.  /opt/swerefactor arrives from the
    # shared infra image and holds the runner every task uses; this task's frozen
    # answers are a different thing with a different provenance, so they sit
    # beside it rather than inside it.  suite.toml exports the same path, and the
    # default here is what an operator running a module by hand gets.
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
        # A module that dies writes what it has.  The runner treats an empty result
        # as an error and charges the module its weight, which is right -- but a
        # partial result plus the traceback is what makes it debuggable.
        import traceback
        emit.notes.append(f"{type(exc).__name__}: {exc}")
        emit.metadata["traceback"] = traceback.format_exc()[-4000:]
        emit.write(args.result)
        log.write(f"module {module} raised: {exc}")
        return 1
    finally:
        log.close()
    emit.write(args.result)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
