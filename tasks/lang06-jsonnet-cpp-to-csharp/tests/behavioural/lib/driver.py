#!/usr/bin/env python3
"""Runs one behavioural module of lang06-jsonnet-cpp-to-csharp.

Every module asks the same question in a different slice: *given these bytes on
stdin and these arguments, does the submission's program print what the original
printed?*  Nothing here reads the submission's source.  The only thing that
touches it is the `build` module, which publishes it and then forgets how.

That is deliberate and it is the whole design of this stage.  The original is a
pair of command-line programs; a port of it is a pair of command-line programs;
the port is complete exactly insofar as the two pairs are indistinguishable from
outside.  A check that looked at a class name would be measuring one way of
getting there, and there are many.

    driver.py --module build      publish the submission's two programs
    driver.py --module num        one family of cases
    driver.py --self-check        prove the module map is total, at build time
    driver.py --replay-reference  prove the case list is answerable, at build time

The expectations were frozen at image-build time by running the *original* over
the same case list, in this same executor.  So a module's job is three steps:
select its cases, run them against the submission, and diff.  What "diff" means
is byte equality of stdout, of stderr, of the exit status, and of every file the
run created, changed or removed -- because all four are things a user of the
original can see, and a port that gets three of them right is not finished.

Which leaves one thing the frozen record cannot tell you about itself: whether the
program it was recorded from would pass it.  It should, tautologically, and it does
not follow -- an output that embeds a path, a clock or an address is stable when
recorded once and different when replayed, and the case is then unpassable by
anything while looking in a report exactly like a port that got it wrong.
`--replay-reference` grades the reference against its own record and requires
2,602/2,602, so a case like that fails the image build instead of silently costing
every submission the same weight.  The eighteen behaviour families carry the whole
1.00 of this stage, which is what makes that number the one worth proving.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

# `run-module.sh` execs `python3 -I -B`, which implies -P: the script's own directory
# is NOT on sys.path.  Without this line every import below fails, and it fails
# only under the real runner -- a host-side test that happens to run from lib/
# would pass.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import build as buildmod  # noqa: E402
import cases as casemod  # noqa: E402
import executor  # noqa: E402
import spec  # noqa: E402
import vlib  # noqa: E402
from vlib import Log  # noqa: E402

#: One module per case family.  The mapping is the identity on purpose: the
#: families in spec.FAMILY_WEIGHTS were chosen as the units a reviewer would
#: group by, and re-grouping them here would mean two places to state the same
#: judgment.  `_check_total` proves the map is a bijection, so a family added to
#: the case list without a module -- which would silently stop being graded --
#: fails the image build instead.
FAMILY_MODULES: tuple[str, ...] = tuple(spec.FAMILY_WEIGHTS)

#: The one module that does not grade cases.  It stays at weight 0.00 because it
#: publishes the two programs every family below runs, so its failure is already
#: charged through them.  The other weight-0 module, `artifacts`, read the published
#: assemblies' metadata tables and reported on them without being paid for it; a
#: module that cannot affect the score is not a module.
BUILD_MODULE = "build"

BUILD_STATE = "build.json"

#: How long one case may run before it is recorded as a timeout.  The frozen
#: expectations were taken at the same limit, and no upstream case comes close:
#: the slowest is under two seconds.  A submission that needs more than this has
#: a performance defect that is indistinguishable, from outside, from a hang.
CASE_TIMEOUT = executor.DEFAULT_TIMEOUT

#: Cases whose reference outcome was excluded at freeze time (the original
#: aborted or hung).  There are none in a clean freeze; the field exists so that
#: if one ever appears it is skipped rather than graded against a crash.
EXCLUDED_KEY = "excluded"


def _check_total(expectations: dict) -> None:
    """The module map, the weight table and the frozen families must agree.

    Three lists that have to say the same thing, checked in every direction.  A
    family in the expectations that no module claims stops being graded and
    nothing fails; a module claiming a family that is not there scores zero over
    an empty pool and drags the stage down.  Neither is visible in a report.
    """
    frozen = set(expectations.get("family_counts", {}))
    weighted = set(spec.FAMILY_WEIGHTS)
    claimed = set(FAMILY_MODULES)

    if claimed != weighted:
        raise SystemExit(
            f"the module map and the weight table disagree: "
            f"only in map {sorted(claimed - weighted)}, "
            f"only in weights {sorted(weighted - claimed)}")
    if frozen != claimed:
        raise SystemExit(
            f"the frozen expectations and the module map disagree: "
            f"frozen but unclaimed {sorted(frozen - claimed)}, "
            f"claimed but not frozen {sorted(claimed - frozen)}. A family no "
            f"module claims is not graded and nothing fails, so this is a "
            f"verifier defect rather than a submission failure.")


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
            weight: float = 1.0, detail: str = "", required: bool = False,
            **extra: Any) -> None:
        entry: dict[str, Any] = {
            "id": check_id,
            "unit": self.module,
            "verdict": verdict,
            "summary": summary,
            "weight": weight,
        }
        # `required` is accepted and dropped.  It is still a field of the result
        # contract, where it belongs to the audit stage -- a gate criterion that
        # fails the gate on its own -- but the behavioural scorer does not read it:
        # stage 2 asks every weighted module for every scored check, so there is no
        # subset for a flag to name.  Kept as a parameter so the call sites that pass
        # it still work, and not written out, because a flag in behavioural.json that
        # nothing honours is one a reader will take for a rule.
        _ = required
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

    Every mismatch here aborts rather than failing a check.  Grading a submission
    against expectations that changed after they were frozen produces a number
    that means nothing, and a zero is worse than an error because it looks like a
    verdict.

    Three separate things are verified, and they catch different accidents:

      * `freeze_format` and `schema_version` -- the file was written by a
        different version of this code than the one about to read it.
      * `case_digest` -- the case list reassembled here is not the one that was
        frozen.  This is the likely one: cases.py and expectations.json are
        shipped separately, and a case edited after the freeze would otherwise be
        graded against the wrong expectation.
      * `upstream_hashes` -- the .jsonnet files the upstream families feed to the
        programs still hash to what they hashed when frozen.  These come from the
        verifier's own read-only copy of State A, never from the submission, so
        this is a check on our assets and not on the tree under test.
    """

    def __init__(self, root: Path, log: Log) -> None:
        self.root = root
        self.log = log
        self.upstream_root = root / "upstream"
        self.inspector = root / "tools" / "AsmInspect.dll"

        path = root / "expectations.json"
        if not path.is_file():
            raise SystemExit(f"the frozen expectations are missing at {path}")
        t0 = time.time()
        self.frozen = vlib.read_json(path)
        log.write(f"expectations: {len(self.frozen.get('cases', {}))} case(s) "
                  f"loaded in {time.time() - t0:.1f}s")

        want_format = self.frozen.get("freeze_format")
        if want_format != "1.2":
            raise SystemExit(
                f"expectations.json declares freeze_format {want_format!r}; this "
                f"driver reads 1.2. The verifier image was assembled from "
                f"mismatched parts.")
        if self.frozen.get("schema_version") != spec.SCHEMA_VERSION:
            raise SystemExit(
                f"expectations were frozen against schema "
                f"{self.frozen.get('schema_version')!r}, the shipped spec.py is "
                f"{spec.SCHEMA_VERSION!r}")

        self.cases = casemod.assemble(self.frozen.get("upstream_files") or {})
        digest = casemod.digest(self.cases)
        if digest != self.frozen.get("case_digest"):
            raise SystemExit(
                f"the case list does not match the frozen one: reassembled "
                f"{digest}, expectations record {self.frozen.get('case_digest')}. "
                f"cases.py and expectations.json are out of step; refusing to "
                f"score.")
        if len(self.cases) != self.frozen.get("case_count"):
            raise SystemExit(
                f"case count {len(self.cases)} != frozen "
                f"{self.frozen.get('case_count')}")

        self._verify_upstream()
        self.by_family: dict[str, list] = {}
        for case in self.cases:
            self.by_family.setdefault(case.family, []).append(case)

    def _verify_upstream(self) -> None:
        """Our own copy of the graded .jsonnet inputs is byte-for-byte the frozen one.

        The executor materializes these from `upstream_root`, so if the copy in
        this image differed from the copy the freeze ran against, every upstream
        case would be graded against an expectation for a different program.
        """
        hashes = self.frozen.get("upstream_hashes") or {}
        if not hashes:
            raise SystemExit("expectations.json records no upstream_hashes")
        missing: list[str] = []
        changed: list[str] = []
        for rel, want in sorted(hashes.items()):
            path = self.upstream_root / rel
            if not path.is_file():
                missing.append(rel)
                continue
            if vlib.sha256_file(path) != want:
                changed.append(rel)
        if missing or changed:
            raise SystemExit(
                f"the verifier's copy of the upstream inputs does not match the "
                f"freeze: {len(missing)} missing, {len(changed)} changed. "
                f"First few: {(missing + changed)[:5]}")
        self.log.write(f"upstream inputs: {len(hashes)} file(s) verified "
                       f"against the freeze")

    def family(self, name: str) -> list:
        cases = self.by_family.get(name, [])
        if not cases:
            raise SystemExit(
                f"family {name!r} selected no cases; the module map no longer "
                f"matches the case list")
        return cases

    def expectation(self, cid: str) -> dict | None:
        return (self.frozen.get("cases") or {}).get(cid)


