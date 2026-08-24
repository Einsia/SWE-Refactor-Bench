#!/usr/bin/env python3
"""The engine every module in this suite runs.

`run-module.sh` invokes this with `--module <id>`; the slice of the corpus that id
owns is `catalog.MODULE_FAMILIES[id]`, and everything else about a module -- its
weight, its timeout, its title -- lives in `suite.toml`. So fifteen modules are
fifteen entries in a table rather than fifteen programs, and the only thing that
distinguishes them at runtime is which families they read.

Three modules are not plain corpus slices and are handled ahead of the dispatch:

  `build`       produces the artifact the other fourteen need.
  `structure`   grades seven cases computed by driving the delivered tree in ways
                the corpus cannot express -- from another directory, with `src/`
                deleted, through `require`.
  `types`       compiles fixed consumer programs against the declarations the
                submission emitted. It is the only module that reads a `.d.ts` at
                all, and it does so by compiling, never by matching text.

sys.path
--------
`run-module.sh` runs this under `python3 -I`. Isolated mode implies `-P`, which
drops the script's own directory from `sys.path`, so `import catalog` fails before
anything else can. The insertion below is what makes the imports work, and it is
load-bearing in a way that is invisible to every static check: removing it makes
all fifteen modules fail to start, and the stage reports a dead verifier rather
than a bad submission. That failure mode is why `-I` is worth the annotation --
the isolation is deliberate (a submission ships a `package.json` and could ship a
`catalog.py` too; nothing it writes should be importable by the program grading
it), but it costs this one line.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # see the module docstring

import build as buildmod  # noqa: E402
import catalog  # noqa: E402
import executor  # noqa: E402
import freeze  # noqa: E402
import structure as structmod  # noqa: E402
import vlib  # noqa: E402
from vlib import Log  # noqa: E402

# `types-surface.py` is not an identifier, so it cannot be imported by name. The
# hyphen is deliberate and matches every other module directory in the suite; the
# alternative -- renaming the file to `types_surface.py` -- would make one module's
# file the odd one out to satisfy a syntax rule that `importlib` already answers.
types_surface = importlib.import_module("types-surface")


def env_path(name: str, *, required: bool = True) -> Path | None:
    raw = os.environ.get(name, "")
    if not raw:
        if required:
            raise SystemExit(f"driver: ${name} is not set; "
                             f"the behavioural runner sets it")
        return None
    return Path(raw)


class Driver:
    """One module's run, start to finish."""

    def __init__(self, module_id: str) -> None:
        self.module_id = module_id
        self.repo = env_path("SRB_REPO")
        self.suite_dir = env_path("SRB_SUITE_DIR")
        self.work = env_path("SRB_WORK")
        self.shared = env_path("SRB_SUITE_WORK")
        self.result_path = env_path("SRB_RESULT")
        self.original = env_path("SRB_ORIGINAL", required=False)
        self.log = Log(self.work / "module.log")
        self.checks: list[dict] = []
        self.metadata: dict = {"module": module_id}
        self.started = time.time()

    # -- reporting ---------------------------------------------------------

    def add(self, check_id: str, passed: bool, summary: str, *,
            weight: float = 1.0, required: bool = False, detail: str = "",
            verdict: str | None = None) -> None:
        self.checks.append({
            "id": check_id,
            "verdict": verdict or ("pass" if passed else "fail"),
            "summary": summary[:1000],
            "weight": weight,
            "required": required,
            "detail": detail[:4000],
        })

    def add_case(self, outcome: vlib.CaseOutcome, weight: float) -> None:
        payload = outcome.to_dict()
        self.checks.append({
            "id": outcome.case_id,
            "verdict": "pass" if outcome.passed else "fail",
            "summary": f"{outcome.family}/{outcome.kind}",
            "weight": weight,
            "required": False,
            "detail": (payload.get("detail", "") + (
                "\n" + payload["diff"] if payload.get("diff") else ""))[:4000],
            "metadata": {"family": outcome.family, "op": outcome.kind},
        })

    def emit(self, status: str = "ok", summary: str = "") -> int:
        """Write $SRB_RESULT. Always called, including on the failure paths.

        A module that raises without writing this file gives the runner no checks
        at all, which reads as "the module found nothing wrong". So every exit
        from this program goes through here, and a crash writes status=error with
        the traceback as the summary -- a failure must state a reason.
        """
        payload = {
            "status": status,
            "summary": summary[:1000],
            "checks": self.checks,
            "metadata": self.metadata | {
                "duration_sec": round(time.time() - self.started, 2),
                "checks": len(self.checks),
                "commands": self.log.commands[-40:],
            },
        }
        self.result_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.result_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=1, sort_keys=False)
        self.log.write(f"{self.module_id}: {status}, "
                       f"{len(self.checks)} check(s)"
                       f"{': ' + summary if summary else ''}")
        self.log.close()
        return 0

    # -- the corpus --------------------------------------------------------
    #
    # Two files per stem, paired by line index rather than by id.
    #
    # Pairing is positional because id pairing cannot work: every case whose id the
    # protocol rejects is answered under 0, so ids are not unique across the
    # corpus. Nor is one answer always one line -- ten protocol cases are graded on
    # their whole stdout stream, several response lines and the trailing newline
    # included -- which is why the expectation file is a JSON envelope per case
    # rather than raw NDJSON. `freeze.load_expected` is the only reader of that
    # format and both sides call it.
    #
    # The envelope also carries the case id, so the pairing is checked rather than
    # assumed. It is still not what pairs them; it is what catches a file that has
    # drifted. The failure mode of a positional pairing gone wrong is every case
    # being graded against its neighbour's expectation, which looks exactly like a
    # catastrophically bad port, and it would be the one report nobody could
    # interpret.

    def assets(self) -> Path:
        root = Path(os.environ.get("SWEREFACTOR_ASSETS", "/opt/assets"))
        if not root.is_dir():
            raise SystemExit(f"driver: assets directory {root} is missing; "
                             f"the image build populates it")
        return root

    def load_corpus(self, stem: str) -> list[dict]:
        """Read `<stem>-cases.jsonl` and `<stem>-expected.jsonl` as one list."""
        base = self.assets() / "corpus"
        cases_path = base / f"{stem}-cases.jsonl"
        expected_path = base / f"{stem}-expected.jsonl"
        for path in (cases_path, expected_path):
            if not path.is_file():
                raise SystemExit(f"driver: {path} is missing")

        cases = [json.loads(line) for line in
                 cases_path.read_text(encoding="utf-8").splitlines()
                 if line.strip()]
        expected = freeze.load_expected(expected_path)
        if len(cases) != len(expected):
            raise SystemExit(
                f"driver: {stem} has {len(cases)} cases and {len(expected)} "
                f"expectations; the pairing is positional so this cannot be "
                f"graded (freeze.py writes both and asserts equal length)")
        for case, (case_id, want) in zip(cases, expected):
            if case_id != case["id"]:
                raise SystemExit(
                    f"driver: {stem}: expectation {case_id!r} sits where case "
                    f"{case['id']!r} does. The two files have drifted out of "
                    f"order, and grading them would compare every case from here "
                    f"on against a neighbour's answer.")
            case["expected"] = want
        return cases

    def corpus_index(self) -> tuple[dict[str, set[str]], dict[str, float]]:
        """Which families live in which stem, and what one case of each is worth.

        Both halves come from counting all four stems, and both need the whole
        corpus rather than this module's slice of it.

        The weights, because a family's per-case weight is its budget divided by how
        many cases are in it -- a property of the corpus, not of whoever is grading.
        Handing `family_weights` only the subset a module owns would return the same
        numbers, since the division is per-family, and would trip its completeness
        guard: the one that fires when a family carries a budget and has no cases.
        That guard is worth keeping live at grading time. It is what notices assets
        generated apart from the catalog that shipped with them, which a Dockerfile
        edit can produce and no other check would see.

        The stem map, because the alternative is a table in this file saying which
        families are in which file, and that table can be wrong in a way nothing
        reports: a module that owns two families and looks in one stem grades half
        its slice and calls the other half absent. Counting is cheap -- four files,
        one `json.loads` per line, against a corpus this module is about to run
        thirteen thousand processes' worth of work on -- and it cannot drift from
        what `gen.py` actually emitted.
        """
        base = self.assets() / "corpus"
        stem_families: dict[str, set[str]] = {}
        counts: dict[str, int] = {}
        for stem in freeze.STEMS:
            path = base / f"{stem}-cases.jsonl"
            if not path.is_file():
                raise SystemExit(f"driver: {path} is missing")
            per_stem = catalog.family_counts_in(path)
            stem_families[stem] = set(per_stem)
            for family, n in per_stem.items():
                counts[family] = counts.get(family, 0) + n
        return stem_families, catalog.family_weights(counts)

    def families_for_module(self) -> tuple[str, ...]:
        families = catalog.MODULE_FAMILIES.get(self.module_id)
        if families is None:
            raise SystemExit(
                f"driver: module {self.module_id!r} is not in "
                f"catalog.MODULE_FAMILIES; suite.toml and catalog.py disagree "
                f"about which modules exist")
        return families

    # -- the probe under test ---------------------------------------------
    #
    # Built once by the `build` module into $SRB_SUITE_WORK, which exists for
    # exactly this: fourteen modules need the same artifact and rebuilding it
    # fifteen times would multiply the build by fifteen.
    #
    # A module that finds no ledger does not build one itself. It reports the
    # dependency as an error and stops, because a module that silently rebuilt
    # would turn "the build is broken" into fifteen identical build failures spread
    # across the report instead of one, and each of them would be charged to the
    # module's own subject.

    def probe_prefix(self) -> tuple[list[str], Path, dict]:
        """The argv prefix, working directory and ledger for the built probe.

        The prefix is `[node, <build>/dist/probe.js]` -- an argv list, not a path,
        even though every element of it is the same for every module. `structure`
        invokes the same build from a different directory and the verification stage
        drives two trees at once, and neither is expressible by a function that
        returned a directory and appended `dist/probe.js` itself.

        `node` comes out of the ledger rather than off PATH. The build resolved it
        once, absolutely, and recorded which one it used; a module that resolved it
        again could pick up a different one, and "the two sides ran on different
        runtimes" is not a finding any report of this stage could survive.
        """
        ledger_path = self.shared / "build-ledger.json"
        if not ledger_path.is_file():
            raise SystemExit(
                f"driver: {ledger_path} is absent, so the `build` module did not "
                f"complete; this module has nothing to run. The build module's "
                f"own result carries the reason.")
        ledger = vlib.read_json(ledger_path)
        if not ledger.get("ok"):
            raise SystemExit(
                f"driver: the build did not produce a probe "
                f"({ledger.get('summary', 'no reason recorded')}); "
                f"this module has nothing to run")
        probe = Path(ledger["probe_js"])
        if not probe.is_file():
            raise SystemExit(f"driver: the build recorded {probe}, which is gone")
        return ([ledger.get("node", "node"), str(probe)],
                Path(ledger["build_dir"]), ledger)

    # -- a corpus module ---------------------------------------------------

    def grade_corpus(self) -> tuple[int, int]:
        """Run this module's corpus slice and record a check per case.

        Returns `(passed, total)` and does *not* emit, so that a module with a
        corpus slice and a second half of its own can land both in one result file.
        No module in this suite currently has both -- `structure` and `types` have
        no corpus slice at all -- and the split is kept anyway: collapsing it would
        mean the first module that grew a second half had to restructure this
        function under time pressure.

        Families in `catalog.NON_CORPUS_FAMILIES` are dropped before the corpus is
        read. Neither `_release` nor `_types` appears in any `*-cases.jsonl` and
        neither ever will -- one is computed by driving the build, the other by
        compiling against it -- so leaving them in the set would make the
        "absent from the corpus" refusal below fire on a module that is working
        correctly.
        """
        families = set(self.families_for_module()) - catalog.NON_CORPUS_FAMILIES
        if not families:
            raise SystemExit(
                f"driver: module {self.module_id!r} has no corpus families left "
                f"after dropping {sorted(catalog.NON_CORPUS_FAMILIES)}; a module "
                f"that grades no corpus cases should not be reaching this path")
        prefix, cwd, ledger = self.probe_prefix()
        self.metadata["families"] = sorted(families)
        self.metadata["build"] = {k: ledger.get(k)
                                  for k in ("node", "node_version", "tsc",
                                            "probe_js")}

        # The corpus is four pairs of files rather than one, because three slices of
        # it are not shaped like the bulk: `fresh` is generated from a seed instead
        # of committed, `protocol` cases carry raw wire lines where the others carry
        # a request object, and `fixture` cases carry the name of the dataset they
        # came from. Which stem holds which family is read off the assets, not
        # asserted here -- see `corpus_index`.
        stem_families, weights = self.corpus_index()
        stems = sorted(stem for stem, present in stem_families.items()
                       if present & families)
        unplaced = sorted(families - set().union(*stem_families.values()))
        if unplaced:
            self.add(f"{self.module_id}/corpus-present", False,
                     f"no stem of the corpus contains {unplaced}",
                     required=True,
                     detail="This module owns families the corpus does not have. "
                            "catalog.check_catalog compares the two at image build "
                            "time, so reaching this at grading time means the "
                            "assets and the catalog were built apart.")
            raise SystemExit(f"families absent from the corpus: {unplaced}")

        cases: list[dict] = []
        for stem in stems:
            for case in self.load_corpus(stem):
                if case.get("family") in families:
                    cases.append(case)

        # No emptiness check here. `family_counts_in` only reports families it
        # counted at least one case of, so a family named in `stem_families` has
        # cases in that stem by construction, and `unplaced` above has already
        # caught the families that are in no stem at all.
        counts: dict[str, int] = {}
        for case in cases:
            counts[case["family"]] = counts.get(case["family"], 0) + 1
        self.metadata["case_counts"] = counts
        self.metadata["stems"] = stems
        unweighted = sorted(set(counts) - set(weights))
        if unweighted:
            raise AssertionError(
                f"{self.module_id}: no weight for family(ies) {unweighted}; the "
                f"cases were read and the weights computed from the same four "
                f"files, so this is not reachable by drift between them")

        runner = executor.ProbeRunner(
            prefix, cwd=cwd, log=self.log, label=f"{self.module_id}:probe")
        requests = [executor.request_for(case) for case in cases]
        self.log.write(f"{self.module_id}: {len(requests)} case(s) across "
                       f"{len(counts)} family(ies)")
        outcome = runner.run_all(requests)
        # Positional, matching how `freeze.capture` paired the expectations to
        # these same cases. Looking each answer up by its predicted id instead
        # collapsed every id-0 protocol case onto one key; see BatchOutcome.
        if len(outcome.ordered) != len(cases):
            raise AssertionError(
                f"{self.module_id}: ran {len(cases)} cases and got "
                f"{len(outcome.ordered)} answer slots; the positional pairing "
                f"between cases and answers cannot be resolved here")

        passed = 0
        for case, response in zip(cases, outcome.ordered):
            ok, detail, diff = executor.compare(case["expected"], response)
            passed += ok
            weight = weights[case["family"]]  # asserted present above
            self.add_case(
                vlib.CaseOutcome(
                    case_id=case["id"], family=case["family"],
                    kind=case.get("request", {}).get("op", "") if isinstance(
                        case.get("request"), dict) else "protocol",
                    passed=ok, weight=weight, detail=detail, diff=diff),
                weight)

        self.metadata["probe_spawns"] = outcome.spawns
        if outcome.complaints:
            # Reported, not scored. The cases these lines belong to already failed
            # on their own; charging again for the malformed output would grade one
            # defect twice.
            self.metadata["stdout_complaints"] = outcome.complaints[:40]
        if outcome.crashed:
            self.metadata["crashed"] = True
            self.metadata["stderr_tail"] = outcome.stderr_tail

        return passed, len(cases)

    def run_corpus_module(self) -> int:
        """A module that is nothing but a corpus slice: grade it and emit."""
        passed, total = self.grade_corpus()
        rate = passed / total if total else 0.0
        return self.emit("ok", f"{passed}/{total} cases ({rate:.1%})")

    # -- dispatch ----------------------------------------------------------

    def run(self) -> int:
        if self.module_id == "build":
            return buildmod.run(self)
        if self.module_id == "structure":
            return structmod.run(self)
        if self.module_id == "types":
            return types_surface.run(self)
        return self.run_corpus_module()


