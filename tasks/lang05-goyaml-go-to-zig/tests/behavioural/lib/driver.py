#!/usr/bin/env python3
"""One module of the behavioural suite, run as its own process.

Every directory under `tests/behavioural/modules/` has a `run.sh` that lands here
with `SRB_MODULE_ID` set, and the id decides what gets measured.  There is one
file rather than twenty-two because the modules differ in *which* cases they run,
not in how: they all build nothing, drive the executable the build module
published, and compare its bytes against answers frozen when the image was built.

    build            the resolved driver's build, once, for the whole suite.
                     Publishes the built tree and the shim ledger to
                     $SRB_SUITE_WORK.  Which driver, and so which compiler, is
                     read off the tree -- see `graded_builds` in the contract.
    <frozen modules> one shard of the frozen cases each, sharded by family at
                     image build time.
    unseen-inputs    cases generated here, from a seed the submission has
                     never seen, answered here by the reference binary.
    real-world-files twelve YAML files, six operations each, compared by digest.
    protocol         the transport rules, plus how the process behaves.
    provenance       what the build and the binary say about how they were made.

Nothing in this file opens a `.zig` file.  That is the boundary between this stage
and stage 1: reading the submission's source to judge how the port was written is
stage 1's question, asked there by a model that can tell a comment mentioning Go
from a call into it.  This stage builds the thing and measures what it does.

The module contract, in full, is in `swerefactor/behavioural.py`.  What matters here:
the environment names the paths, `$SRB_RESULT` gets the JSON, and the exit code is
advisory -- a module that measured 4,000 failures ran perfectly.
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

#: Every entry point here runs under `python3 -I`, which implies `-P`: the
#: script's own directory is *not* on sys.path.  That is wanted -- nothing in
#: /workspace/repo should be importable while grading it -- but it also means the
#: sibling modules below have to be put back deliberately.  Without this line the
#: first `import catalog` raises ModuleNotFoundError, every module writes an
#: `error` result, and the stage reports a dead verifier rather than a bad
#: submission.  Resolved so a run through a symlinked run.sh lands in lib/ and not
#: in modules/<id>/.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import build as buildmod  # noqa: E402
import catalog  # noqa: E402
import probe as probemod  # noqa: E402
import provenance as provmod  # noqa: E402
import structure as structuremod  # noqa: E402
import vlib  # noqa: E402
from vlib import CaseOutcome, Log  # noqa: E402

#: Where the frozen assets are mounted.  Overridable so the suite can be run
#: against a rebuilt asset tree while it is being developed.
DEFAULT_ASSETS = Path(os.environ.get("SWEREFACTOR_ASSETS", "/opt/assets"))

TASK = "lang05-goyaml-go-to-zig"
MANIFEST_SCHEMA = "swerefactor-verifier-manifest-v1"
RESULT_SCHEMA = "swerefactor.stage-result/1"

#: How many frozen expectations the build module re-answers with the reference to
#: prove the binary beside them is the one that produced them.
SELF_CHECK_SAMPLE = 300


# -- assets -------------------------------------------------------------------


class Assets:
    """The frozen inputs, verified against the digests the image recorded.

    Each module verifies only what it reads.  That is not a shortcut: hashing the
    40 MB expectation file in all seventeen frozen modules would cost more than the
    grading, and a module that never opens a file has established nothing by
    hashing it.  The keys are exactly the ones `freeze.py:digest_all` writes, so a
    renamed asset is a KeyError here rather than a silently unverified read.
    """

    def __init__(self, root: Path, log: Log) -> None:
        self.root = root
        self.log = log
        self.manifest = vlib.read_json(root / "verifier-manifest.json")
        if self.manifest.get("schema") != MANIFEST_SCHEMA:
            raise SystemExit(
                f"asset manifest is schema {self.manifest.get('schema')!r}, this "
                f"driver reads {MANIFEST_SCHEMA!r}"
            )
        if self.manifest.get("task") != TASK:
            raise SystemExit(
                f"asset manifest is for task {self.manifest.get('task')!r}, not "
                f"{TASK!r}"
            )
        self.digests: dict[str, str] = dict(self.manifest.get("digests") or {})
        self.counts: dict = dict(self.manifest.get("counts") or {})
        self.baseline = root / "baseline"
        self.cases = root / "cases"
        self.documents = root / "documents"
        self.reference = root / "bin" / "reference"
        self.generator = root / "bin" / "generator"
        self.harvest = root / "upstream-cases.ndjson"
        self.shim = root / "shim" / "goshim.py"

        #: Every non-shard frozen input, keyed exactly as `freeze.py:digest_all`
        #: keys it.  This exists so `self_check` can *derive* what to verify rather
        #: than list it.  The image built clean and that module aborted on the first
        #: grading run, taking every module that reads its state file with it.
        #:
        #: A key here that `digest_all` does not write -- or a path here that freeze
        #: wrote under a different name -- is now a failed image build.  The
        #: `verify(key, path)` signature is unchanged, so call sites still name the
        #: file they are about to read; passing one that disagrees with this table
        #: is itself an error.
        self.frozen: dict[str, Path] = {
            "contract": root / "source-contract.json",
            "requests": self.cases / "requests.ndjson",
            "expected": self.cases / "expected.ndjson",
            "cases": self.cases / "cases.json",
            "documents": self.cases / "document-manifest.json",
            "document_digests": self.cases / "document-digests.json",
            "protocol_requests": self.cases / "protocol.ndjson",
            "protocol_expected": self.cases / "protocol-expected.ndjson",
            "protocol_cases": self.cases / "protocol-cases.json",
            "reference": self.reference,
            "generator": self.generator,
            "upstream_cases": self.harvest,
            "shim": self.shim,
        }

        # Last, because `verify` consults the table above.  Every module needs the
        # contract, so reading it here means one verified read instead of seventeen.
        self.contract = self.frozen_json("contract")

    # -- verified reads ----------------------------------------------------

    def verify(self, key: str, path: Path) -> Path:
        """Confirm one asset is the file the image recorded, or refuse to grade.

        A mismatch is an image defect, not a failing submission: the expectations
        and the requests disagreeing would fail every case for a reason no report
        could explain.  So it aborts, and the module is recorded as an error.
        """
        if key not in self.digests:
            raise SystemExit(
                f"the manifest records no digest for {key!r}; this driver and the "
                f"image's freeze.py disagree about what is shipped"
            )
        # A non-shard key must name the path `frozen` records for it.  Every such
        # read now goes through `frozen_file`, which takes the path from that table,
        # so this cannot fire today -- it is here to keep it that way.  A future call
        # site that hand-writes a path beside a table key would otherwise hash the
        # right key against the wrong file, and a digest mismatch reads as a tampered
        # asset tree rather than as a typo.
        expected = self.frozen.get(key)
        if expected is not None and Path(path) != expected:
            raise SystemExit(
                f"verify({key!r}) was given {path}, but the frozen table records "
                f"{expected}; the caller and the digest table disagree about which "
                f"file this key names"
            )
        if not path.is_file():
            raise SystemExit(f"frozen input missing: {path}")
        got = vlib.sha256_file(path)
        if got != self.digests[key]:
            raise SystemExit(
                f"frozen input {key} has digest {got[:16]}, the image recorded "
                f"{self.digests[key][:16]}. The grading inputs are not the frozen "
                f"ones; refusing to score."
            )
        return path

    def verified_json(self, key: str, path: Path) -> dict:
        return vlib.read_json(self.verify(key, path))

    def frozen_file(self, key: str) -> Path:
        """Verify one non-shard input by key alone, and hand back its path.

        The preferred read.  `verify(key, path)` remains for the shard keys, which
        are composed per module and so cannot come from a table -- but for
        everything else, naming the file at the call site meant the same basename
        was written in this file and in freeze.py, and a typo in either was a
        digest mismatch at grading time that reads like a tampered asset tree.
        Here the path can only come from `frozen`, so the disagreement freeze can
        still have with this driver is a missing key, which `self_check` reads
        every one of at image build time.
        """
        if key not in self.frozen:
            raise SystemExit(
                f"{key!r} is not a frozen input; Assets.frozen names "
                f"{sorted(self.frozen)}"
            )
        return self.verify(key, self.frozen[key])

    def frozen_json(self, key: str) -> dict:
        return vlib.read_json(self.frozen_file(key))

    def module_dir(self, module_id: str) -> Path:
        return self.cases / "modules" / module_id

    def shard(self, module_id: str) -> tuple[Path, Path, dict[int, dict]]:
        """One module's own requests, expectations and case index, all verified.

        The digest keys are the shard's filenames, exactly as `freeze.py:digest_all`
        derived them -- `module/<id>/requests.ndjson`, not `module/<id>/requests`.
        Spelled out rather than built from a loop because getting one of them wrong
        is a KeyError at grading time, and the three literals here are the same
        three literals there.
        """
        base = self.module_dir(module_id)
        requests = self.verify(f"module/{module_id}/requests.ndjson",
                               base / "requests.ndjson")
        expected = self.verify(f"module/{module_id}/expected.ndjson",
                               base / "expected.ndjson")
        index = self.verified_json(f"module/{module_id}/cases.json",
                                   base / "cases.json")
        cases = {int(c["id"]): c for c in index["cases"]}
        declared = int(index.get("count") or 0)
        if declared != len(cases):
            raise SystemExit(
                f"the {module_id} shard declares {declared} cases and lists "
                f"{len(cases)}"
            )
        # Against the manifest as well as against itself.  The index and the two
        # NDJSON files were written together, so a disagreement here means one of
        # the three was replaced after the freeze.
        promised = int((self.counts.get("module_cases") or {}).get(module_id, 0))
        if promised and promised != len(cases):
            raise SystemExit(
                f"the manifest promises {promised} cases for {module_id} and the "
                f"shard holds {len(cases)}"
            )
        return requests, expected, cases


# -- the built submission -----------------------------------------------------


class BuiltTree:
    """The submission as the build module left it, plus how to drive it.

    Every module but `build` finds the built tree in $SRB_SUITE_WORK rather than
    building its own.  A from-scratch build costs minutes and produces the same tree
    twenty-two times; worse, twenty-two builds of one submission can disagree,
    and a suite whose modules measured different binaries is a suite whose report
    cannot be read.
    """

    def __init__(self, shared: Path, contract: dict, log: Log) -> None:
        self.shared = shared
        self.contract = contract
        self.log = log
        self.repo = shared / "repo"
        self.scratch = shared / "scratch"
        self.shim_dir = shared / "shim"
        self.build_ledger = shared / "shim-build.jsonl"
        self.state_path = shared / "build-state.json"

    @property
    def available(self) -> bool:
        return self.state_path.is_file()

    def state(self) -> dict:
        if not self.state_path.is_file():
            raise SystemExit(
                "the build module published no state; either it did not run or it "
                "failed before publishing. Every other module depends on it."
            )
        return vlib.read_json(self.state_path)

    def driver(self) -> dict:
        """The build driver this snapshot was built with.

        The id the build module recorded, when there is published state to read it
        from; otherwise resolved from the tree.  Preferring the record matters
        because resolution reads the tree and the build has since written into it:
        re-resolving is a second chance to disagree with the driver that actually
        ran, and every artefact path, command and pinned version hangs off the
        answer.  The fallback is for a module that runs before the state exists or
        after a partial write, which still needs to be able to name an artefact.
        """
        recorded = None
        if self.state_path.is_file():
            try:
                observed = ((vlib.read_json(self.state_path).get("outcome") or {})
                            .get("observed") or {})
                recorded = (observed.get("driver") or {}).get("id")
            except (OSError, ValueError):
                recorded = None
        drivers = ((self.contract.get("graded_builds") or {}).get("drivers")) or []
        if recorded:
            for entry in drivers:
                if entry.get("id") == recorded:
                    return entry
            raise SystemExit(
                f"the build recorded driver {recorded!r}, which "
                f"source-contract.json does not declare: "
                f"{[d.get('id') for d in drivers]}"
            )
        return buildmod.resolve_driver(self.repo, self.contract)

    def artifact(self) -> Path:
        declared = self.driver().get("binaries") or []
        if not declared:
            raise SystemExit(
                f"driver {self.driver().get('id')!r} declares no binary; there is "
                f"nothing for this module to drive"
            )
        return self.repo / declared[0]["path"]

    def graded_env(self, ledger: Path) -> dict:
        """The environment a graded run of the submission's binary sees.

        The Go tripwire is on it, ahead of the real toolchain, with its own
        ledger.  A submission that spawns a Go tool *while being graded* -- only
        for aliases, only past a size threshold -- would otherwise be caught by
        nothing: the provenance module's own probe asks four requests, and this is
        the other several thousand.
        """
        state = self.state()
        return buildmod.shim_env(
            self.shim_dir, ledger,
            home=Path(state["home"]), cache=Path(state["cache"]),
        )

    def reference_env(self) -> dict:
        """The reference's environment.  Real PATH, no tripwire, no ledger.

        The reference is Go, and the tripwire's whole purpose is to refuse Go.
        Running the standard under it would be running the standard under a rule
        written to stop a submission from becoming the standard.
        """
        return vlib.base_env(PATH=buildmod.REAL_PATH)

    def outcome(self) -> buildmod.BuildOutcome:
        """The BuildOutcome the build module recorded, read back without building.

        Two modules need facts only the build could observe: the provenance module
        needs its ledger, and the build module's own cases need the two rebuild
        durations and the paths the build wrote.  None of that exists on disk once
        the build process has exited, which is why it is published rather than
        re-derived.
        """
        return buildmod.BuildOutcome.from_json(self.state()["outcome"])


# -- the envelope -------------------------------------------------------------


class Emitter:
    """Collects cases and writes the one JSON the suite runner reads.

    Written from a `finally` in `main`, so a module that raises still reports what
    it measured before the exception -- 4,000 graded cases and a crash in the
    4,001st is a partial result, and discarding it would turn one defect into a
    zero for the whole module.
    """

    def __init__(self, module_id: str, result_path: Path) -> None:
        self.module_id = module_id
        self.result_path = result_path
        self.started = time.time()
        self.cases: list[CaseOutcome] = []
        self.notes: list[str] = []
        self.metadata: dict = {}
        self.status = "ok"
        self.summary = ""

    def add(self, cases) -> None:
        self.cases.extend(cases)

    def note(self, text: str) -> None:
        self.notes.append(text)

    def fail(self, summary: str) -> None:
        """Mark the module as not having run.  Not the same as failing cases.

        A module that could not measure anything has established nothing about the
        submission, and the scorer treats it as an error rather than as evidence
        of a defect.  The distinction is the whole reason `status` exists.
        """
        self.status = "error"
        self.summary = summary

    def abort(self, summary: str) -> None:
        """The module raised.  Keep whatever it had already measured.

        Distinct from `fail` only in the note: a module that graded 4,000 cases and
        then hit an exception has said something true about those 4,000, and the
        scorer needs to know both that the number is real and that it is partial.
        """
        self.status = "error"
        self.summary = summary
        if self.cases:
            self.note(
                f"the module raised after grading {len(self.cases)} case(s); those "
                f"verdicts stand, the rest were never attempted"
            )

    def payload(self) -> dict:
        checks = []
        for case in self.cases:
            entry = {
                "id": case.case_id,
                "verdict": case.verdict,
                "summary": (case.detail or "")[:1000],
                "weight": round(float(case.weight), 6),
            }
            if case.required:
                entry["required"] = True
            if case.diff:
                entry["detail"] = case.diff[:4000]
            if case.evidence:
                entry["evidence"] = [{"note": e[:600]} for e in case.evidence[:20]]
            if case.duration:
                entry["duration_sec"] = round(case.duration, 4)
            if case.family or case.kind:
                entry["metadata"] = {"family": case.family, "kind": case.kind}
            checks.append(entry)
        # Skips are out of both sides of the ratio, the same rule the scorer
        # applies to the payload this writes.  Counted here as well as reported, so a
        # reader comparing `cases_passed` against `cases` is comparing two numbers
        # with the same denominator instead of seeing a module that "failed" the cases
        # it deliberately did not run.  The scorer's denominator is the wider one --
        # it charges a skip -- and `cases_skipped` below is what reconciles the two.
        scored = [c for c in self.cases if c.scored]
        skipped = [c for c in self.cases if not c.scored]
        passed = sum(1 for c in scored if c.passed)
        earned = sum(c.weight for c in scored if c.passed)
        total = sum(c.weight for c in scored)
        metadata = dict(self.metadata)
        metadata.update({
            "cases": len(scored),
            "cases_passed": passed,
            "weight_earned": round(earned, 4),
            "weight_total": round(total, 4),
        })
        if skipped:
            metadata["cases_skipped"] = len(skipped)
            metadata["skipped_ids"] = sorted(c.case_id for c in skipped)[:20]
            metadata["weight_skipped"] = round(
                sum(c.weight for c in skipped), 4)
        payload = {
            "schema": RESULT_SCHEMA,
            "stage": "behavioural",
            "task": TASK,
            "unit": self.module_id,
            "status": self.status,
            "duration_sec": round(time.time() - self.started, 3),
            "checks": checks,
            "metadata": metadata,
        }
        if self.summary:
            payload["summary"] = self.summary
        if self.notes:
            payload["notes"] = self.notes
        return payload

    def write(self) -> None:
        self.result_path.parent.mkdir(parents=True, exist_ok=True)
        vlib.write_json(self.result_path, self.payload())

    def exit_code(self) -> int:
        if self.status != "ok":
            return 2
        # Over the cases this driver was able to ask.  A skipped case has `passed`
        # False because it was never run, and reading that as a failure would exit 1
        # on a tree the driver deliberately did not ask them of.
        return 0 if all(c.passed for c in self.cases if c.scored) else 1


# -- the build module ---------------------------------------------------------


def module_build(assets: Assets, tree: BuiltTree, submitted: Path,
                 emit: Emitter, log: Log) -> None:
    """Build the submission once, publish the result, then grade the build.

    Order matters and is not obvious.  The tree is published *before* the cases
    are graded, so a build that succeeded but whose cases then hit an exception
    still leaves the other twenty-one modules something to measure -- one broken
    case here must not cost the whole suite its build.
    """
    log.section("snapshot")
    if not submitted.is_dir():
        emit.fail(f"no submission at {submitted}")
        return
    if tree.repo.exists():
        shutil.rmtree(tree.repo)
    copied = vlib.copy_tree(submitted, tree.repo)
    tree.scratch.mkdir(parents=True, exist_ok=True)
    log.write(f"snapshot: {copied} files copied to {tree.repo}")

    # The reference must run here, or the fresh module has no oracle.  Checked in
    # this module because it is the one that runs first, and a broken image should
    # be visible before twenty minutes of grading rather than after.
    reference_ok, reference_note = check_reference(assets, tree, log)
    if not reference_ok:
        emit.note(reference_note)
        log.write(f"WARNING: {reference_note}")

    log.section("build")
    builder = buildmod.Builder(tree.repo, tree.shared, tree.shim_dir, log,
                              contract=assets.contract)
    # The tripwire is for a build that must not reach a Go tool.  The Go driver's
    # build *is* the Go tool, so installing it there would either shadow the
    # compiler or, worse, record the build's own legitimate invocations as
    # attempts -- and `shim-clean` is not scored on that path for exactly that
    # reason.  The directory is still created, so the ledger's absence below is the
    # only thing distinguishing the two.
    if not builder.uses_go:
        buildmod.install_shim(assets.frozen_file("shim"), tree.shim_dir, log)
    else:
        tree.shim_dir.mkdir(parents=True, exist_ok=True)
    outcome = builder.run_all()
    if outcome.built:
        # Not a free ordering.  `probe_rebuild` needs the cache the first build
        # left, `probe_standalone` needs zig-out/ intact, and
        # `probe_cache_outside_tree` deliberately builds with the cache variables
        # unset -- so it goes last, because it is the one probe that changes the
        # environment the others assume.
        builder.probe_rebuild()
        builder.probe_version()
        builder.probe_standalone(tree.scratch)
        builder.probe_cache_outside_tree()
    else:
        log.write("build failed; the build probes have nothing to ask")

    # Last thing this module does with a compiler, on either path.  Every module
    # after this one drives the built binary with no Go toolchain in the container:
    # deleted before the build when the build was Zig, deleted here when it was Go.
    # Unconditional, so a build that failed halfway does not leave one behind.
    builder.finish()

    # The Builder writes its ledger at `workspace/shim-build.jsonl`, and the
    # workspace it was given is the shared directory -- so it is already where the
    # provenance module looks.  Asserted rather than assumed, because the two
    # filenames are written in two files and a rename in one of them would leave
    # `shim-clean` reading an empty ledger and passing every submission.
    if builder.shim_log != tree.build_ledger:
        raise SystemExit(
            f"the build wrote its ledger to {builder.shim_log} and the provenance "
            f"module reads {tree.build_ledger}; shim-clean would grade nothing"
        )
    if not tree.build_ledger.is_file():
        # An absent ledger and an empty one mean the same thing -- no Go tool was
        # invoked -- but only one of them is a file the next module can open.
        tree.build_ledger.write_bytes(b"")

    vlib.write_json(tree.state_path, {
        "task": TASK,
        "repo": str(tree.repo),
        "home": str(builder.home),
        "cache": str(builder.cache),
        "built": outcome.built,
        "artifact": str(tree.artifact()),
        "artifact_exists": tree.artifact().is_file(),
        "reference_ok": reference_ok,
        "files_copied": copied,
        "outcome": outcome.to_json(),
    })
    log.write(f"published build state: built={outcome.built}")

    emit.metadata["built"] = outcome.built
    emit.metadata["files_copied"] = copied
    # The resolved driver decides which checks are scored at all -- three
    # provenance checks publish unscored under Go -- so it is the single most
    # important fact about how a behavioural score was reached.  Before this it
    # survived only in build.log's prose, which meant a reader with the JSON and
    # not the sibling text logs could not tell full marks earned by a real Zig
    # port from one earned by an untouched Go tree.  Measured on 2026-08-04: a
    # quarter-finished port produced a behavioural.json byte-identical in verdicts
    # to State A's, with 0 keys matching "driver" anywhere in 2.8 MB.
    _drv = builder.driver or {}
    emit.metadata["driver"] = _drv.get("id") or ""
    emit.metadata["driver_language"] = _drv.get("language") or ""
    if not outcome.built:
        # Named from what ran, not spelled here.  Two drivers resolve in this suite
        # and a hardcoded `zig build` would misreport the other one's failure as a
        # Zig failure -- in the one report a reader turns to when everything
        # downstream is red.
        install = " ".join((outcome.observed.get("driver") or {}).get("install")
                           or ["the build"])
        emit.note(
            f"`{install}` did not succeed, so every module that drives the "
            f"executable has nothing to drive. Their cases are recorded as failures "
            f"against this one root cause."
        )
    emit.add(structuremod.run_build_cases(
        tree.repo, outcome, assets.contract, tree.scratch / "build-cases", log,
    ))


def check_reference(assets: Assets, tree: BuiltTree, log: Log) -> tuple[bool, str]:
    """The reference in this image must still produce the frozen answers.

    Not a formality.  The expectations were frozen by a binary built during the
    image build; this confirms the binary shipped beside them is that one, by
    having it re-answer a slice of them and comparing bytes.  Without it, a
    reference replaced or rebuilt after the freeze would keep grading the frozen
    cases correctly -- those are bytes -- while silently answering the fresh
    module's two thousand cases as something else.

    A failure is recorded and reported rather than raised: it costs the fresh
    module, which withholds itself, and there is no reason for it to cost the
    seventeen modules that grade bytes.
    """
    requests = assets.frozen_file("requests")
    expected = assets.frozen_file("expected")
    reference = assets.frozen_file("reference")
    work = tree.shared / "reference-check"
    work.mkdir(parents=True, exist_ok=True)

    def head(src: Path, dst: Path) -> None:
        with src.open("rb") as fh, dst.open("wb") as out:
            for index, line in enumerate(fh):
                if index >= SELF_CHECK_SAMPLE:
                    break
                out.write(line)

    sample, want, got = (work / "sample.ndjson", work / "want.ndjson",
                         work / "got.ndjson")
    head(requests, sample)
    head(expected, want)
    result = vlib.run(
        [str(reference), "--batch", str(sample), str(got)],
        cwd=work, env=tree.reference_env(), timeout=600.0, log=log,
        label="reference-selfcheck",
    )
    if not result.ok:
        return False, (
            f"the reference binary in this image would not answer the frozen "
            f"cases ({result.tail()}); the unseen-inputs module has no oracle"
        )
    want_bytes, got_bytes = want.read_bytes(), got.read_bytes()
    if want_bytes != got_bytes:
        offset, context = vlib.first_difference(want_bytes, got_bytes)
        return False, (
            f"the reference in this image does not reproduce the frozen "
            f"expectations: first difference at byte {offset}. {context}"
        )
    log.write(
        f"reference reproduces the first {SELF_CHECK_SAMPLE} frozen expectations "
        f"byte for byte"
    )
    return True, ""


# -- the modules that drive the built executable ------------------------------


def runner_for(module_id: str, tree: BuiltTree, log: Log) -> probemod.Runner | None:
    """A session against the submission's executable, or None if there is none.

    The ledger is per-module, so a Go invocation during grading is attributed to
    the module that provoked it.  It is read back by the provenance module, which
    is why the filename is derived from the module id rather than shared.
    """
    binary = tree.artifact()
    if not binary.is_file():
        return None
    ledger = tree.shared / f"shim-graded-{module_id}.jsonl"
    scratch = tree.shared / "scratch" / module_id
    scratch.mkdir(parents=True, exist_ok=True)
    return probemod.Runner(
        [str(binary)], log=log, cwd=scratch, env=tree.graded_env(ledger),
    )


def no_artifact_note(tree: BuiltTree) -> str:
    state = tree.state() if tree.available else {}
    if not state.get("built"):
        install = " ".join(
            (((state.get("outcome") or {}).get("observed") or {}).get("driver") or {})
            .get("install") or ["the build"]
        )
        return (
            f"`{install}` did not succeed, so there is no executable to drive. "
            f"Every case in this module is recorded as failed against that one "
            f"cause; see the build module for what the compiler said."
        )
    return (
        f"the build succeeded but installed nothing at {tree.artifact()}, so there "
        f"is no executable to drive. Every case in this module is recorded as "
        f"failed against that one cause."
    )


def module_frozen(module_id: str, assets: Assets, tree: BuiltTree, emit: Emitter,
                  log: Log) -> None:
    """One shard of the frozen cases: this module's families and no others.

    The shard was cut at image build time by the same table that declares this
    module, so what it reads is its own families -- not the whole ten thousand
    cases filtered down, which is seventeen modules each streaming 40 MB to
    discard most of it.
    """
    requests, expected, cases = assets.shard(module_id)
    families = catalog.module_families().get(module_id, ())
    log.section(f"{module_id}: {len(cases)} frozen cases in {list(families)}")
    weights = catalog.family_weights(
        {f: sum(1 for c in cases.values() if c.get("family") == f) for f in families}
    )
    runner = runner_for(module_id, tree, log)
    if runner is None:
        emit.note(no_artifact_note(tree))
        emit.add(failed_cases(cases, weights, "no executable was built"))
        return
    emit.add(runner.run_cases(requests, expected, cases, label=module_id,
                              weights=weights))
    emit.metadata["crashes"] = len(runner.crashes)
    emit.metadata["restarts"] = runner.restarts
    if runner.crashes:
        emit.note(
            f"the executable died {len(runner.crashes)} time(s) while answering "
            f"this module's cases and was restarted; the cases in flight are "
            f"recorded as failures"
        )


def module_fresh(module_id: str, assets: Assets, tree: BuiltTree, emit: Emitter,
                 log: Log) -> None:
    """Cases generated now, from a seed the submission has never seen.

    The anti-memorisation term.  A submission carrying the frozen answers scores
    zero here and keeps its other modules, which is exactly the shape that should
    be visible in a report.

    The generator and the reference are both in this image and nowhere else, so
    neither the inputs nor the answers were reachable from the agent's container.
    A failure to run either of them withholds the module -- status `error`, no
    cases -- rather than failing it: an oracle that will not start in our container
    is our defect, and charging a submission for it would be charging it for our
    container.
    """
    declared = dict((assets.manifest.get("fresh") or {}).get("families") or {})
    seed = int((assets.manifest.get("fresh") or {}).get("seed") or 0)
    count = int((assets.manifest.get("fresh") or {}).get("count") or 0)
    log.section(f"{module_id}: {count} cases from seed {seed}")
    if not seed or not count:
        emit.fail("the manifest declares no fresh seed or count")
        return
    generator = assets.frozen_file("generator")
    reference = assets.frozen_file("reference")
    harvest = assets.frozen_file("upstream_cases")
    work = tree.shared / "fresh"
    work.mkdir(parents=True, exist_ok=True)
    env = tree.reference_env()

    generated = vlib.run(
        [str(generator), "--out", str(work), "--harvest", str(harvest),
         "--seed", str(seed), "--count", str(count)],
        cwd=work, env=env, timeout=900.0, log=log, label="fresh-generate",
    )
    requests = work / "fresh-requests.ndjson"
    index = work / "fresh-cases.json"
    if not generated.ok or not requests.is_file() or not index.is_file():
        emit.fail(f"the fresh case generator failed: {generated.tail()}")
        return
    payload = json.loads(index.read_text(encoding="utf-8"))
    cases = {int(c["id"]): c for c in payload["cases"]}
    counts: dict[str, int] = dict(payload["families"])
    # The generator's own output against what the manifest promised.  A generator
    # that produced a different shape from the same seed is not deterministic, and a
    # module whose weights came from a different census than its cases would divide
    # its budget by the wrong number.
    if declared and counts != declared:
        emit.fail(
            f"the generator produced {counts} from seed {seed}; the image recorded "
            f"{declared}. It is not deterministic, or the image was rebuilt around "
            f"a changed one."
        )
        return
    expected = work / "fresh-expected.ndjson"
    answered = vlib.run(
        [str(reference), "--batch", str(requests), str(expected)],
        cwd=work, env=env, timeout=1800.0, log=log, label="fresh-reference",
    )
    if not answered.ok or not expected.is_file():
        emit.fail(f"the reference would not answer the fresh cases: "
                  f"{answered.tail()}")
        return
    emit.metadata.update({
        "seed": seed,
        "requested": count,
        "generated": len(cases),
        "families": dict(sorted(counts.items())),
        "generate_sec": round(generated.duration, 3),
        "answer_sec": round(answered.duration, 3),
    })
    weights = catalog.family_weights(counts, catalog.FRESH_BUDGETS)
    runner = runner_for(module_id, tree, log)
    if runner is None:
        emit.note(no_artifact_note(tree))
        emit.add(failed_cases(cases, weights, "no executable was built"))
        return
    emit.add(runner.run_cases(requests, expected, cases, label="fresh",
                              weights=weights))
    emit.metadata["crashes"] = len(runner.crashes)


def module_documents(module_id: str, assets: Assets, tree: BuiltTree,
                     emit: Emitter, log: Log) -> None:
    """Twelve YAML files a person might actually have on disk.

    Whole files rather than generated fragments, and compared by digest of the
    reference's answer rather than by value: what is being asked is whether the
    port handles a real document, and a real document is where the interactions
    between features live.
    """
    manifest_path = assets.frozen_file("documents")
    digests_path = assets.frozen_file("document_digests")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))["documents"]
    digests = json.loads(digests_path.read_text(encoding="utf-8"))["digests"]
    log.section(f"{module_id}: {len(manifest)} document cases")
    if not manifest:
        emit.fail("the document manifest is empty")
        return
    missing = [e["key"] for e in manifest if e["key"] not in digests]
    if missing:
        emit.fail(f"{len(missing)} document case(s) have no frozen answer: "
                  f"{missing[:4]}")
        return
    absent = [e["file"] for e in manifest
              if not (assets.documents / e["file"]).is_file()]
    if absent:
        emit.fail(f"document source(s) missing from the image: {sorted(set(absent))}")
        return
    weight = catalog.DOCUMENT_BUDGET / len(manifest)
    runner = runner_for(module_id, tree, log)
    if runner is None:
        emit.note(no_artifact_note(tree))
        emit.add([
            CaseOutcome(case_id=f"documents:{e['key']}", family="documents",
                        kind="document", passed=False, weight=weight,
                        detail="no executable was built")
            for e in manifest
        ])
        return
    emit.add(runner.run_documents(manifest, digests, assets.documents,
                                 weight=weight))
    emit.metadata["documents"] = len(manifest)


def module_protocol(module_id: str, assets: Assets, tree: BuiltTree,
                    emit: Emitter, log: Log) -> None:
    """The transport, and the process that speaks it.

    Two tables in one module because they answer one question from two sides.  The
    protocol cases are lines the generator cannot carry -- a truncated object, a bare
    `[`, 40 KB of one key -- and the probe cases are about the process: that it
    flushes, that it reads to EOF, that it survives being copied away from its
    build tree.  Both are the transport every other module assumes already works.
    """
    lines_path = assets.frozen_file("protocol_requests")
    answers_path = assets.frozen_file("protocol_expected")
    cases_path = assets.frozen_file("protocol_cases")
    lines, answers, cases = probemod.load_protocol(cases_path, lines_path,
                                                   answers_path)
    log.section(f"{module_id}: {len(cases)} protocol cases, "
                f"{len(catalog.PROBE_CASES)} process cases")
    if not cases:
        emit.fail("the protocol case file is empty")
        return
    weight = catalog.PROTOCOL_BUDGET / len(cases)
    runner = runner_for(module_id, tree, log)
    outcome = tree.outcome()
    if runner is None:
        emit.note(no_artifact_note(tree))
        emit.add([
            CaseOutcome(case_id=f"protocol:{case.get('id', index)}",
                        family="protocol", kind="protocol", passed=False,
                        weight=weight, detail="no executable was built")
            for index, case in enumerate(cases)
        ])
    else:
        emit.add(runner.run_protocol(lines, answers, cases, weight=weight))

    # The process cases run whether or not a session could be established: several
    # of them are about a probe that will not start, and answering "no executable"
    # is what they are for.
    emit.add(structuremod.run_probe_cases(
        tree.repo, outcome, assets.contract,
        tree.shared / "scratch" / f"{module_id}-cases", log,
        probe_answer=(lambda request: ask_once(tree, module_id, request, log)),
    ))


def ask_once(tree: BuiltTree, module_id: str, request: dict, log: Log) -> dict | None:
    """Put one request to the executable in a session of its own.

    Used by the handshake case, which needs an answer rather than a comparison.
    A session per question is wasteful and deliberate: the case is about what the
    binary says when asked, and reusing a session that a previous question left in
    an unknown state would answer a different question.
    """
    runner = runner_for(module_id, tree, log)
    return None if runner is None else runner.ask_one(request)


def module_provenance(module_id: str, assets: Assets, tree: BuiltTree,
                      emit: Emitter, log: Log) -> None:
    """What the build and the binary say about how they were made."""
    baseline = assets.baseline
    if not baseline.is_dir() or not any(baseline.glob("*.go")):
        # Two of the four checks search the binary for State A's code.  Without the
        # baseline they cannot be answered, and answering them anyway would fail
        # every submission for a file missing from our image.
        emit.fail(
            f"the frozen State A tree is missing or holds no Go at {baseline}; "
            f"two provenance checks read it and would fail every submission"
        )
        return
    emit.add(provmod.run_provenance_cases(
        tree.repo, tree.outcome(), baseline, assets.contract, log,
        spawn_probe=(lambda: spawn_probe(assets, tree, log)),
    ))


def spawn_probe(assets: Assets, tree: BuiltTree, log: Log) -> tuple[list[dict], str]:
    """Run the executable with nothing but tripwires on PATH.

    A static reading of the source can be evaded by assembling the tool's name at
    run time, so this asks empirically.  The requests exercise the whole library
    rather than the handshake: a wrapper would be reached by parsing, not by saying
    hello.

    PATH here is the tripwire directory alone -- not the tripwires ahead of the
    real toolchain, as the build gets.  Nothing from PATH is needed to parse a
    string, so the strictest possible environment is also a fair one, and it closes
    the `sh -c` indirection as well.

    The ledgers from the graded modules are read too.  A submission that spawns a
    Go tool only for some inputs -- only for aliases, only past a size threshold --
    would pass four synthetic requests and then do it ten thousand times while
    being graded, and those ledgers are the better evidence.
    """
    binary = tree.artifact()
    if not binary.is_file():
        return [], "no executable was built, so it spawned nothing either"
    state = tree.state()
    ledger = tree.shared / "shim-spawn.jsonl"
    if ledger.exists():
        ledger.unlink()
    env = buildmod.shim_env(
        tree.shim_dir, ledger, home=Path(state["home"]), cache=Path(state["cache"]),
        PATH=str(tree.shim_dir),
    )
    # Enough surface that a wrapper would have to be invoked for all of it: an
    # anchor and an alias, a block scalar, a tag, a comment, a flow mapping and a
    # resolver case, then the emitter over the same tree.
    source = (
        "# head\n"
        "anchored: &a {x: 1, y: [2, 3]}\n"
        "alias: *a\n"
        "block: |-\n  first\n  second\n"
        "tagged: !!str 0o17\n"
        "plain: .inf   # line\n"
    )
    scratch = tree.shared / "scratch" / "spawn"
    scratch.mkdir(parents=True, exist_ok=True)
    runner = probemod.Runner([str(binary)], log=log, cwd=scratch, env=env)
    requests = [
        {"id": index, "op": op, "source": source}
        for index, op in enumerate(catalog.SPAWN_PROBE_OPS, 1)
    ]
    _answers, answered = runner.ask_batch(requests)

    events = buildmod.read_shim_ledger(ledger)
    graded: list[dict] = []
    for path in sorted(tree.shared.glob("shim-graded-*.jsonl")):
        graded.extend(buildmod.read_shim_ledger(path))
    if graded:
        log.write(
            f"{len(graded)} Go invocation(s) recorded across the graded modules' "
            f"ledgers"
        )
    note = (
        f"the executable answered {answered}/{len(requests)} requests covering "
        f"anchors, block scalars, tags, comments and the emitter with only "
        f"tripwires on PATH, and the graded modules' ledgers hold "
        f"{len(graded)} invocation(s)"
    )
    return events + graded, note


def failed_cases(cases: dict[int, dict], weights: dict[str, float],
                 reason: str) -> list[CaseOutcome]:
    """Every case in one shard, recorded as failed.

    Enumerated rather than summarised: the module's denominator has to be the same
    whether or not the executable existed, or a submission that built nothing would
    divide a small earned total by a small possible total and come out looking
    competent.
    """
    return [
        CaseOutcome(
            case_id=f"case:{case_id}",
            family=meta.get("family", "unknown"),
            kind="probe", passed=False,
            weight=weights.get(meta.get("family", ""), 1.0),
            detail=reason,
        )
        for case_id, meta in sorted(cases.items())
    ]


# -- dispatch -----------------------------------------------------------------
# Module kind -> the function that grades it.  The build module takes a different
# argument list from the other five because it is the only one that runs before a
# built tree exists, so it is dispatched separately rather than being bent into
# this signature.
KINDS: dict[str, object] = {
    "frozen": module_frozen,
    "fresh": module_fresh,
    "documents": module_documents,
    "protocol": module_protocol,
    "provenance": module_provenance,
}


def module_entry(module_id: str) -> dict:
    for module in catalog.MODULES:
        if module["id"] == module_id:
            return module
    raise SystemExit(
        f"no module named {module_id!r} in catalog.MODULES; the suite declares a "
        f"module the driver cannot dispatch"
    )


def run_module(module_id: str, emit: Emitter, log: Log) -> None:
    entry = module_entry(module_id)
    kind = entry["kind"]
    assets = Assets(DEFAULT_ASSETS, log)
    contract = assets.contract

    if kind == "build":
        submitted = Path(os.environ.get("SRB_REPO", "/workspace/repo"))
        tree = BuiltTree(shared_dir(), contract, log)
        module_build(assets, tree, submitted, emit, log)
        return

    handler = KINDS.get(kind)
    if handler is None:
        raise SystemExit(
            f"module {module_id!r} declares kind {kind!r}, which the driver has no "
            f"handler for; add it to KINDS or fix catalog.MODULES"
        )
    tree = BuiltTree(shared_dir(), contract, log)
    if not tree.available:
        # The build module publishes the tree.  If it is missing, the build module
        # did not finish -- it crashed, or the suite ran this module first.  Either
        # way the cause is upstream of here, and reporting `error` says so, where
        # failing every case would say the submission is broken.
        raise SystemExit(
            f"{module_id}: no built tree at {tree.state_path}. The `build` module "
            f"publishes it and must run first; it either did not run or did not "
            f"finish."
        )
    handler(module_id, assets, tree, emit, log)


def shared_dir() -> Path:
    """The directory the suite gives every module for shared state.

    `SRB_SUITE_WORK` is the infra's one documented answer to "a build that several
    modules need and that costs minutes to repeat", which is exactly this.  There
    is no fallback: running without it would give each module its own empty
    directory, twenty-one of them would find no tree, and the report would blame
    the submission for a harness misconfiguration.
    """
    raw = os.environ.get("SRB_SUITE_WORK")
    if not raw:
        raise SystemExit(
            "SRB_SUITE_WORK is unset. Every module but `build` reads the built "
            "tree from it; without it there is nothing to grade against."
        )
    path = Path(raw)
    path.mkdir(parents=True, exist_ok=True)
    return path


# -- self-check ---------------------------------------------------------------


def self_check() -> int:
    """Prove the dispatch and the catalog agree, without a submission.

    Run at image build time.  What it catches is the class of mistake that is
    invisible until a real submission is graded: a module in the table with no
    handler, a shard the freeze never cut, a weight in `suite.toml` that drifted
    from the catalog. All of those produce a module whose verdict is `error` hours
    into a grading run.
    """
    problems: list[str] = []
    # Counted, not assumed.  The driver table runs inside the `assets is not None`
    # branch below, so "no problems" and "never ran" are the same empty list; this
    # is what the success line reports, and zero is a problem in its own right.
    drivers_checked = 0

    for module in catalog.MODULES:
        kind = module["kind"]
        if kind != "build" and kind not in KINDS:
            problems.append(
                f"module {module['id']!r} has kind {kind!r} with no handler in KINDS"
            )
    for kind in KINDS:
        if not any(m["kind"] == kind for m in catalog.MODULES):
            problems.append(f"KINDS has a handler for {kind!r}, which no module uses")

    problems.extend(catalog.check_modules())

    log = Log(None)
    try:
        assets = Assets(DEFAULT_ASSETS, log)
    except Exception as exc:  # noqa: BLE001 - report it, do not traceback
        problems.append(f"the frozen assets would not load: {type(exc).__name__}: {exc}")
        assets = None

    if assets is not None:
        promised = dict(assets.counts.get("module_cases") or {})
        for module in catalog.MODULES:
            if module["kind"] != "frozen":
                continue
            # Through `shard()` rather than by stat, so the digest keys this driver
            # composes are checked against the ones the freeze actually wrote.  A
            # key that drifted is a KeyError here, at image build time, instead of
            # twenty minutes into a grading run.
            try:
                _reqs, _exps, cases = assets.shard(module["id"])
            except SystemExit as exc:
                problems.append(f"module {module['id']!r}: {exc}")
                continue
            if not cases:
                problems.append(f"the {module['id']!r} shard holds no cases")
            if module["id"] not in promised:
                problems.append(
                    f"the manifest's module_cases does not mention {module['id']!r}"
                )
        stray = sorted(set(promised) - {m["id"] for m in catalog.MODULES})
        if stray:
            problems.append(
                f"the manifest promises shards for modules that no longer exist: "
                f"{stray}"
            )
        # Every non-shard input, derived from `Assets.frozen` and actually read.
        # Derived rather than enumerated: a hand-written list omits an input
        # silently, and the omission costs the whole stage at grading time while
        # the image builds clean.
        for key in sorted(assets.frozen):
            try:
                assets.frozen_file(key)
            except SystemExit as exc:
                problems.append(f"frozen input {key!r}: {exc}")
        # And the other direction: a digest freeze writes that no module can reach
        # is either a dead entry or a read that is silently unverified.
        shards = {k for k in assets.digests if k.startswith("module/")}
        orphans = sorted(set(assets.digests) - shards - set(assets.frozen))
        if orphans:
            problems.append(
                f"the manifest carries digests nothing verifies: {orphans}; either "
                f"freeze.py:digest_all or Assets.frozen is wrong"
            )
        if not assets.baseline.is_dir():
            problems.append(
                f"the frozen State A tree is missing at {assets.baseline}; the "
                f"provenance module reads it"
            )
        # Driver detection, against a table of trees.  Here rather than in a test
        # nobody runs, because detection decides the compiler, the artefact path,
        # the allowed build outputs and which provenance cases are scored -- and
        # every module downstream reads the resolved answer instead of the tree, so
        # a misroute is not recoverable later in the run.
        try:
            problems.extend(buildmod.check_drivers(
                vlib.read_json(assets.frozen_file("contract"))))
            drivers_checked = len(buildmod.DRIVER_RESOLUTION_CASES)
        except SystemExit as exc:
            problems.append(f"the driver resolution table could not run: {exc}")

    if drivers_checked != len(buildmod.DRIVER_RESOLUTION_CASES):
        problems.append(
            f"the driver resolution table did not run: {drivers_checked} of "
            f"{len(buildmod.DRIVER_RESOLUTION_CASES)} case(s) resolved"
        )

    for problem in problems:
        print(f"self-check: {problem}", file=sys.stderr)
    if problems:
        print(f"self-check: {len(problems)} problem(s)", file=sys.stderr)
        return 1
    print(
        f"self-check: {len(catalog.MODULES)} modules, "
        f"{len(KINDS) + 1} kinds, assets verified, "
        f"{drivers_checked} driver resolution case(s) agree"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--module", help="module id, or $SRB_MODULE_ID")
    parser.add_argument("--result", help="where to write the result JSON, or "
                                        "$SRB_RESULT")
    parser.add_argument("--self-check", action="store_true",
                        help="validate the dispatch against the catalog and the "
                             "frozen assets, then exit")
    args = parser.parse_args(argv)

    if args.self_check:
        return self_check()

    module_id = args.module or os.environ.get("SRB_MODULE_ID") or ""
    if not module_id:
        parser.error("--module or SRB_MODULE_ID is required")
    result_path = Path(args.result or os.environ.get("SRB_RESULT")
                       or f"/tmp/{module_id}.json")

    log = Log(vlib.LOG_DIR / f"{module_id}.log")
    emit = Emitter(module_id, result_path)
    started = vlib.now()
    try:
        run_module(module_id, emit, log)
    except SystemExit as exc:
        # A raised SystemExit here means the module could not be graded at all:
        # no assets, no built tree, an unknown kind.  It is written as a module
        # with zero checks, which the infra records as `error` -- distinct from a
        # module that graded its cases and found them wrong.
        emit.abort(str(exc.code) if exc.code not in (None, 0) else "aborted")
    except Exception as exc:  # noqa: BLE001 - a traceback in the log, not in stdout
        log.write("".join(traceback.format_exception(exc)))
        emit.abort(f"{type(exc).__name__}: {exc}")
    finally:
        # Always, and last.  The infra reads the file; a module that raised
        # without writing one is a module whose failure says nothing about why.
        emit.metadata["duration_sec"] = round(vlib.now() - started, 2)
        emit.metadata["log"] = str(log.path) if log.path else ""
        emit.write()
        log.close()
    return emit.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