# --------------------------------------------------------------------------- #
# The shared build
# --------------------------------------------------------------------------- #


def run_build(assets: Assets, work: Path, submitted: Path, emit: Emitter,
              log: Log) -> int:
    """Publish the submission's two programs and record where they landed.

    This is the only module that costs minutes, and eighteen modules need its
    output, so it runs first and writes build.json into $SRB_SUITE_WORK.  Nothing
    else crosses the process boundary.

    Every check here is about whether a program came out, never about how it was
    written.  `discovery` is the one that needs saying twice: it passes when a
    project producing each named assembly exists, whatever that project is called
    and wherever it lives, because that is what the contract promises.
    """
    repo = work / "repo"
    emit.metadata["snapshot"] = buildmod.snapshot(submitted, repo, log)

    projects = buildmod.find_cli_projects(repo)
    emit.metadata["projects"] = {
        name: {"path": p.rel_path, "exe": p.is_exe}
        for name, p in sorted(projects.items())}
    log.write(f"discovery: {emit.metadata['projects']}")

    found = sorted(projects)
    emit.add("build/discovery",
             "pass" if len(found) == len(buildmod.CLI_NAMES) else "fail",
             f"a project producing each of {', '.join(buildmod.CLI_NAMES)} exists",
             required=True,
             detail="" if len(found) == len(buildmod.CLI_NAMES) else
             (f"found projects producing {found or 'nothing'}; missing "
              f"{sorted(set(buildmod.CLI_NAMES) - set(found))}. The assembly "
              f"name is what is looked for -- <AssemblyName>, or the .csproj "
              f"file name when the project does not set one -- so the project "
              f"may live anywhere and be called anything."),
             projects=found)

    state: dict[str, Any] = {"repo": str(repo), "bins": {}, "publish": {},
                            "ok": False}

    if len(found) != len(buildmod.CLI_NAMES):
        # No point running restore: the tree does not contain the two programs.
        emit.add("build/restore", "fail",
                 "not run: the two programs were not found in the submission")
        for name in buildmod.CLI_NAMES:
            emit.add(f"build/publish/{name}", "fail",
                     f"not run: no project produces {name}")
        _write_state(work, state)
        return 1

    restored = buildmod.restore(repo, projects, log)
    emit.add("build/restore", "pass" if restored.ok else "fail",
             "the projects restore from the offline package feed",
             detail="" if restored.ok else restored.tail(lines=40, limit=2400))
    if not restored.ok:
        for name in buildmod.CLI_NAMES:
            emit.add(f"build/publish/{name}", "fail",
                     f"not run: the restore failed, so {name} was not published")
        _write_state(work, state)
        return 1

    published: dict[str, buildmod.PublishOutcome] = {}
    for name in buildmod.CLI_NAMES:
        outcome = buildmod.publish(repo, projects[name],
                                   work / "publish" / name, log)
        published[name] = outcome
        emit.add(f"build/publish/{name}",
                 "pass" if outcome.ok else "fail",
                 f"{name} publishes to a runnable program",
                 detail=outcome.detail,
                 project=outcome.project)
        if outcome.ok:
            state["bins"][name] = outcome.launcher
            state["publish"][name] = outcome.publish_dir

    state["ok"] = all(o.ok for o in published.values())
    _write_state(work, state)

    # A program that publishes but cannot answer `--version` is not runnable, and
    # every case module after this would report the same failure a thousand times
    # over.  Asked here so it is reported once, as what it is.
    for name in buildmod.CLI_NAMES:
        if not published[name].ok:
            emit.add(f"build/runs/{name}", "fail",
                     f"not run: {name} did not publish")
            continue
        probe = vlib.run(state["bins"][name] + ["--version"], timeout=120.0,
                         log=log, label=f"{name} --version")
        # The *content* of --version is graded by the cli family against the
        # frozen reference output.  All that is asked here is that the program
        # starts and exits without a runtime failure.
        started = not probe.timed_out and probe.returncode >= 0
        emit.add(f"build/runs/{name}", "pass" if started else "fail",
                 f"the published {name} starts and exits on its own",
                 detail="" if started else probe.tail(lines=20, limit=1200))

    log.write(f"build: ok={state['ok']} bins={state['bins']}")
    return 0 if state["ok"] else 1