def main(argv: list[str]) -> int:
    module_id = ""
    for index, arg in enumerate(argv):
        if arg == "--module" and index + 1 < len(argv):
            module_id = argv[index + 1]
        elif arg.startswith("--module="):
            module_id = arg.split("=", 1)[1]
    module_id = module_id or os.environ.get("SRB_MODULE_ID", "")
    if not module_id:
        print("driver: --module is required", file=sys.stderr)
        return 2

    driver = Driver(module_id)
    try:
        return driver.run()
    except SystemExit as exc:
        # A SystemExit here is a dependency this module cannot satisfy -- no build
        # ledger, a missing corpus file. It still has to reach $SRB_RESULT with a
        # reason: a module that exits without writing that file gives the runner
        # no checks, which reads as "nothing was found wrong".
        message = str(exc.code) if exc.code not in (0, None) else "stopped"
        driver.add(f"{module_id}/module-ran", False, message[:400], required=True,
                   detail=message, verdict="error")
        return driver.emit("error", message)
    except Exception as exc:  # noqa: BLE001 -- the last line before a silent zero
        import traceback
        tb = traceback.format_exc()
        driver.log.write(f"{module_id}: unhandled {type(exc).__name__}: {exc}")
        driver.add(f"{module_id}/module-ran", False,
                   f"the module crashed: {type(exc).__name__}: {exc}",
                   required=True, detail=tb, verdict="error")
        return driver.emit("error", f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
