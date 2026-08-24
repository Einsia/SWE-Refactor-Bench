#!/usr/bin/env python3
"""The stage-2 entry point.  Every module in suite.toml runs this file.

One driver rather than fourteen scripts, because the modules here differ in
which slice of the frozen corpus they own, not in how they run.  `--module <id>`
names the slice; suite.toml names the weight.

WHAT THIS STAGE MAY AND MAY NOT ASSERT

It builds the submission and asks the built artifacts questions.  It does not
read the submission's source, and it does not assert anything about how the port
is organised -- not a crate name, not a module name, not a function name, not a
file that must exist.  Those are stage 1's business, judged by a model with both
trees open, because they are judgements rather than measurements.

The two exceptions are exactly the two things a *distribution* consumes and this
task's instruction promises by name: the workspace publishes four crates at
pinned versions, and `cargo install` puts `acorn` and `acorn-probe` on the PATH.
Those are contract terms, checked through `cargo metadata` and the install
prefix -- never by opening a .rs file.

WHAT IT READS

  /opt/assets       the frozen inputs: baseline tree, built JS reference,
                    corpus, expectations, digests.  Written at image build time,
                    before any submission existed, then made read-only.
  /workspace/repo   the submission, copied once and never mutated in place.
  $SRB_SUITE_WORK   shared scratch.  `build` writes build.json here and every
                    later module reads it; the toolchain output stays put so
                    thirteen modules do not each pay for a cargo build.

THE INTERPRETER TRIPWIRE

Every graded subprocess runs with a PATH whose `node`/`npm`/`npx` are recording
tripwires that exit non-zero.  Two ledgers, never merged: the build's answers
`shim-clean` (did building need an interpreter), and the graded run's answers
`no-runtime-spawn` (did answering a request need one).  Merging them would make
"the build shelled out" and "the parser shells out" indistinguishable, and they
are different defects.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
# The engine lives beside this file.  run-module.sh uses `python3 -I`, which
# implies -P and so drops the script's own directory from sys.path; the engine
# modules import each other by bare name, so the path goes back on here rather
# than being turned into a package.  -I is kept for what it does do: no user
# site directory, no PYTHON* from the environment, nothing a submission wrote.
sys.path.insert(0, str(HERE))

import build as buildmod  # noqa: E402
import catalog  # noqa: E402
import cli as climod  # noqa: E402
import audit  # noqa: E402
import probe as probemod  # noqa: E402
import structure  # noqa: E402
import vlib  # noqa: E402
from vlib import CaseOutcome, GateOutcome, Log  # noqa: E402

TASK = "lang04-acorn-js-to-rust"
MANIFEST_SCHEMA = "swerefactor-verifier-manifest-v1"
# freeze.check_unportable writes it; Assets reads it.  Versioned separately from
# the manifest because it is a separate claim: the manifest says what was frozen,
# this says which of it has no answer to grade.
EXCLUDED_SCHEMA = "lang04-unportable-cases-v1"

#: The frozen inputs whose digests this module verifies, from the inventory that
#: also tells freeze.py what to digest and manifest_check.py what to demand.
FROZEN_INPUTS = catalog.FROZEN_INPUTS

DEFAULT_ASSETS = Path(os.environ.get("SWEREFACTOR_ASSETS", "/opt/assets"))
# SRB_REPO first, because that is the one the behavioural runner actually exports
# (infra/swerefactor/behavioural.py:125) from the `repo` it was constructed with.
# SWEREFACTOR_REPO is this image's ENV default and names the same directory, so
# reading only it worked -- but only by coincidence: the runner's argument was
# inert, and a harness that staged the submission anywhere else would have been
# told "no submission at /workspace/repo" while the tree sat where it had asked
# for it to be graded.  lang01, lang02 and lang03 all read SRB_REPO; this was
# lang04 alone.
DEFAULT_REPO = Path(
    os.environ.get("SRB_REPO")
    or os.environ.get("SWEREFACTOR_REPO")
    or "/workspace/repo"
)
DEFAULT_WORK = Path(os.environ.get("SRB_SUITE_WORK", "/workspace/suite"))
DEFAULT_LOGS = Path(os.environ.get("SWEREFACTOR_LOG_DIR", "/logs/verifier"))

#: Written by `build`, read by everything after it.  suite.toml orders `build`
#: first; it no longer marks it required, because that rule is retired.  A module
#: that finds no build.json says so and names the ordering, rather than silently
#: building a second copy -- which is what carries the dependency now.
BUILD_STATE = "build.json"

# --------------------------------------------------------------------------- #
# The module map
# --------------------------------------------------------------------------- #
# Each behavioural module owns a set of catalog families.  `_check_total` proves
# the partition is exact -- every family claimed once, nothing invented -- and
# raises rather than scoring if it is not, because a family claimed by no module
# is weight that silently disappears and a family claimed by two is weight
# counted twice.

MODULES: dict[str, tuple[str, ...]] = {
    # A. the build everything else stands on
    "build": (),
    # B. the installed deliverable, through cargo metadata and the prefix
    # C. `provenance` is not a module here.  Its six questions are artifact-backed
    #    but semantic, so they are stage 1's alone, and suite.toml does not declare
    #    the module -- listing it here would fail this file's own self-check
    #    ("driver.py implements a module suite.toml does not declare").
    #    PROVENANCE_GATES below is kept regardless: it is still half of the
    #    partition `_check_partition` proves.
    "structure": (),
    # D. the parser, on acorn's own suite
    "parser-suite": ("parse_upstream",),
    # E. the parser through its other public entry points
    "parser-options": ("parse_options", "parse_sourcetype", "parse_collect",
                       "parse_expression_at"),
    # F. the version gates: one accept/reject table per ecmaVersion
    "parser-versions": ("parse_versions",),
    # G. the tokenizer as a public iterator
    "tokenizer": ("tokenize",),
    # H. acorn-loose: error recovery has its own shape
    "loose": ("loose_parse",),
    # I. acorn-walk: four traversal orders
    "walk": ("walk_full", "walk_full_ancestor", "walk_simple", "walk_recursive"),
    # J. the four position queries over a parsed tree
    "find-node": ("find_node_at", "find_node_around", "find_node_after",
                  "find_node_before"),
    # K. the same paths on sources nobody wrote by hand
    "generated": ("generated_parse", "generated_tokenize", "generated_loose",
                  "generated_walk", "generated_collect", "generated_find",
                  "generated_expr_at"),
    # L. the character-class tables and the line machinery
    "tables": ("identifier_tables", "line_info", "newline_tests",
               "utility_static"),
    # M. whole real-world files, end to end
    "whole-libraries": (),
    # N. the installed executable, differential against node
    "cli": (),
}

#: Modules that grade something other than a corpus slice.  A kind here is read
#: as a live mapping, so the key has to be a module `run` dispatches to.
KIND_MODULES = {"structure": "struct"}

#: The guards that stay here, because each one has a built artifact to point at.
#: The line is not "mechanical versus semantic" -- it is whether the gate has an
#: artifact.  Every one of these is answered by opening a file the build
#: produced or a ledger the build wrote.
PROVENANCE_GATES = (
    "shim-clean",              # the build's tripwire ledger
    "no-external-crates",      # cargo metadata + the offline-fetch probe
    "no-build-scripts",        # cargo metadata: custom-build and proc-macro
    "binaries-are-static-rust",  # the shipped ELF's producer metadata
    "no-js-in-binaries",       # the shipped ELF's contents
    "no-runtime-spawn",        # the graded run's tripwire ledger
)

#: The guards stage 1 owns, where a model reads both trees.  Listed here so
#: `run_provenance` can prove the two lists partition the catalog: a guard in
#: neither is a guard nobody answers, and this file is where that would otherwise
#: go unnoticed.
#: The arrow names the gate id in tests/evaluation.toml that owns the question.
#: Several stage-2 guards collapse into one gate, because they are two mechanical
#: halves of a single judgement.
SEMANTIC_GATES = (
    "no-js-sources",              # -> javascript-left
    "no-js-by-content",           # -> javascript-left (same gate, both halves)
    "no-bundled-reference",       # -> no-embedded-reference
    "rust-present",               # -> parser-is-written-here
    "no-interpreter-in-source",   # -> no-interpreter
    "no-verifier-awareness",      # -> no-verifier-awareness
    "no-env-dispatch",            # -> one-implementation
    "single-implementation",      # -> one-implementation (same gate, both halves)
    "no-network",                 # -> no-interpreter (no out-of-process work)
    "no-frozen-answers",          # -> trees-are-computed
)
# No guard defers to an *advisory* gate.  Advisory means `required = false`, which
# puts the answer outside the conjunction `grade_audit` builds the verdict
# from, so the vote would be cast and then read by nothing -- worse than a guard in
# neither list, which `_check_partition` at least reports.  The licences are covered
# regardless: the read-only scan hashes every preserved path against upstream's, so
# an edited `LICENSE` or `AUTHORS` arrives as authored bytes.

#: `workspace-shape` is in neither list on purpose.  It needs `cargo metadata`,
#: so it has an artifact and could live here -- but `structure`'s
#: `workspace-members` and `crate-versions` already measure exactly what it
#: measures, from the same command.  Grading it in both places would charge one
#: defect twice, so it is accounted for as folded into `structure` and the
#: partition check below expects to find it there.
FOLDED_INTO_STRUCTURE = ("workspace-shape",)

# --------------------------------------------------------------------------- #
# What these six can be asked of a tree that has not been migrated yet
# --------------------------------------------------------------------------- #
#
# Nothing, and two of them do not fail when asked -- they pass, and the sentence
# they pass with is false.  Measured on State A built by `build.JsBuilder`:
#
#   FAIL  no-external-crates        cargo metadata did not run; cannot enumerate
#                                   dependencies
#   FAIL  no-build-scripts          cargo metadata did not run; cannot enumerate
#                                   build targets
#   FAIL  binaries-are-static-rust  no ELF binary was installed under <prefix>
#   FAIL  no-js-in-binaries         no shipped binary to inspect
#   PASS  shim-clean                the build invoked no JavaScript interpreter
#   PASS  no-runtime-spawn          the probe answered a parse request; the CLI
#                                   parsed a file (rc=1); with only tripwires on
#                                   PATH
#
# The four failures are honest: the artifact is absent and the gate says so.  The
# two passes are the reason this table exists.  Both read a tripwire ledger, and
# both ledgers are empty for a reason that has nothing to do with what they
# assert: `JsBuilder` sets `uses_shim = False`, because a build whose every step
# is node cannot run behind a tripwire that refuses node; and the launcher it
# installs execs node by absolute path, so the tripwires `spawn_check` puts on
# PATH are never consulted -- `exercised=2, events=0`.
#
# `gate_shim_clean` already guards one version of this ("a build that never ran
# writes the same empty ledger as a build that never wanted an interpreter") by
# checking `built` first, and `gate_no_runtime_spawn` guards its own ("a tree with
# no binaries installed spawns nothing") by counting what it exercised.  A
# succeeding node build defeats both from the side neither anticipated: it built,
# it answered, and it left no ledger.  So the guard cannot be fixed by tightening
# it here -- the missing evidence is not recoverable from the artifacts this path
# produces, which is precisely what makes the case not-asked rather than passing.
#
# `skip`, not `fail`: a gate that reports "no ELF binary was installed" about an
# npm monorepo has measured that the tree is not a Rust port, which is true, was
# never in question, and is stage 1's `javascript-left` to charge for.
JS_PROV_SKIP_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("this reads the tripwire ledger, and on this path the ledger is empty for a "
     "reason unrelated to what the gate asserts: the build's every step is node, "
     "so it cannot run behind a tripwire that refuses node, and the launcher it "
     "installs execs node by absolute path without consulting PATH. Measured, the "
     "gate passes and its sentence is false. The question is owned by stage 1's "
     "required `no-interpreter` gate, and the enforcement was never here in any "
     "case: the tripwire is on the PATH `build` runs under, and `build` is the one "
     "required module, so a submission that needs an interpreter to build scores "
     "zero for the stage before this module is reached",
     ("shim-clean", "no-runtime-spawn")),
    ("this reads the crate graph `cargo metadata` resolves, which exists only "
     "where there is a Cargo.toml to resolve. What it forbids is owned by stage 1: "
     "`parser-is-written-here` on third-party crates (\"any third-party crate is "
     "already a contract break\") and `no-interpreter` on a build script that runs "
     "node or npm",
     ("no-external-crates", "no-build-scripts")),
    ("this opens the shipped binary as an ELF -- for rustc's producer metadata, or "
     "for State A's JavaScript inside it -- and this path installs shell launchers, "
     "so there is no such file to read rather than a bad one. `no-js-in-binaries` "
     "is owned by stage 1's required `no-embedded-reference` (\"a base64 or hex blob "
     "that decodes to JavaScript\"). `binaries-are-static-rust` has no stage-1 "
     "owner and is the one question that becomes advisory here; it corroborates "
     "rather than solely owning, because a submission shipping a non-rustc binary "
     "still has to leave a Rust parser in the tree for `parser-is-written-here` to "
     "find, and a Rust parser cargo built is rustc-produced by construction",
     ("binaries-are-static-rust", "no-js-in-binaries")),
)

JS_PROV_SKIP: dict[str, str] = {check: reason
                                for reason, checks in JS_PROV_SKIP_GROUPS
                                for check in checks}

_PROV_UNCOVERED = set(PROVENANCE_GATES) - set(JS_PROV_SKIP)
_PROV_INVENTED = set(JS_PROV_SKIP) - set(PROVENANCE_GATES)
if _PROV_UNCOVERED or _PROV_INVENTED:
    # Every gate this module runs needs a reason, or the JS path reports a verdict
    # for it again.  Derived from `PROVENANCE_GATES`, not from a count: a table
    # checked against a number typed from itself agrees with itself and is wrong.
    raise SystemExit(
        "JS_PROV_SKIP_GROUPS does not cover PROVENANCE_GATES exactly: "
        + "; ".join(filter(None, [
            f"no reason for {sorted(_PROV_UNCOVERED)}" if _PROV_UNCOVERED else "",
            f"reason for non-gate {sorted(_PROV_INVENTED)}" if _PROV_INVENTED else "",
        ]))
    )

# The two modules an identity run cannot cover, because each needs a submission
# to build and install.  The identity run grades the JavaScript reference against
# the answers frozen from it, which proves the corpus, the framing and the
# comparison; it cannot prove anything about building a Rust tree, and it must not
# be allowed to look as though it did.  The self-check below rejects a name here
# that is not a module, so this list cannot name one nothing implements.
IDENTITY_SKIPS = ("build", "structure")


def _check_partition() -> None:
    """Every catalog guard is claimed exactly once, and nothing is invented.

    A guard in none of the three lists is a guard nobody answers -- in this stage
    or the next -- and it would look identical to a clean submission.  A guard in
    two is charged twice.  Neither is visible from a passing run, so it raises.
    """
    declared = {check for _, check, _, _ in catalog.GUARD_CASES}
    claimed: dict[str, list[str]] = {}
    for label, group in (("provenance", PROVENANCE_GATES),
                         ("stage-1", SEMANTIC_GATES),
                         ("structure", FOLDED_INTO_STRUCTURE)):
        for check in group:
            claimed.setdefault(check, []).append(label)
    problems = []
    for check in sorted(declared - set(claimed)):
        problems.append(f"guard {check!r} is claimed by no stage")
    for check in sorted(set(claimed) - declared):
        problems.append(f"guard {check!r} is claimed but not in the catalog")
    for check, owners in sorted(claimed.items()):
        if len(owners) > 1:
            problems.append(f"guard {check!r} is claimed by {', '.join(owners)}")
    if problems:
        raise SystemExit(
            "driver.py and catalog.py disagree about the guards:\n  "
            + "\n  ".join(problems)
        )


def _check_exclusion(assets: Assets) -> list[str]:
    """Every excluded case reaches a module that will skip it.

    `run_corpus` asserts that the ids the list names in *its* slice were skipped,
    which is the check that catches a join that stopped working.  It cannot catch
    an id in a family no module claims: no module would hold it, so no module
    would notice its absence, and the case would simply never be graded by
    anything -- indistinguishable, in the report, from one that passed.

    So the arrow is followed the other way here, at build time, over the module
    map rather than over one module's slice.
    """
    owner: dict[str, list[str]] = {}
    for module_id, families in MODULES.items():
        for family in families:
            owner.setdefault(family, []).append(module_id)
    problems = []
    for cid in sorted(assets.excluded):
        family = (assets.cases.get(cid) or {}).get("family") or "unknown"
        holders = owner.get(family, [])
        if not holders:
            problems.append(
                f"excluded case {cid} is in family {family!r}, which no module "
                f"claims: nothing would skip it and nothing would ask it"
            )
        elif len(holders) > 1:
            problems.append(
                f"excluded case {cid} is in family {family!r}, claimed by "
                f"{', '.join(holders)}: it would be skipped once per module"
            )
    return problems


def _check_applicability() -> list[str]:
    """What each module asks of each kind of tree, asserted in both directions.

    Two tables decide it -- `structure.JS_STRUCT_KEEP`/`JS_STRUCT_SKIP` and this
    module's `JS_PROV_SKIP` -- and a wrong entry in either is invisible from a
    passing run.  Too few skips and a case fails on a pre-migration tree for
    lacking something it never had; too many and a case that was measurable is
    dropped, which is weight awarded for a check that did not run.

    So both directions are asserted, against `catalog` rather than against a
    written-down list.  A Rust tree must be asked every declared case: that is what
    stops the skip path from being reachable by a real submission at all.  A
    JavaScript tree must be asked exactly the ten `structure` cases measured to
    pass on `build.JsBuilder`'s output and none of the six provenance gates.  The
    counts below are the only place a number is written, and each one is checked
    against the catalog it partitions, never against itself.
    """
    problems: list[str] = []
    declared = [check for _, check, _, _ in catalog.STRUCT_CASES]

    # Direction 1: a Rust tree is asked everything, in both modules.
    for check in declared:
        reason = structure.skip_reason(check, buildmod.LANG_RUST)
        if reason:
            problems.append(
                f"structural check {check!r} is skipped for a Rust tree "
                f"({reason[:60]}...) -- a submission would not be asked it"
            )

    # Direction 2: a JavaScript tree is asked the ten that were measured to pass.
    asked = [c for c in declared if not structure.skip_reason(c, buildmod.LANG_JS)]
    skipped = [c for c in declared if structure.skip_reason(c, buildmod.LANG_JS)]

    # Both tables against the catalog, in both directions.  Comparing `asked` to
    # `JS_STRUCT_KEEP` would be near-vacuous -- `asked` is computed from `KEEP`, so
    # it can only differ where `KEEP` names something the catalog does not declare
    # -- and it would say nothing at all about `JS_STRUCT_SKIP`.  What has to hold
    # is that the two tables partition the declared cases: `structure` raises on an
    # overlap and on a case in neither, and this is the remaining direction, a name
    # in a table that the catalog has since renamed or dropped.
    tabled = set(structure.JS_STRUCT_KEEP) | set(structure.JS_STRUCT_SKIP)
    for check in sorted(tabled - set(declared)):
        table = ("JS_STRUCT_KEEP" if check in structure.JS_STRUCT_KEEP
                 else "JS_STRUCT_SKIP")
        problems.append(
            f"{table} names {check!r}, which catalog.STRUCT_CASES does not declare "
            f"-- a renamed or dropped case leaves an entry that decides nothing"
        )
    for check in sorted(set(declared) - tabled):
        problems.append(
            f"structural case {check!r} is in neither applicability table, so "
            f"whether a pre-migration tree can be asked it has no answer"
        )
    if len(asked) != 10 or len(skipped) != 7:
        problems.append(
            f"a pre-migration tree is asked {len(asked)} structural case(s) and "
            f"skips {len(skipped)}; the measured split is 10 asked and 7 skipped. "
            f"If the catalog changed, re-measure before editing this number: the "
            f"reasons in JS_STRUCT_SKIP_GROUPS record what each case did, not what "
            f"it was expected to do"
        )
    for check in skipped:
        # Every skip must say why, in a sentence, not by being absent from a list.
        if not structure.skip_reason(check, buildmod.LANG_JS).strip():
            problems.append(f"structural check {check!r} is skipped with no reason")

    # The provenance module skips all six, and every gate it runs has a reason.
    for check in PROVENANCE_GATES:
        if not JS_PROV_SKIP.get(check, "").strip():
            problems.append(
                f"provenance gate {check!r} has no reason in JS_PROV_SKIP, so a "
                f"pre-migration tree would be asked it and two of these six pass "
                f"on an absent artifact with a false sentence"
            )
    return problems


def _lang_routing_problems() -> list[str]:
    """Which tree gets JsBuilder, asserted on constructed trees rather than read.

    `_check_applicability` proves the skip tables are right *given* a language.
    This proves the language is right, which is the question underneath it: the
    JavaScript path substitutes a builder the task author wrote for a `make build`
    State A does not have, so a submission routed onto it is measured on that
    builder's artifacts rather than its own -- and passes, at full marks, having
    never run the build the contract publishes.

    Which is why the routing is keyed on the file the builder needs.  A rule
    reading `(repo / "Cargo.toml").is_file()` sends a submission with sixteen
    `.rs` files under `acorn/src/` and no workspace manifest down the JavaScript
    path, to be graded `javascript, built by JsBuilder` at full marks.  Nothing in
    a passing run would show it: `structure` skips seven cases with State A's
    measured reasons, exactly as it does on the tree those reasons came from.

    The cases are constructed rather than pointed at, because the pre-migration
    tree is what the grading image has and the interesting trees are the ones it
    does not.
    """
    import tempfile

    def tree(*paths: str) -> Path:
        root = Path(tempfile.mkdtemp(prefix="srb-lang-"))
        for rel in paths:
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x\n", encoding="utf-8")
        return root

    # acorn's own layout, as the frozen tarball ships it.
    STATE_A = ("package.json", "acorn/src/index.js", "acorn/src/expression.js",
               "acorn-walk/src/index.js", "test/run.js")

    cases: tuple[tuple[str, tuple[str, ...], str], ...] = (
        # The one tree the JavaScript path is for.
        ("State A as the tarball ships it", STATE_A, buildmod.LANG_JS),
        # A conforming port: the manifest is at the root where the contract says.
        ("a workspace at the root", ("Cargo.toml", "acorn/src/lib.rs"),
         buildmod.LANG_RUST),
        # The one that was mis-routed.  A per-crate manifest and no workspace root
        # is a non-conforming port, and "does not build" is the honest verdict;
        # "behaviour preserved" is not.
        ("a per-crate manifest with no workspace root",
         STATE_A + ("acorn/Cargo.toml", "acorn/src/lib.rs"), buildmod.LANG_RUST),
        # Mid-port, which is what a run that spends its budget on the parser
        # actually hands in.  Sixteen of these took full marks as State A.
        ("sources written, nothing declared yet",
         STATE_A + ("acorn/src/tokenize.rs", "acorn/src/parse.rs"),
         buildmod.LANG_RUST),
        ("one .rs file and nothing else", ("acorn/src/lib.rs",),
         buildmod.LANG_RUST),
        # A Makefile is the contract's own entry point: a tree with one is
        # claiming to answer `make build`, so it is asked to.
        ("a Makefile and no sources yet", STATE_A + ("Makefile",),
         buildmod.LANG_RUST),
        ("a lockfile alone", STATE_A + ("Cargo.lock",), buildmod.LANG_RUST),
        # Neither tree.  Rust-that-does-not-build is the correct verdict for an
        # empty directory; JavaScript would hand it State A's builder.
        ("an empty tree", (), buildmod.LANG_RUST),
        ("acorn's layout gutted", ("package.json",), buildmod.LANG_RUST),
        # Not evidence: a build product this verifier discards, and State A's own
        # dependency tree.  Either one counting would route State A to Rust and
        # take the 40.00/40 control with it.
        ("a stale target/ from the agent's own cargo run",
         STATE_A + ("target/release/build/x/out/generated.rs",),
         buildmod.LANG_JS),
        ("a .rs file inside node_modules",
         STATE_A + ("node_modules/some-pkg/vendor/lib.rs",), buildmod.LANG_JS),
    )

    problems: list[str] = []
    for label, paths, expected in cases:
        root = tree(*paths)
        try:
            got = buildmod.detect_language(root)
            if got != expected:
                builder = ("JsBuilder" if got == buildmod.LANG_JS else "Builder")
                problems.append(
                    f"{label}: detect_language says {got!r}, not {expected!r} -- "
                    f"this tree would be built by {builder}"
                )
        finally:
            shutil.rmtree(root, ignore_errors=True)

    # The JavaScript path exists for exactly one tree, and the skip tables are
    # written from measurements taken on it.  If a second kind of tree can reach
    # it, those measurements are being applied to something they were not taken
    # from, whatever the counts say.
    reachable = [label for label, paths, expected in cases
                 if expected == buildmod.LANG_JS]
    if len(reachable) != 3:
        problems.append(
            f"{len(reachable)} of the trees above are routed to JavaScript, not 3 "
            f"(State A, and State A with each of the two directories that are not "
            f"evidence): {', '.join(reachable)}"
        )
    return problems


def _check_families(families: dict[str, int]) -> None:
    """Every frozen corpus family is owned by exactly one behavioural module."""
    owned: dict[str, list[str]] = {}
    for module, group in MODULES.items():
        for family in group:
            owned.setdefault(family, []).append(module)
    problems = []
    for family in sorted(set(families) - set(owned)):
        problems.append(
            f"family {family!r} ({families[family]} cases) is owned by no module"
        )
    for family in sorted(set(owned) - set(families)):
        problems.append(f"module owns family {family!r}, which the corpus lacks")
    for family, owners in sorted(owned.items()):
        if len(owners) > 1:
            problems.append(f"family {family!r} is owned by {', '.join(owners)}")
    if problems:
        raise SystemExit(
            "the frozen corpus and this driver's module map disagree:\n  "
            + "\n  ".join(problems)
        )


class Emitter:
    """Accumulates one module's checks and writes the stage-result JSON."""

    def __init__(self, unit: str, result: Path, log: Log) -> None:
        self.unit = unit
        self.result = result
        self.log = log
        self.started = time.time()
        self.checks: list[dict] = []
        self.notes: list[str] = []
        self.metadata: dict = {}

    def add(self, check_id: str, verdict: str, summary: str, *,
            weight: float = 1.0, detail: str = "", **extra) -> None:
        entry = {
            "id": check_id,
            "verdict": verdict,
            "summary": summary[:400],
            "weight": round(float(weight), 6),
        }
        if detail:
            entry["detail"] = detail[:4000]
        if extra:
            entry["metadata"] = extra
        self.checks.append(entry)

    def note(self, text: str) -> None:
        self.notes.append(text)
        self.log.write(f"note: {text}")

    def counts(self) -> tuple[int, int, float, float, int]:
        """passed, scored, earned weight, scored weight, skipped.

        A skipped check is inside both totals and earns nothing, which is what
        `pooled` charges it, so this module's own account of itself agrees with the
        score it is given.

        `skipped` is still returned and still reported, because it remains worth
        knowing: a module that answered badly and one that could not answer at all
        both rate low, and this is what separates them.  What it does not do is
        change the denominator.

        Only one thing reaches this as a skip.  A structural case about a Rust
        delivery, asked of a tree that was never migrated -- charged, because the
        migration is the task.  The corpus cases whose frozen answer has no
        portable form are not emitted as checks at all, so they are not in
        `self.checks` to be counted.  See `run_differential`.
        """
        passed = sum(1 for c in self.checks if c["verdict"] == "pass")
        total_w = sum(c["weight"] for c in self.checks)
        earned = sum(c["weight"] for c in self.checks if c["verdict"] == "pass")
        skipped = sum(1 for c in self.checks if c["verdict"] == "skip")
        return passed, len(self.checks), earned, total_w, skipped

    def write(self) -> None:
        passed, total, earned, total_w, skipped = self.counts()
        payload = {
            "schema": "swerefactor.stage-result/1",
            "stage": "behavioural",
            "unit": self.unit,
            "duration_sec": round(time.time() - self.started, 3),
            "checks": self.checks,
            "notes": self.notes,
            "metadata": {
                "task": TASK,
                "checks_passed": passed,
                # Every check emitted, skips included: `checks_total -
                # checks_passed` is read as the number of things wrong, and a skip
                # is one of those now -- the module was asked and had nothing to
                # answer with.  `checks_skipped` says how many of them are that
                # kind, which is the part a reader still needs.
                "checks_total": total,
                "checks_skipped": skipped,
                "weight_earned": round(earned, 4),
                "weight_total": round(total_w, 4),
                **self.metadata,
            },
        }
        self.result.parent.mkdir(parents=True, exist_ok=True)
        self.result.write_text(
            json.dumps(payload, indent=2, sort_keys=False) + "\n",
            encoding="utf-8",
        )
        self.log.write(
            f"{self.unit}: {passed}/{total} checks, "
            f"{earned:.2f}/{total_w:.2f} weight"
            + (f", {skipped} of them not answerable by this tree and charged 0"
               if skipped else "")
            + f" -> {self.result}"
        )