def _write_state(work: Path, state: dict) -> None:
    (work / BUILD_STATE).write_text(json.dumps(state, indent=2) + "\n",
                                    encoding="utf-8")


def read_build(work: Path) -> dict:
    path = work / BUILD_STATE
    if not path.is_file():
        raise SystemExit(
            f"{path} is missing: the build module has to run before this one. It "
            f"is declared first in suite.toml, so reaching here means the suite "
            f"ran out of order.")
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# The graded families
# --------------------------------------------------------------------------- #


def run_family(assets: Assets, work: Path, family: str, emit: Emitter,
               log: Log) -> int:
    """Run one family's cases against the submission and diff every channel."""
    state = read_build(work)
    selected = assets.family(family)
    emit.metadata["cases"] = len(selected)
    emit.metadata["family_weight"] = spec.FAMILY_WEIGHTS[family]

    if not state.get("ok"):
        # Every case is recorded as failed rather than skipped.  A submission that
        # produced no program has not passed these behaviours; it has prevented
        # them from being asked about, and a skip would drop them from the
        # denominator and score the module 1.0 over nothing.
        emit.notes.append("the submission did not publish, so every case in this "
                          "family is recorded as failed rather than skipped")
        for case in selected:
            emit.add(case.cid, "fail",
                     "not run: the submission produced no runnable program",
                     family=family)
        return 1

    bins = {name: list(launcher) for name, launcher in state["bins"].items()}
    log.write(f"{family}: {len(selected)} case(s) against {bins}")

    graded = {"pass": 0, "fail": 0, "skip": 0}
    t0 = time.time()
    # unprivileged=True: `bins` is the submission's own published program, and the
    # answers it is being measured against are readable on this filesystem at
    # /opt/assets/expectations.json.  Dropping the uid is what stops the program
    # under test from reading its own answer key.  freeze.py calls the same executor
    # without this, because at freeze time the binary is the C++ reference and there
    # is no key yet.
    for case, outcome in executor.run_all(selected, bins,
                                          upstream_root=str(assets.upstream_root),
                                          timeout=CASE_TIMEOUT,
                                          unprivileged=True):
        verdict, summary, detail = _grade_case(case, outcome, assets)
        graded[verdict] = graded.get(verdict, 0) + 1
        emit.add(case.cid, verdict, summary, detail=detail, family=family)
    emit.metadata["graded"] = graded
    emit.metadata["duration_sec"] = round(time.time() - t0, 1)
    log.write(f"{family}: {graded} in {time.time() - t0:.0f}s")
    return 0 if not graded["fail"] else 1


def _grade_case(case, outcome: executor.Outcome, assets: Assets
                ) -> tuple[str, str, str]:
    """One case against its frozen expectation, over all four channels.

    Returns (verdict, summary, detail).  The comparison is byte equality on
    everything a caller of the original can observe: the exit status, stdout,
    stderr, the files the run wrote or changed, and the files it removed.  A case
    passes only if all of them match -- a program that prints the right answer to
    the wrong stream has not reproduced the behaviour.
    """
    frozen = assets.expectation(case.cid)
    summary = case.note or f"{case.binary} {' '.join(case.argv)}"[:160]

    if frozen is None:
        # Cannot happen after the digest check, which proves the case list and the
        # expectations came from one freeze.  Kept as an error rather than a fail
        # because if it ever fires it is our defect, not the submission's.
        return "error", summary, (
            f"no expectation was frozen for {case.cid}, although the case digest "
            f"matched; this is a verifier defect")

    if frozen.get(EXCLUDED_KEY):
        return "skip", summary, (
            f"excluded at freeze time: {frozen[EXCLUDED_KEY]}. The reference did "
            f"not produce a behaviour here, so there is nothing to reproduce.")

    if frozen.get("key") != case.key():
        return "error", summary, (
            f"the case input changed since the freeze (key {case.key()} vs frozen "
            f"{frozen.get('key')}); this is a verifier defect")

    if outcome.timed_out:
        return "fail", summary, (
            f"the submission did not exit within {CASE_TIMEOUT:g}s; the reference "
            f"exited {frozen['rc']}")
    if outcome.aborted:
        return "fail", summary, (
            f"the submission was killed by a signal (rc={outcome.rc}); the "
            f"reference exited {frozen['rc']}. A crash is not a behaviour: the "
            f"reference never aborts on any graded input.")

    problems: list[str] = []
    diffs: list[str] = []

    if outcome.rc != frozen["rc"]:
        problems.append(f"exit status {outcome.rc}, reference {frozen['rc']}")

    for channel in ("stdout", "stderr"):
        want = _b64d(frozen.get(channel, ""))
        got = getattr(outcome, channel)
        if got != want:
            problems.append(
                f"{channel} differs ({len(got)} bytes, reference {len(want)})")
            diffs.append(f"--- {channel}\n"
                         + vlib.unified_diff(want, got, limit=20))

    want_files = {k: _b64d(v) for k, v in (frozen.get("created") or {}).items()}
    got_files = dict(outcome.created)
    for rel in sorted(set(want_files) | set(got_files)):
        if rel not in got_files:
            problems.append(f"did not write {rel}")
        elif rel not in want_files:
            problems.append(f"wrote {rel}, which the reference does not")
        elif got_files[rel] != want_files[rel]:
            problems.append(f"{rel} differs")
            diffs.append(f"--- {rel}\n"
                         + vlib.unified_diff(want_files[rel], got_files[rel],
                                             limit=20))

    want_removed = tuple(frozen.get("removed") or ())
    if outcome.removed != want_removed:
        extra = sorted(set(outcome.removed) - set(want_removed))
        kept = sorted(set(want_removed) - set(outcome.removed))
        if extra:
            problems.append(f"deleted its input: {', '.join(extra)}")
        if kept:
            problems.append(f"left in place: {', '.join(kept)}")

    if not problems:
        return "pass", summary, ""

    detail = "; ".join(problems[:6])
    if len(problems) > 6:
        detail += f" (+{len(problems) - 6} more)"
    if diffs:
        detail += "\n" + "\n".join(diffs[:2])
    return "fail", summary, detail[:4000]