class Assets:
    """The frozen inputs, with their digests proved before anything reads them.

    A corpus that disagrees with the expectations it was frozen beside would
    grade every case as a mismatch, and nothing in the report would say the fault
    was the image's.  So a digest mismatch raises instead of scoring: an absent
    number is recoverable, a wrong one is not.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.manifest = vlib.read_json(root / "verifier-manifest.json")
        if self.manifest.get("schema") != MANIFEST_SCHEMA:
            raise SystemExit(
                f"asset manifest is schema {self.manifest.get('schema')!r}, "
                f"this driver reads {MANIFEST_SCHEMA!r}"
            )
        self.contract = vlib.read_json(root / FROZEN_INPUTS["contract"])
        self.baseline = root / "baseline"
        self.reference = root / "reference"
        self.corpus = root / "corpus"
        self.requests = root / FROZEN_INPUTS["requests"]
        self.expected = root / FROZEN_INPUTS["expected"]
        self.fixture_dir = self.reference / "test" / "bench" / "fixtures"

        recorded = self.manifest.get("digests") or {}
        inputs = {key: root / rel for key, rel in FROZEN_INPUTS.items()}
        for key, path in sorted(inputs.items()):
            if key not in recorded:
                raise SystemExit(f"the manifest records no digest for {key!r}")
            if not path.is_file():
                raise SystemExit(f"frozen input missing: {path}")
            got = vlib.sha256_file(path)
            if got != recorded[key]:
                raise SystemExit(
                    f"frozen input {key} has digest {got[:16]}, the image "
                    f"recorded {recorded[key][:16]}. The grading inputs are not "
                    f"the frozen ones; refusing to score."
                )

        payload = json.loads(inputs["cases"].read_text(encoding="utf-8"))
        self.cases = {int(c["id"]): c for c in payload["cases"]}
        self.families: dict[str, int] = dict(payload["families"])
        self.count = int(payload.get("count") or len(self.cases))
        self.seed = payload.get("seed")
        self.fixtures = json.loads(
            inputs["fixtures"].read_text(encoding="utf-8")
        )["fixtures"]
        self.fixture_digests = json.loads(
            inputs["fixture_digests"].read_text(encoding="utf-8")
        )["digests"]
        if len(self.cases) != self.count:
            raise SystemExit(
                f"cases.json declares {self.count} cases and lists "
                f"{len(self.cases)}"
            )

        # The cases whose frozen answer is a crash inside the reference, whose
        # message is V8's wording for the shape of the code that crashed rather
        # than anything acorn defines.  freeze.check_unportable derives this from
        # the expectations and proves the premise against the reference's own
        # sources; grading reads the result and does not re-derive it, because the
        # sources the rule is about are not in the submission's tree.
        excluded = json.loads(inputs["excluded"].read_text(encoding="utf-8"))
        if excluded.get("schema") != EXCLUDED_SCHEMA:
            raise SystemExit(
                f"excluded.json is schema {excluded.get('schema')!r}, this driver "
                f"reads {EXCLUDED_SCHEMA!r}"
            )
        self.excluded_reason = str(excluded["reason"])
        self.excluded: dict[int, str] = {
            int(c["id"]): f"{c['op']} expects {c['kind']}: {c['message']}"
            for c in excluded["cases"]
        }
        if len(self.excluded) != int(excluded["count"]):
            raise SystemExit(
                f"excluded.json declares {excluded['count']} case(s) and lists "
                f"{len(self.excluded)}; two entries share an id"
            )
        stray = sorted(set(self.excluded) - set(self.cases))
        if stray:
            raise SystemExit(
                f"excluded.json names {len(stray)} case(s) the corpus does not "
                f"hold: {stray[:6]}. The exclusion list and the corpus were "
                f"frozen from different runs."
            )

        # catalog.py lives in this image, not in the frozen assets, so a catalog
        # edited after the corpus was frozen would otherwise grade a family at a
        # fallback weight and say nothing.
        problems = catalog.check_catalog(self.families, len(self.fixtures))
        missing = [e["key"] for e in self.fixtures
                   if e["key"] not in self.fixture_digests]
        if missing:
            problems.append(
                f"{len(missing)} fixture case(s) have no frozen digest: "
                f"{missing[:4]}"
            )
        if problems:
            raise SystemExit(
                "the frozen corpus and this image's catalog disagree:\n  "
                + "\n  ".join(problems)
            )
        _check_families(self.families)
        self.weights = catalog.family_weights(self.families)
        self.fixture_weight = (
            catalog.FIXTURE_BUDGET / len(self.fixtures) if self.fixtures else 0.0
        )

    def node(self) -> Path:
        """The image's own `node`, resolved once to an absolute path.

        Every graded subprocess runs under a PATH whose `node` is a tripwire, so
        the reference cannot be reached by name.  Absent, the CLI differential
        has nothing to compare against, which is an image defect.
        """
        found = buildmod.find_node()
        if found is None:
            raise SystemExit(
                "no `node` on the verifier's own PATH; the CLI differential has "
                "nothing to compare against"
            )
        return found


class ProbeVoice:
    """One probe session held open for the questions a module asks directly.

    A dead or unstartable probe answers None, which every asking check is written
    to handle.  It must not raise: the probe failing to run is a graded outcome,
    not a verifier fault.
    """

    def __init__(self, binary: Path, *, cwd: Path, env: dict, log: Log) -> None:
        self.binary = binary
        self.cwd = cwd
        self.env = env
        self.log = log
        self.session: probemod.Session | None = None
        self.dead = False

    def ask(self, request: dict) -> dict | None:
        if self.dead:
            return None
        if not self.binary.is_file():
            self.dead = True
            self.log.write(f"probe-voice: no probe at {self.binary}")
            return None
        try:
            if self.session is None:
                self.session = probemod.Session(
                    argv=[str(self.binary)], cwd=self.cwd, env=self.env
                )
                self.session.start()
            line = self.session.ask(json.dumps(request).encode("utf-8"))
            return json.loads(line.decode("utf-8"))
        except Exception as exc:
            self.dead = True
            self.log.write(f"probe-voice: {type(exc).__name__}: {exc}")
            return None

    def close(self) -> None:
        if self.session is not None:
            self.session.kill()
            self.session = None


class Driver:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.module = args.module
        self.work = args.work
        self.work.mkdir(parents=True, exist_ok=True)
        self.logs = args.log
        self.logs.mkdir(parents=True, exist_ok=True)
        self.log = Log(self.logs / f"behavioural-{self.module}.log")
        self.emit = Emitter(self.module, args.result, self.log)
        self.assets = Assets(args.assets)
        self.submitted = args.repo
        # Everything below lives in the shared work directory, so `build` can
        # produce it and the other twelve modules can read it.
        self.repo = self.work / "repo"
        self.scratch = self.work / "scratch"
        self.scratch.mkdir(parents=True, exist_ok=True)
        self.shim_dir = self.work / "shim"
        self.state = self.work / BUILD_STATE
        self.gate_ledger = self.work / "shim-gate.jsonl"
        self.behavioural_ledger = self.work / f"shim-run-{self.module}.jsonl"
        self.enforce = os.environ.get("SRB_SHIM", "enforce") != "off"
        self.identity = args.identity
        self.builder: buildmod.Builder | None = None
        self.outcome: buildmod.BuildOutcome | None = None

    # -- shared plumbing ---------------------------------------------------

    def env(self) -> dict:
        """The environment every graded run of a submission binary sees."""
        return buildmod.shim_env(
            self.shim_dir, self.behavioural_ledger, home=self.builder.home
        )

    def make_builder(self, *, language: str = "") -> buildmod.Builder:
        """The builder for the tree in the work directory.

        `make_builder` rather than `Builder`, because which builder can run a tree
        depends on what the tree is: a Rust port gets the contract's `make` argv,
        and the pre-migration JavaScript tree -- the one every expectation in this
        suite was frozen from -- gets the npm-shaped analogue.  `language` is
        passed by `read_build` so a later module uses the builder the recorded
        build was actually produced by, not a fresh opinion about the tree.
        """
        self.builder = buildmod.make_builder(
            self.repo, self.work, self.shim_dir, self.log,
            contract=self.assets.contract, assets=self.assets.root,
            language=language,
        )
        return self.builder

    def read_build(self) -> buildmod.BuildOutcome:
        """Rehydrate what `build` left behind, or explain why there is nothing.

        A module that runs before `build` is a suite.toml ordering bug, and it
        would otherwise present as every case failing -- which reads as a broken
        submission.  So it raises with the ordering named.
        """
        if self.identity:
            # There is no submission in an identity run and so no build to read.
            # The outcome is returned empty rather than faked: `probe_argv` and
            # `run_cli` both branch on `self.identity` before they would reach the
            # prefix, and any module that used this outcome for anything else would
            # get an obviously empty one instead of a plausible wrong one.
            self.outcome = buildmod.BuildOutcome(
                build_dir=self.work / "identity-build",
                prefix=self.work / "identity-prefix",
            )
            return self.outcome
        if not self.state.is_file():
            raise SystemExit(
                f"no {BUILD_STATE} in {self.work}: module {self.module!r} ran "
                f"before `build`. suite.toml must declare `build` first, and the "
                f"runner must share $SRB_SUITE_WORK across modules."
            )
        payload = vlib.read_json(self.state)
        outcome = buildmod.BuildOutcome.from_summary(payload, self.work)
        # The recorded language, not a fresh detection: this builder is a reader of
        # a build that has already happened, and the two must not be able to
        # disagree about what was built.
        self.make_builder(language=outcome.language)
        self.outcome = outcome
        return outcome

    def probe_binary(self) -> Path:
        return self.outcome.prefix / "bin" / "acorn-probe"

    def probe_argv(self) -> list[str] | None:
        """What the differential runs.  Normally the submission's probe.

        `--identity` substitutes the JavaScript driver the expectations were
        frozen from, which must score exactly 1.0.  Anything less is a broken
        image rather than a broken submission, which is why the build-time
        self-check runs it.
        """
        if self.identity:
            return [str(self.assets.node()), str(self.assets.root / "reference.js")]
        binary = self.probe_binary()
        return [str(binary)] if binary.is_file() else None

    # -- A. build ----------------------------------------------------------

    def run_build(self) -> None:
        """Copy the submission, discard what it shipped built, and build it.

        First, and everything after this reads the install prefix it publishes.

        A failure here costs this module its own 6.45% of the rate, and the
        thirteen downstream report their own zeros.  Read them that way -- against
        a tree that did not build, they are one finding repeated, not thirteen.
        The stage pays nothing either way; what this module adds is the reason the
        thirteen zeros are there, so the report does not name every behaviour as
        changed when the finding is that there is nothing to run.
        """
        self.log.section("build submission")
        if not self.submitted.is_dir():
            raise SystemExit(f"no submission at {self.submitted}")
        if self.repo.exists():
            shutil.rmtree(self.repo)
        copied = vlib.copy_tree(self.submitted, self.repo)
        self.log.write(f"copied {copied} files from {self.submitted}")

        if self.enforce:
            buildmod.install_shim(
                self.assets.root / "shim" / "jsshim.py", self.shim_dir, self.log
            )
        else:
            # The build-time self-check only.  With the tripwire off a submission
            # is free to shell out to node, so every interpreter gate is void --
            # which is why this says so in the result rather than only in a log.
            self.shim_dir.mkdir(parents=True, exist_ok=True)
            self.emit.note(
                "SRB_SHIM=off: the interpreter tripwire was disabled, so a "
                "submission was free to spawn node. Every interpreter-related "
                "check in this stage is void and this is not a grading result."
            )

        builder = self.make_builder()
        outcome = builder.run_all()
        if outcome.installed:
            # Order matters: rebuild and reinstall need the artifacts, clean
            # removes them, and install-from-clean is only a real question once
            # they are gone.
            builder.probe_rebuild()
            builder.probe_reinstall(self.scratch)
            builder.probe_offline_fetch()
            builder.probe_clean()
            builder.probe_install_from_clean(self.scratch)
        else:
            self.log.write("not installed; the build probes have nothing to ask")
        self.outcome = outcome
        vlib.write_json(self.state, outcome.summary())

        # Four checks, each a separate claim a downstream consumer would make.
        self.emit.add(
            "build/compiles", "pass" if outcome.built else "fail",
            "the published build argv succeeds on the submitted tree"
            if outcome.built else "the submission does not build",
            weight=4.0,
            detail=outcome.build.tail() if outcome.build else "no build was run",
        )
        self.emit.add(
            "build/installs", "pass" if outcome.installed else "fail",
            "the published install argv succeeds"
            if outcome.installed else "the submission does not install",
            weight=3.0,
            detail=outcome.install.tail() if outcome.install else "not installed",
        )
        for name in buildmod.RELEASE_BINARIES:
            path = outcome.prefix / "bin" / name
            ok = path.is_file() and os.access(path, os.X_OK)
            self.emit.add(
                f"build/installed-{name}", "pass" if ok else "fail",
                f"bin/{name} is installed and executable" if ok else
                f"bin/{name} is missing from the install prefix",
                weight=1.5,
                detail=f"looked for {path}",
            )
        self.emit.metadata.update({
            "prefix": str(outcome.prefix),
            "built": outcome.built,
            "installed": outcome.installed,
            "discarded": dict(sorted(outcome.discarded.items())),
            "shim_enforced": self.enforce,
            # Which builder answered, in a field.  Every applicability decision
            # downstream -- the seven structural skips, all six provenance gates --
            # turns on this one value, so a report showing skips has to say what
            # they were keyed on rather than leaving a reader to infer it from
            # which cases are missing.  It is also the only thing separating "a
            # pre-migration tree scored full marks, as the operator's self-test
            # intends" from "a submission scored full marks without being Rust".
            "language": outcome.language,
        })

    # -- B. the installed deliverable --------------------------------------

    def run_structure(self) -> None:
        """The structural cases, over the install prefix and cargo metadata.

        Nothing here opens a .rs file, and State A's tree is not passed in at all.
        What it asks is what a distribution asks: does `make install` put the
        documented set of files in a prefix, does `make clean` remove what it made,
        does the workspace publish the crates it says it does at the versions it
        says, do the binaries run once the build tree is gone.
        """
        self.log.section("structural cases")
        outcome = self.read_build()
        voice = ProbeVoice(
            self.probe_binary(), cwd=self.scratch,
            env=self.builder.env(), log=self.log,
        )
        try:
            cases = structure.run_structure_phase(
                self.repo, outcome, self.assets.contract,
                self.scratch / "struct", self.log, probe_answer=voice.ask,
            )
        finally:
            voice.close()
        for case in cases:
            if case.not_applicable:
                self.emit.add(
                    case.case_id, "skip",
                    f"not asked: {case.not_applicable}"[:200],
                    weight=case.weight, detail=case.not_applicable,
                    family=case.family, kind=case.kind,
                )
                continue
            self.emit.add(
                case.case_id, "pass" if case.passed else "fail",
                case.detail[:200] or case.case_id,
                weight=case.weight, detail=case.detail,
                family=case.family, kind=case.kind,
            )
        self.emit.metadata["folded_guards"] = list(FOLDED_INTO_STRUCTURE)
        skipped = [c.case_id for c in cases if c.not_applicable]
        if skipped:
            # In the report as well as the log, because the module's rate is over
            # the cases it asked and a reader comparing 34.0 against the catalog's
            # 55.0 would otherwise have no way to see why.
            self.emit.note(
                f"{len(skipped)} of {len(cases)} structural case(s) are not "
                f"applicable to a {self.outcome.language} tree and were not asked: "
                f"{', '.join(c.split('/')[-1] for c in skipped)}. See "
                f"structure.JS_STRUCT_SKIP_GROUPS for the reason per case."
            )

    # -- C. artifact-backed guards -----------------------------------------

    def spawn_check(self) -> tuple[list[dict], str, int]:
        """Run both binaries with nothing but tripwires on PATH.

        A static scan for an interpreter's name is evaded by assembling it at
        run time, so this asks empirically instead.  Both binaries are exercised:
        a submission that shelled out only from the CLI would otherwise be asked
        about a code path it does not use.

        Returns the ledger, a description, and how many of the two binaries were
        actually exercised.  That third value is the point: an empty ledger means
        "asked, and nothing was spawned" only if something was asked, and a tree
        that installed no binaries produces the same empty ledger as a clean one.
        """
        if self.outcome is None:
            return [], "no build to run", 0
        if self.gate_ledger.exists():
            self.gate_ledger.unlink()
        env = buildmod.shim_env(
            self.shim_dir, self.gate_ledger, home=self.builder.home,
            PATH=str(self.shim_dir),
        )
        notes: list[str] = []
        exercised = 0
        # Enough surface that a wrapper would have to be invoked: a class body, a
        # tagged template, a named capture group, an async arrow, for-await.
        source = ("class A extends B { #x = 1; static { g`${/(?<y>a)/u}` } }\n"
                  "const f = async (x = 0) => { for await (const y of x) yield* y }\n")
        probe_bin = self.probe_binary()
        if probe_bin.is_file():
            voice = ProbeVoice(probe_bin, cwd=self.scratch, env=env, log=self.log)
            answered = voice.ask({
                "id": 1, "op": "parse", "source": source,
                "options": {"ecmaVersion": 2025, "locations": True},
            })
            voice.close()
            exercised += 1
            notes.append("the probe answered a parse request"
                         if answered is not None else
                         "the probe did not answer, so it spawned nothing either")
        else:
            notes.append("no acorn-probe was installed")
        acorn_bin = self.outcome.prefix / "bin" / "acorn"
        if acorn_bin.is_file():
            case_dir = self.scratch / "spawn-check"
            case_dir.mkdir(parents=True, exist_ok=True)
            (case_dir / "spawn.js").write_text(source, encoding="utf-8")
            result = vlib.run([str(acorn_bin), "spawn.js"], cwd=case_dir, env=env,
                              timeout=probemod.PER_REQUEST_TIMEOUT, log=self.log,
                              label="spawn-check-cli")
            exercised += 1
            notes.append(f"the CLI parsed a file (rc={result.returncode})")
        else:
            notes.append("no acorn was installed")
        events = buildmod.read_shim_ledger(self.gate_ledger)
        return events, "; ".join(notes) + "; with only tripwires on PATH", exercised

    def run_provenance(self) -> None:
        """The six guards that have a built artifact to point at.

        Nothing calls this: `suite.toml` declares no `provenance` module, and
        `run` has no branch for one.  All seventeen guards are stage 1's, and
        `PROVENANCE_GATES`/`JS_PROV_SKIP` are proved exact at import and again by
        `--self-check`, neither of which enters this body.  What it holds that
        nothing else does is the description of what each of the six opened, and
        that is why it is here to read.

        The dividing line is not "mechanical versus semantic" -- it is whether
        the gate has an artifact.
        """
        self.log.section("provenance gates")
        _check_partition()
        outcome = self.read_build()
        if outcome.language != buildmod.LANG_RUST:
            # A tree that has not been migrated yet: every gate here reads a Rust
            # build's artifact, and two of them pass on the absence with a false
            # sentence.  See JS_PROV_SKIP_GROUPS for what each was measured to do.
            for check in PROVENANCE_GATES:
                gate_id = next(g for g, c, _, _ in catalog.GUARD_CASES if c == check)
                reason = JS_PROV_SKIP[check]
                self.emit.add(gate_id, "skip", f"not asked: {reason}"[:200],
                              weight=1.0, detail=reason, check=check)
                self.log.write(f"  SKIP {gate_id}: {reason[:120]}")
            self.emit.note(
                f"none of the {len(PROVENANCE_GATES)} provenance gates is asked of a "
                f"{outcome.language} tree: each reads an artifact of a Rust build, and "
                f"shim-clean and no-runtime-spawn pass on its absence with a sentence "
                f"that is false. Reasons and their stage-1 owners: "
                f"driver.JS_PROV_SKIP_GROUPS."
            )
            self.emit.metadata["deferred_to_stage_1"] = list(SEMANTIC_GATES)
            self.emit.metadata["folded_into_structure"] = list(FOLDED_INTO_STRUCTURE)
            return
        auditor = audit.IntegrityAuditor(
            self.repo, outcome, self.assets.baseline, self.assets.contract,
            self.scratch / "provenance", self.log,
            probe_env_probe=self.spawn_check,
        )
        by_check = {check: (gate_id, note)
                    for gate_id, check, _, note in catalog.GUARD_CASES}
        cases = []
        for check in PROVENANCE_GATES:
            gate_id, note = by_check[check]
            cases.append({"id": gate_id, "check": check,
                          "params": {"mandatory": True}, "note": note})
        gates = auditor.evaluate(cases)
        for gate in gates:
            self.emit.add(
                gate.gate_id, "pass" if gate.passed else "fail",
                gate.detail[:200] or gate.check,
                weight=1.0, detail=gate.detail,
                evidence=gate.evidence[:12], check=gate.check,
            )
        self.emit.metadata["deferred_to_stage_1"] = list(SEMANTIC_GATES)
        self.emit.metadata["folded_into_structure"] = list(FOLDED_INTO_STRUCTURE)

    # -- D-L. the behavioural slices ---------------------------------------

    def run_slice(self, families: tuple[str, ...]) -> None:
        """One module's families, over the frozen request/expectation corpus.

        The probe answers a line of NDJSON per request and the bytes are compared
        against what the JavaScript produced at freeze time.  Nothing about the
        submission's structure enters into it: the comparison is the answer, and
        the only thing the module knows about the port is which binary to run.
        """
        self.log.section(f"differential: {', '.join(families)}")
        self.read_build()
        wanted = set(families)
        cases = {cid: meta for cid, meta in self.assets.cases.items()
                 if meta.get("family") in wanted}
        if not cases:
            raise SystemExit(
                f"module {self.module!r} owns {sorted(wanted)} and the frozen "
                f"corpus holds no case in any of them"
            )
        argv = self.probe_argv()
        if argv is None:
            # Enumerated as failures rather than skipped: the weight total has to
            # be the same whether or not a probe existed, or a submission that
            # installed nothing would divide a small earned total by a small
            # possible total and look competent.
            reason = "not run: the submission installed no bin/acorn-probe"
            for cid, meta in sorted(cases.items()):
                self.emit.add(
                    f"corpus:{cid}", "fail", reason,
                    weight=self.assets.weights.get(meta.get("family", ""), 1.0),
                    detail=reason, family=meta.get("family"),
                )
            self.emit.note(
                f"{len(cases)} case(s) in this module were not measurable: no "
                f"bin/acorn-probe was installed. Each is recorded as a failure "
                f"because nothing was measured, not as a skip."
            )
            return

        env = (vlib.base_env(PATH=buildmod.REAL_PATH) if self.identity
               else self.env())
        runner = probemod.Runner(argv, log=self.log, cwd=self.scratch, env=env)
        # An identity run is asked everything, including the cases no port can
        # answer.  Those 31 expectations are the reference crashing, and the
        # reference is the code that crashed -- so it is the one probe that both
        # can answer them and must: skipping them here would leave the only
        # expectations in the corpus that nothing ever verifies, and a corrupted
        # one among them would first be noticed by a submission being told it got
        # a crash wrong.  The exclusion is about what a *port* can be asked, and
        # an identity run is not a port.
        outcomes = runner.run_corpus(
            self.assets.requests, self.assets.expected, cases,
            label=self.module, weights=self.assets.weights,
            corpus_lines=self.assets.count,
            excluded={} if self.identity else self.assets.excluded,
        )
        for case in outcomes:
            if case.not_applicable:
                # No check at all, and these are the one kind that must not be
                # charged: the frozen answer is a crash inside the reference whose
                # message is V8's wording for the shape of the code that crashed,
                # so no port can be asked for it and none ever answered one --
                # measured, 31 ids across `generated`, `walk` and
                # `parser-options`, skipped by every cell that ran them.  Charging
                # would put an id no submission can ever pass in every
                # submission's denominator.
                #
                # Deliberately not the same treatment the structural cases get: a
                # Rust-delivery case asked of an unmigrated tree is charged,
                # because there the question is answerable and the tree is the
                # reason it was not answered.  The dividing line is whether the
                # *reference* has an answer, which is what
                # `freeze.check_unportable` proves.
                #
                # `probe.py` still produces the outcome, and it must: the
                # invariants that every case in the slice leaves exactly one
                # outcome, and that the exclusion reached the ids it names, are
                # what would catch an exclusion list that stopped joining.  The
                # record survives here too -- the note below and
                # `unportable_cases` in the metadata -- so what is not emitted is
                # the graded check, not the evidence.
                continue
            self.emit.add(
                case.case_id, "pass" if case.passed else "fail",
                (case.detail or case.case_id)[:200],
                weight=case.weight, detail=case.detail, family=case.family,
            )
        unasked = [c for c in outcomes if c.not_applicable]
        if unasked:
            # In the report as well as the log, for the same reason the structural
            # skips are: a reader comparing this module's case count against the
            # catalog needs to see why it asked fewer.
            per_family: dict[str, int] = {}
            for case in unasked:
                per_family[case.family] = per_family.get(case.family, 0) + 1
            self.emit.note(
                f"{len(unasked)} of this module's {len(outcomes)} frozen case(s) "
                f"have no portable answer; they are not graded and not counted, so "
                f"the rate above is over the remaining "
                f"{len(outcomes) - len(unasked)} ("
                + ", ".join(f"{f} {n}" for f, n in sorted(per_family.items()))
                + f"): {self.assets.excluded_reason}"
            )
            self.emit.metadata["unportable_cases"] = sorted(
                int(c.case_id.rsplit(":", 1)[1]) for c in unasked)
        if runner.crashes:
            self.emit.note(
                f"the probe died and was restarted {len(runner.crashes)} time(s) "
                f"during this module"
            )
            self.emit.metadata["crashes"] = runner.crashes[:12]
        self.recheck_runtime_spawn()

    # -- M. whole files ----------------------------------------------------

    def run_whole_libraries(self) -> None:
        """acorn's own benchmark inputs: real libraries, parsed whole.

        A corpus case is a construct; a fixture is a file somebody shipped. They
        fail differently -- a port can be right about every construct in
        isolation and wrong about a 300KB file, because the failure is in
        accumulated state rather than in a rule.
        """
        self.log.section("fixtures")
        self.read_build()
        argv = self.probe_argv()
        if argv is None:
            reason = "not run: the submission installed no bin/acorn-probe"
            for entry in self.assets.fixtures:
                self.emit.add(
                    f"fixtures:{entry['key']}", "fail", reason,
                    weight=self.assets.fixture_weight, detail=reason,
                    family="fixtures",
                )
            self.emit.note(
                f"{len(self.assets.fixtures)} fixture case(s) were not "
                f"measurable: no bin/acorn-probe was installed."
            )
            return
        env = (vlib.base_env(PATH=buildmod.REAL_PATH) if self.identity
               else self.env())
        runner = probemod.Runner(argv, log=self.log, cwd=self.scratch, env=env)
        outcomes = runner.run_fixtures(
            self.assets.fixtures, self.assets.fixture_digests,
            self.assets.fixture_dir, weight=self.assets.fixture_weight,
        )
        for case in outcomes:
            self.emit.add(
                case.case_id, "pass" if case.passed else "fail",
                (case.detail or case.case_id)[:200],
                weight=case.weight, detail=case.detail, family=case.family,
            )
        self.recheck_runtime_spawn()

    # -- N. the installed executable ---------------------------------------

    def run_cli(self) -> None:
        """The CLI differential, both sides in this container, argv for argv.

        This is the only module that runs the JavaScript live rather than
        comparing against frozen bytes, because the surface is a process: exit
        status, stdout and stderr together, for the same argv, on the same files.
        """
        self.log.section("cli differential")
        outcome = self.read_build()
        if self.identity:
            prefix = self.scratch / "identity-prefix"
            (prefix / "bin").mkdir(parents=True, exist_ok=True)
            wrapper = prefix / "bin" / "acorn"
            wrapper.write_text(
                f'#!/bin/sh\nexec "{self.assets.node()}" '
                f'"{self.assets.reference / "acorn/bin/acorn"}" "$@"\n',
                encoding="utf-8",
            )
            wrapper.chmod(0o755)
            env = vlib.base_env(PATH=buildmod.REAL_PATH)
        else:
            prefix = outcome.prefix
            env = self.env()
        outcomes, problems = climod.run_cli_phase(
            prefix, self.assets.reference, self.scratch / "cli", self.log,
            env=env, node_bin=self.assets.node(),
        )
        if problems:
            # The reference could not run, or one of this image's own expectations
            # turned out to be unreproducible. Both are image defects, and scoring
            # what did run would report a fraction of a module as the module.
            raise SystemExit(
                "the CLI differential could not be run against this image's own "
                "reference, so nothing here is about the submission:\n  "
                + "\n  ".join(problems[:6])
            )
        # The declared budget, not the sum of the hand-written weights: the
        # per-case numbers are ratios between CLI cases, and what the CLI is worth
        # against the corpus is CLI_BUDGET.
        raw = sum(o.weight for o in outcomes)
        if raw > 0:
            scale = catalog.CLI_BUDGET / raw
            for case in outcomes:
                case.weight *= scale
        for case in outcomes:
            self.emit.add(
                case.case_id, "pass" if case.passed else "fail",
                (case.detail or case.case_id)[:200],
                weight=case.weight, detail=case.detail, family=case.family,
            )
        self.recheck_runtime_spawn()

    def recheck_runtime_spawn(self) -> None:
        """Did this module's graded run reach for an interpreter?

        `no-runtime-spawn` is decided in `provenance` on one synthetic request. A
        submission that shells out only for some inputs -- only for modules, only
        past a size threshold -- passes that and then does it thousands of times
        while being graded. The ledger from the graded run is the better evidence
        and it arrives late, so every module that ran the submission reports what
        landed in its own ledger.
        """
        events = buildmod.read_shim_ledger(self.behavioural_ledger)
        if not events:
            return
        tools = sorted({str(e.get("tool")) for e in events})
        self.emit.add(
            f"guard/no-runtime-spawn:{self.module}", "fail",
            f"the graded run attempted {len(events)} interpreter invocation(s) "
            f"({', '.join(tools[:6])}) while cases were being measured",
            weight=0.0,
            detail="; ".join(
                f"{e.get('tool')} "
                f"{' '.join(str(a) for a in (e.get('argv') or [])[:6])}"
                for e in events[:12]
            ),
        )
        self.emit.note(
            "the interpreter tripwire fired during this module's graded run; "
            "stage 1's no-interpreter gate is the one that decides what it means, "
            "and this check carries no weight of its own."
        )

    # -- dispatch ----------------------------------------------------------

    def run(self) -> int:
        if self.module not in MODULES:
            raise SystemExit(
                f"unknown module {self.module!r}; suite.toml and this driver's "
                f"MODULES map must agree. Known: {', '.join(sorted(MODULES))}"
            )
        if self.identity and self.module in IDENTITY_SKIPS:
            # Refused rather than skipped.  Both of these ask a submission to
            # build, install and account for its artifacts, and there is no
            # submission in an identity run -- so running them would either grade
            # the frozen JavaScript as if it were a Rust port, or pass vacuously.
            # The Dockerfile's identity loop knows the same list; if it drifts,
            # this is what fails the build.
            raise SystemExit(
                f"module {self.module!r} needs a submission to build and cannot be "
                f"part of an identity run; identity covers "
                f"{len(MODULES) - len(IDENTITY_SKIPS)} of {len(MODULES)} modules"
            )
        if self.module == "build":
            self.run_build()
        elif self.module == "structure":
            self.run_structure()
        elif self.module == "whole-libraries":
            self.run_whole_libraries()
        elif self.module == "cli":
            self.run_cli()
        else:
            self.run_slice(MODULES[self.module])
        self.emit.write()
        return 0


def self_check(args: argparse.Namespace) -> int:
    """Prove at image build time what cannot be proved at grading time.

    Three things, none of which involves a submission:

      * the module map partitions the frozen corpus exactly, and the guard split
        partitions the catalog exactly -- both raise rather than score;
      * every module id in suite.toml is one this driver implements, and every
        module this driver implements is declared in suite.toml;
      * the frozen digests match the files they were taken from.

    The identity run -- grading the JavaScript reference against its own frozen
    answers -- is a separate, much longer check and is invoked by the Dockerfile
    with `--identity`, not from here.
    """
    log = Log(args.log / "behavioural-self-check.log")
    assets = Assets(args.assets)
    _check_partition()
    problems: list[str] = []
    problems += _check_applicability()
    problems += _lang_routing_problems()
    problems += _check_exclusion(assets)

    for gate in IDENTITY_SKIPS:
        if gate not in MODULES:
            problems.append(
                f"IDENTITY_SKIPS names {gate!r}, which is not a module -- the "
                f"Dockerfile's identity loop skips by this list and would run it"
            )

    suite = Path(args.suite)
    if suite.is_file():
        try:
            try:
                import tomllib
            except ModuleNotFoundError:  # Python 3.10 and older, i.e. an author's host
                import tomli as tomllib  # type: ignore[no-redef]
            declared = [m.get("id") for m in
                        (tomllib.loads(suite.read_text(encoding="utf-8"))
                         .get("module") or [])]
        except Exception as exc:  # pragma: no cover - a broken suite.toml
            declared = []
            problems.append(f"suite.toml is unreadable: {type(exc).__name__}: {exc}")
        for module_id in declared:
            if module_id not in MODULES:
                problems.append(
                    f"suite.toml declares module {module_id!r}, which driver.py "
                    f"does not implement"
                )
        for module_id in MODULES:
            if module_id not in declared:
                problems.append(
                    f"driver.py implements module {module_id!r}, which suite.toml "
                    f"does not declare -- its weight would never be earned"
                )
    else:
        problems.append(f"no suite.toml at {suite}")

    if problems:
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    total_families = sum(len(v) for v in MODULES.values())
    log.write(
        f"self-check ok: {assets.count} corpus cases in {len(assets.families)} "
        f"families over {total_families} module claims, "
        f"{len(assets.excluded)} with no portable answer, "
        f"{len(assets.fixtures)} fixtures, {len(catalog.CLI_CASES)} CLI cases, "
        f"{len(catalog.STRUCT_CASES)} structural, {len(PROVENANCE_GATES)} "
        f"provenance gates, {len(SEMANTIC_GATES)} deferred to stage 1"
    )
    print(
        f"behavioural self-check: {len(MODULES)} modules, "
        f"{assets.count - len(assets.excluded)} of {assets.count} cases asked, "
        f"{len(assets.families)} families, {len(assets.fixtures)} fixtures, "
        f"{len(MODULES) - len(IDENTITY_SKIPS)} coverable by identity"
    )
    return 0


def identity_modules() -> list[str]:
    """The modules an identity run covers, in suite order.

    Printed by `--list-identity` so the Dockerfile loops over this list rather than
    over one written into the shell.  A module added to the driver and not to the
    Dockerfile would otherwise be absent from the identity run and nothing would
    say so.
    """
    return [m for m in MODULES if m not in IDENTITY_SKIPS]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--module", default="")
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--list-identity", action="store_true",
                        help="print the modules an identity run covers, one per "
                             "line, for the build's identity loop")
    parser.add_argument("--identity", action="store_true",
                        help="grade the JavaScript reference against its own "
                             "frozen answers; must score 1.0")
    parser.add_argument("--assets", type=Path, default=DEFAULT_ASSETS)
    parser.add_argument("--repo", type=Path, default=DEFAULT_REPO)
    parser.add_argument("--work", type=Path, default=DEFAULT_WORK)
    parser.add_argument("--result", type=Path, default=None)
    parser.add_argument("--log", type=Path, default=DEFAULT_LOGS)
    parser.add_argument("--suite", default=str(HERE.parent / "suite.toml"))
    args = parser.parse_args(argv)

    if args.list_identity:
        # Before the path resolution below, because this needs no assets and is
        # called by the build before the assets are sealed.
        print("\n".join(identity_modules()))
        return 0
    for name in ("assets", "repo", "work", "log"):
        setattr(args, name, getattr(args, name).resolve())
    if args.self_check:
        return self_check(args)
    if not args.module:
        parser.error("--module is required unless --self-check is given")
    if args.result is None:
        args.result = Path(
            os.environ.get("SRB_RESULT", args.log / f"behavioural-{args.module}.json")
        )
    args.result = args.result.resolve()

    driver = None
    try:
        driver = Driver(args)
        return driver.run()
    except SystemExit:
        raise
    except Exception:
        # A partial result with the traceback in it, rather than no result at all.
        # An absent result file is indistinguishable from a module that never ran;
        # this way the stage reports `error` with the reason attached, and the
        # runner can tell a verifier defect from a failing submission.
        tb = traceback.format_exc()
        payload = {
            "schema": "swerefactor.stage-result/1",
            "stage": "behavioural",
            "unit": args.module,
            "checks": (driver.emit.checks if driver else []),
            "notes": [
                "this module did not finish: the driver raised. What follows is a "
                "defect in the verifier unless the traceback names the submission."
            ],
            "metadata": {"task": TASK, "error": tb[-4000:]},
        }
        args.result.parent.mkdir(parents=True, exist_ok=True)
        args.result.write_text(json.dumps(payload, indent=2) + "\n",
                               encoding="utf-8")
        print(tb, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