def _b64d(text: str) -> bytes:
    import base64
    return base64.b64decode(text.encode("ascii")) if text else b""


# --------------------------------------------------------------------------- #
# Build-time replay of the reference against its own case list
# --------------------------------------------------------------------------- #


def _replay_reference(assets_root: Path, reference: Path, log: Log) -> int:
    """Grade the C++ reference against the expectations frozen from it.

    The claim this whole stage rests on is that a port which preserves jsonnet's
    behaviour takes full marks.  That claim is only as good as the case list: a
    case whose expectation the reference itself cannot reproduce is unpassable by
    anything, and it is indistinguishable in a report from a port that got it
    wrong.  Nothing in the ladder noticed such a case, because freeze.py records
    whatever the reference printed and grading only ever compares against the
    record.

    So the record is replayed against its own source.  Every family, every case,
    through `_grade_case` and `executor.run_all` -- the same functions a real
    grading run uses, not a second implementation of them -- and 100% is required.

    Two differences from freezing, both deliberate:

      * `unprivileged=True`, matching grading rather than freezing.  The uid drop
        is the *only* execution difference between the two, so it is the only
        place a case can be answerable when frozen and unanswerable when graded.
        Replaying under the freeze's own conditions would be the one arrangement
        that could not detect that.
      * the reference is reached through `build.json`'s `bins`, the same field a
        submission's published programs arrive in, so the replay enters the family
        path at exactly the point a submission does.

    Build-time only.  The grading image has no jsonnet binary and asserts it has
    none, so this runs in the `frozen` stage where the reference still exists.
    """
    bins: dict[str, list[str]] = {}
    for name in buildmod.CLI_NAMES:
        exe = reference / name
        if not exe.is_file():
            raise SystemExit(
                f"--replay-reference needs {exe}, which does not exist. This mode "
                f"only runs in the image's `frozen` stage, where the C++ build "
                f"output is still present.")
        bins[name] = [str(exe)]

    # The cases run as uid 65534 to match grading, so that uid needs to be able to
    # traverse to the binary and execute it.  Widening a path inside the build
    # stage costs nothing: the whole directory is dropped before the grading stage,
    # which starts from `toolchain` again and asserts no jsonnet exists.
    for name in buildmod.CLI_NAMES:
        os.chmod(reference / name, 0o755)
    walk = reference.resolve()
    while True:
        os.chmod(walk, (os.stat(walk).st_mode & 0o7777) | 0o011)
        if walk.parent == walk:
            break
        walk = walk.parent

    assets = Assets(assets_root, log)
    _check_total(assets.frozen)

    totals = {"pass": 0, "fail": 0, "skip": 0}
    failures: list[str] = []
    for family in FAMILY_MODULES:
        selected = assets.family(family)
        counts = {"pass": 0, "fail": 0, "skip": 0}
        t0 = time.time()
        for case, outcome in executor.run_all(selected, bins,
                                              upstream_root=str(assets.upstream_root),
                                              timeout=CASE_TIMEOUT,
                                              unprivileged=True):
            verdict, summary, detail = _grade_case(case, outcome, assets)
            counts[verdict] = counts.get(verdict, 0) + 1
            totals[verdict] = totals.get(verdict, 0) + 1
            if verdict == "fail" and len(failures) < 40:
                failures.append(f"{case.cid}: {summary} | {detail[:400]}")
        print(f"  {family:<14} w={spec.FAMILY_WEIGHTS[family]:<5} "
              f"pass={counts['pass']:<5} fail={counts['fail']:<4} "
              f"skip={counts['skip']:<4} in {time.time() - t0:.0f}s", flush=True)

    graded = totals["pass"] + totals["fail"]
    print(f"reference replay: {totals['pass']}/{graded} passed, "
          f"{totals['skip']} skipped")
    if totals["fail"]:
        for line in failures:
            print(f"  FAIL {line}")
        raise SystemExit(
            f"the C++ reference fails {totals['fail']} of its own {graded} frozen "
            f"case(s). Every expectation here was recorded from this same binary, "
            f"so a failure is a defect in the case -- an unstable output, a path "
            f"or timestamp in the bytes, a file the run leaves behind -- and not a "
            f"property of any port. Stage 2 would be unpassable by this much.")
    if totals["skip"]:
        # A skip here is an `excluded` expectation: the reference aborted or hung
        # at freeze time.  freeze.py already warns; this makes it fail the build,
        # because such a case is graded as a skip forever and quietly leaves the
        # denominator.
        raise SystemExit(
            f"{totals['skip']} case(s) were skipped, meaning their expectation "
            f"was excluded at freeze time because the reference aborted or timed "
            f"out. A clean freeze has none.")
    return 0


# --------------------------------------------------------------------------- #
# Build-time self-check
# --------------------------------------------------------------------------- #


def _self_check(assets_root: Path, suite_path: Path) -> int:
    """Prove the suite, the module map and the frozen expectations agree.

    All of this is also checked on the first module of a real grading run, which
    is exactly the problem: by then the build has cost forty minutes and six
    verification rounds have been budgeted, and the thing that went wrong is a
    family name typed two ways.  The check that matters most is the one that
    cannot fail loudly at grading time -- a family no module claims does not fail
    anything, it silently stops being graded.
    """
    frozen = vlib.read_json(assets_root / "expectations.json")
    _check_total(frozen)

    # The weights in suite.toml are read back and compared against
    # spec.FAMILY_WEIGHTS.  They are the same numbers written in two files, and
    # the whole reason FAMILY_WEIGHTS carries a paragraph of justification is that
    # the numbers are a judgment; a suite.toml that quietly disagreed would make
    # that justification describe something that is not what runs.
    raw = _load_suite(suite_path)
    declared = {m["id"]: m for m in raw.get("module", [])}

    missing = sorted(set(FAMILY_MODULES) - set(declared))
    if missing:
        raise SystemExit(f"suite.toml declares no module for: {missing}")
    if BUILD_MODULE not in declared:
        raise SystemExit(f"suite.toml declares no {BUILD_MODULE!r} module")
    extra = sorted(set(declared) - set(FAMILY_MODULES) - {BUILD_MODULE})
    if extra:
        raise SystemExit(
            f"suite.toml declares modules this driver cannot run: {extra}")

    for family in FAMILY_MODULES:
        want = spec.FAMILY_WEIGHTS[family]
        got = float(declared[family].get("weight", 0.0))
        if abs(got - want) > 1e-9:
            raise SystemExit(
                f"module {family!r} is weighted {got} in suite.toml but "
                f"{want} in spec.FAMILY_WEIGHTS")

    # `build` must stay at weight 0.00, and it is the only module allowed to be
    # worth nothing.  Two reasons, and they agree: this stage scores only what
    # State A can answer, and State A is C++ -- it has no .csproj for `dotnet
    # publish` to find, so charging for the publish means the reference cannot
    # pass a stage assembled from its own recorded behaviour; and it publishes the
    # programs all eighteen families run, so a tree that does not publish already
    # fails every one of them and a weight here would charge one failure twice.
    # The same argument is why no module reads the published assemblies' CLR
    # metadata: that question is asked by `no-native-interop`, a required stage-1
    # gate that fails the whole submission.  The reasoning is written out in
    # suite.toml; this is the part that cannot be edited away silently.
    got = float(declared[BUILD_MODULE].get("weight", 0.0))
    if abs(got) > 1e-9:
        raise SystemExit(
            f"the {BUILD_MODULE!r} module is weighted {got} in suite.toml and "
            f"must be 0.00: its subject is a property of State B that State A "
            f"cannot have, so scoring it means the C++ reference fails the stage "
            f"frozen from the C++ reference -- and a tree that does not publish "
            f"already fails all eighteen families. See its `about`.")

    # `required` is not a module key, and the runner refuses a suite that declares
    # one.  Setting it would single out one module in a stage that asks the same of
    # every weighted one -- the file claiming a distinction the stage does not
    # draw.  No strictness rides on the key, and that is measured rather than
    # argued: run_family records every case as fail (not skip) when build.json says
    # ok=false, so a tree that does not publish still rates 0.0000 through the
    # ordinary weighted path.
    still_required = sorted(m for m, d in declared.items() if d.get("required"))
    if still_required:
        raise SystemExit(
            f"these modules declare required = true: {still_required}. `required` is "
            f"not a module key -- the runner refuses a suite that declares one -- "
            f"and it would describe a distinction the stage does not draw. "
            f"Remove it.")

    # With `build` at 0.00 the eighteen families are the whole denominator, so
    # their sum being 1.00 is what makes each weight readable as
    # the share of the stage it is argued to be.  That is asserted by
    # spec._self_check ("FAMILY_WEIGHTS sums to 1.0"), which freeze.py runs
    # through check_or_die before this stage exists -- and FAMILY_MODULES is
    # tuple(spec.FAMILY_WEIGHTS), so a sum taken here is the same arithmetic on
    # the same numbers.  A copy of the assertion in this function could not fail
    # unless spec's had already failed the build, which is a check that looks like
    # coverage and provides none.  The value is computed for the line below;
    # the assertion belongs to spec.py and stays there.
    family_sum = sum(spec.FAMILY_WEIGHTS[f] for f in FAMILY_MODULES)

    order = [m["id"] for m in raw.get("module", [])]
    if order[0] != BUILD_MODULE:
        raise SystemExit(
            f"the {BUILD_MODULE!r} module must be declared first; every other "
            f"module reads the build.json it writes")

    counts = frozen.get("family_counts", {})
    total = sum(counts.values())
    print(f"module map ok: {len(declared)} modules "
          f"({len(FAMILY_MODULES)} families at w={family_sum:.2f} + build at "
          f"w=0.00), {total} graded cases, weights agree with "
          f"spec.FAMILY_WEIGHTS")
    for family in FAMILY_MODULES:
        print(f"  {family:<14} w={spec.FAMILY_WEIGHTS[family]:<5} "
              f"{counts.get(family, 0):>5} cases")
    return 0


def _load_suite(path: Path) -> dict:
    """suite.toml, through whichever TOML reader this Python has.

    tomllib arrived in 3.11 and the verifier image is on 3.11, but the host that
    runs the authoring self-tests is on 3.10.  Rather than depend on the newer
    one, fall back to the infra copy that exists for exactly this reason.
    """
    try:
        import tomllib
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except ModuleNotFoundError:
        pass
    try:
        from swerefactor import tomlcompat
    except ModuleNotFoundError as exc:  # pragma: no cover - image always has one
        raise SystemExit(
            f"no TOML reader available to check {path}: {exc}") from exc
    return tomlcompat.load(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--module")
    parser.add_argument("--self-check", action="store_true",
                        help="check the module map against the frozen "
                             "expectations and exit; used at image build time")
    parser.add_argument("--replay-reference", type=Path, metavar="DIR",
                        help="grade the C++ reference in DIR against the "
                             "expectations frozen from it and require 100%%; "
                             "used at image build time, in the `frozen` stage")
    parser.add_argument("--assets", type=Path,
                        default=Path(os.environ.get("SWEREFACTOR_ASSETS",
                                                    "/opt/assets")))
    parser.add_argument("--suite", type=Path,
                        default=Path(os.environ.get("SRB_SUITE_DIR",
                                                    str(HERE.parent)))
                        / "suite.toml")
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
        return _self_check(args.assets, args.suite)
    if args.replay_reference:
        args.log.parent.mkdir(parents=True, exist_ok=True)
        log = Log(args.log)
        try:
            return _replay_reference(args.assets, args.replay_reference, log)
        finally:
            log.close()
    if not args.module:
        parser.error("--module is required (or --self-check, or "
                     "--replay-reference)")

    module = args.module
    known = set(FAMILY_MODULES) | {BUILD_MODULE}
    if module not in known:
        parser.error(f"unknown module {module!r}; known: {', '.join(sorted(known))}")

    args.work.mkdir(parents=True, exist_ok=True)
    args.log.parent.mkdir(parents=True, exist_ok=True)
    log = Log(args.log)
    emit = Emitter(module)
    try:
        assets = Assets(args.assets, log)
        _check_total(assets.frozen)
        if module == BUILD_MODULE:
            rc = run_build(assets, args.work, args.repo, emit, log)
        else:
            rc = run_family(assets, args.work, module, emit, log)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        # A module that dies writes what it has.  The runner charges a module with
        # no checks its full weight, which is right -- but a partial result plus
        # the traceback is what makes it debuggable.
        import traceback
        emit.notes.append(f"{type(exc).__name__}: {exc}")
        emit.metadata["traceback"] = traceback.format_exc()[-4000:]
        emit.write(args.result)
        log.write(f"module {module} raised: {exc}")
        log.close()
        return 1
    emit.write(args.result)
    log.close()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
