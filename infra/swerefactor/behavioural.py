"""Run the behavioural modules declared in ``suite.toml`` and merge their output.

Each module is an independent process.  That is the whole design:

* a module that segfaults, hangs or writes garbage cannot stop the others from
  running, so one broken module still leaves the suite's other 17,000 checks
  measured and reported;
* a module can be written in whatever language its subject calls for — pytest for
  the HTTP corpus, a shell script for a CMake matrix, a Java program for jar
  forensics — as long as it writes the common JSON;
* a task author can run one module alone while writing it, which is the
  difference between a suite that gets extended and one that does not.

The contract with a module, in full:

    inputs (environment)
      SRB_REPO          the submission, rebuilt from source by the module
      SRB_ORIGINAL      State A, read-only
      SRB_MODULE_DIR    this module's own directory
      SRB_SUITE_DIR     tests/behavioural, for shared helpers in lib/
      SRB_WORK          a scratch directory that already exists
      SRB_SUITE_WORK    scratch shared by every module in this run
      SRB_RESULT        where to write the JSON
      SRB_MODULE_ID     the module's declared id
      SRB_MODULE_TIMEOUT_SEC  the budget, in seconds
      SRB_MODULE_DEADLINE     the same budget as an absolute unix time
      PYTHONPATH        already contains lib/ and the shared infra

``SRB_MODULE_TIMEOUT_SEC`` and ``SRB_MODULE_DEADLINE`` are the deadline this
runner will enforce, and a module that intends to survive it has to act on them: a
module writes its result once, at the end, so a SIGKILL at the deadline discards
every check it had already found.  A module doing work whose duration it cannot
predict should reserve a slice to write in and check the clock against the
deadline.  Both spellings because they are used for different things — the budget
to size sub-timeouts against, the absolute time to compare a clock to without
having to know when the module started.  Advisory: ignoring them is not an error,
it just means the module reports nothing if it overruns, and ``docs/SCHEMA.md``
explains why that is scored 0.0 rather than referred back for a re-run.

``SRB_SUITE_WORK`` exists for one case and should be used for no other: a build
that several modules need and that costs minutes to repeat.  The build module
runs first and publishes its output there; the modules that measure the build's
results read it.  Anything else passed between modules through that directory is
a hidden dependency on module order, which is exactly what the per-module
process boundary is for.

    output
      $SRB_RESULT holding either a full stage-result envelope or the bare
      {"checks": [...]} it reduces to.  Both are accepted: a small module should
      not have to restate the envelope it is a part of.

    exit code
      Advisory.  A module that exits non-zero having written a complete result is
      graded on the result — pytest exits 1 whenever a test fails, and that is a
      score, not a malfunction.  A module that writes nothing is an error however
      it exits.

What this stage does *not* provide is a privilege boundary between a module and
the code it grades.  A module runs as a subprocess of this runner, in the same
container, under the same uid; a submission's build runs as a subprocess of the
module.  ``docs/SCHEMA.md`` states the consequences in full.  Two things follow
for this file, and both are implemented below.

*A submission's build gets ``submission_env()``, not the module's own.*  The eight
names in ``CONTRACT_ENV`` are this runner's; every one of them is the location of
something the submission is being measured against.  A module needs them.  What
the module then hands to the program under test is a separate question, and
``submission_env`` is the answer to it.  This is not a boundary — a build that
goes looking can still find ``/tests/behavioural`` — but it is the difference
between finding it and being handed it.  Both live in ``swerefactor.contract``,
which a task's ``lib/`` can import without pulling in a TOML reader.

*A module's children are reaped when the module exits.*  Each module gets its own
process group and the group is signalled once the module is done.  The immediate
reason is not security: a server left running past its module holds a port, and
answers a request the next module believed it was making to something else.  It
also closes the gap between a module writing its result and this runner reading
it.
"""

from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .config import ModuleConfig, Suite
from .contract import CONTRACT_ENV, REAP_GRACE_SEC, reap_group, submission_env
from .result import Check, StageResult, Unit

#: Re-exported for callers that reach for them here, which is where the contract
#: is documented.  They live in ``contract`` because a task's ``lib/`` imports
#: them, and that module depends on nothing but the standard library.
__all__ = [
    "CONTRACT_ENV",
    "REAP_GRACE_SEC",
    "RESULT_NAME",
    "SuiteRunner",
    "reap_group",
    "submission_env",
]

#: The module's own result file, inside its scratch directory.
RESULT_NAME = "result.json"


class SuiteRunner:
    def __init__(
        self,
        suite: Suite,
        repo: Path,
        original: Path | None,
        work: Path,
        log,
        infra_dir: Path | None = None,
        checkpoint=None,
    ) -> None:
        self.suite = suite
        self.repo = Path(repo)
        self.original = Path(original) if original else None
        self.work = Path(work)
        self.log = log
        self.infra_dir = Path(infra_dir) if infra_dir else _infra_dir()
        self.result = StageResult(stage="behavioural", task=suite.task)
        # Called with the partial result after every module.  The runner does not
        # know where results live -- `cli.py` owns that path -- so the write is
        # passed in rather than reached for, and a caller with nowhere to put a
        # partial result (stage 1's scan reuses this class) simply passes nothing.
        self._checkpoint = checkpoint

    # -- environment ---------------------------------------------------------

    def _env_for(self, module: ModuleConfig, module_dir: Path, work: Path,
                 result_path: Path, *, deadline: float) -> dict[str, str]:
        env = dict(os.environ)
        env.update(self.suite.env)
        env.update(module.env)
        env.update({
            "SRB_REPO": str(self.repo),
            "SRB_MODULE_DIR": str(module_dir),
            "SRB_MODULE_ID": module.id,
            "SRB_SUITE_DIR": str(self.suite.root),
            "SRB_WORK": str(work),
            "SRB_SUITE_WORK": str(self.work / "shared"),
            "SRB_RESULT": str(result_path),
            # The deadline this runner will enforce, so the module can land inside it
            # rather than be killed at it.  Both spellings, because a module needs
            # them for different things: the budget to size its own sub-timeouts
            # against, and the absolute time to compare a clock to without having to
            # know when it was started.
            #
            # Without these, a module's only way to finish was to be lucky. The
            # runner's SIGKILL arrives with no warning, and a module that writes its
            # result once at the end -- which every module in this benchmark does --
            # loses every check it had already recorded. lang02's `streaming` spent
            # 2400s finding 8 stalled cases and published none of them, so the stage
            # reported "recorded nothing" about a module that had recorded 175
            # results, and the run was refused a score it had earned.  A module that
            # reads these can reserve a slice to write in, and its timeout becomes a
            # partial measurement with a stated cause instead of a silence.
            #
            # Advisory, not enforcement: the `proc.wait(timeout=...)` below is still
            # the limit, and a module that ignores these is killed exactly as before.
            "SRB_MODULE_TIMEOUT_SEC": f"{module.timeout_sec:g}",
            "SRB_MODULE_DEADLINE": f"{deadline:.3f}",
            # Determinism: two runs of one module on one submission must agree,
            # or a failure report cannot be acted on.
            "PYTHONHASHSEED": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
            "TZ": "UTC",
            "LC_ALL": env.get("LC_ALL", "C.UTF-8"),
        })
        if self.original:
            env["SRB_ORIGINAL"] = str(self.original)
        libdir = self.suite.root / self.suite.lib_dir
        parts = [str(p) for p in (libdir, self.infra_dir) if p and Path(p).is_dir()]
        if env.get("PYTHONPATH"):
            parts.append(env["PYTHONPATH"])
        env["PYTHONPATH"] = os.pathsep.join(parts)
        return env

    def _command_for(self, module: ModuleConfig, module_dir: Path) -> list[str]:
        if module.command:
            return list(module.command)
        runner = module_dir / "run.sh"
        if runner.is_file():
            return ["bash", str(runner)]
        runner = module_dir / "run.py"
        if runner.is_file():
            return [sys.executable, str(runner)]
        raise FileNotFoundError(
            f"module {module.id!r}: no command declared and neither run.sh nor "
            f"run.py exists in {module_dir}"
        )

    # -- one module ----------------------------------------------------------

    def run_module(self, module: ModuleConfig) -> None:
        module_dir = self.suite.root / module.dir
        work = self.work / module.id
        work.mkdir(parents=True, exist_ok=True)
        result_path = work / RESULT_NAME
        unit = Unit(id=module.id, title=module.title, weight=module.weight)
        if module.about:
            unit.metadata["about"] = module.about
        if module.metadata:
            unit.metadata.update(module.metadata)
        # In place of the seeded `unreached` row, not beside it.  `run()` seeds one row
        # per declared module so a killed stage still describes its whole suite, and
        # appending here left both rows in the table: `result.unit(id)` returns the
        # first match, so every reader that asks for a module by id -- the scorer, the
        # report, `_save`'s own counts -- got the placeholder and not the module that
        # had just run.  Replaced by index rather than by removing and appending, so
        # the table keeps the suite's declared order however late a module finishes.
        for i, existing in enumerate(self.result.units):
            if existing.id == module.id:
                self.result.units[i] = unit
                break
        else:
            self.result.units.append(unit)

        if not module_dir.is_dir():
            unit.status = "error"
            unit.summary = f"module directory {module.dir} does not exist"
            self.log(f"  [{module.id}] MISSING: {unit.summary}")
            return

        try:
            argv = self._command_for(module, module_dir)
        except FileNotFoundError as exc:
            unit.status = "error"
            unit.summary = str(exc)
            self.log(f"  [{module.id}] MISSING: {exc}")
            return

        # One clock reading for both the deadline the module is told and the wait
        # this runner enforces.  Two readings would differ by however long building
        # the environment takes, which is small, unmeasured, and on the wrong side:
        # a module told a later deadline than the runner's would write its result
        # after the SIGKILL that was the reason for telling it.
        started = time.time()
        env = self._env_for(module, module_dir, work, result_path,
                            deadline=started + module.timeout_sec)
        stdout_path = work / "stdout.log"
        self.log(f"  [{module.id}] {shlex.join(argv)}")
        timed_out = False
        rc: int | None = None
        # Its own process group, so that whatever the module leaves running can be
        # found after it exits.  See reap_group.
        try:
            with open(stdout_path, "wb") as out:
                proc = subprocess.Popen(
                    argv, cwd=str(module_dir), env=env, stdout=out,
                    stderr=subprocess.STDOUT, start_new_session=True,
                )
        except OSError as exc:
            unit.status = "error"
            unit.summary = f"could not start the module: {exc}"
            unit.duration_sec = time.time() - started
            self.log(f"  [{module.id}] ERROR: {unit.summary}")
            return
        try:
            try:
                proc.wait(timeout=module.timeout_sec)
                rc = proc.returncode
            except subprocess.TimeoutExpired:
                timed_out = True
        finally:
            # Before the result file is read, not after: a process still running
            # here is a process that can still write it.
            reaped = reap_group(proc.pid, log=self.log if timed_out else None)
            if reaped:
                unit.metadata["reaped_process_group"] = True
            try:
                proc.wait(timeout=REAP_GRACE_SEC)
            except subprocess.TimeoutExpired:
                pass
            if rc is None and not timed_out:
                rc = proc.returncode
        unit.duration_sec = time.time() - started
        unit.metadata["exit_code"] = rc
        unit.metadata["log"] = str(stdout_path)

        checks = self._collect(module, result_path, unit)

        if timed_out:
            unit.status = "timeout"
            unit.summary = (
                f"the module exceeded its {module.timeout_sec:g}s budget"
                + (f"; {len(checks)} check(s) were recorded before the deadline"
                   if checks else " and recorded nothing")
            )
            self.log(f"  [{module.id}] TIMEOUT after {module.timeout_sec:g}s")
        elif not checks:
            # Three ways to record nothing, and the reader's next move differs for
            # each.  A negative exit code is a signal: something outside this
            # process ended the module -- the OOM killer, a `kill`, the host going
            # away -- and that is a fact about the machine, not about the tree under
            # test.  Exit 0 with no checks is a module that believes it succeeded
            # and produced no evidence, which is an authoring bug in the module.
            # Anything else is the module's own crash.  All three stay `error`,
            # because a module that measured nothing must not be paid a rate; they
            # are told apart here because `grade_behavioural` is about to say the
            # module or its image is at fault rather than the submission, and the
            # next person needs to know which of the three it was.
            unit.status = "error"
            tail = _tail(stdout_path)
            if rc is not None and rc < 0:
                unit.metadata["signal"] = -rc
                name = _signame(-rc)
                cause = ("the module was killed by signal "
                         + (f"{-rc} ({name})" if name else str(-rc))
                         + " and wrote no checks")
            elif rc == 0:
                cause = "the module exited successfully and wrote no checks"
            else:
                cause = f"the module wrote no checks (exit {rc})"
            unit.summary = f"{cause}. Last output:\n{tail}"
            self.log(f"  [{module.id}] ERROR: {cause}")
        else:
            passed = sum(1 for c in checks if c.ok)
            self.log(f"  [{module.id}] {passed}/{len(checks)} passed "
                     f"in {unit.duration_sec:.1f}s (exit {rc})")

    def _collect(self, module: ModuleConfig, result_path: Path,
                 unit: Unit) -> list[Check]:
        """Read a module's result file, accepting either accepted shape."""
        if not result_path.is_file():
            return []
        try:
            with open(result_path, encoding="utf-8") as fh:
                raw: Any = json.load(fh)
        except Exception as exc:
            unit.metadata["result_error"] = f"{type(exc).__name__}: {exc}"
            return []
        if isinstance(raw, list):
            raw = {"checks": raw}
        if not isinstance(raw, dict):
            unit.metadata["result_error"] = "the result is not an object"
            return []

        for key in ("metadata", "notes"):
            value = raw.get(key)
            if value:
                unit.metadata[key] = value
        if raw.get("status") in ("error", "timeout"):
            unit.status = raw["status"]
            unit.summary = str(raw.get("summary") or raw.get("error") or "")[:1000]

        collected: list[Check] = []
        seen: set[str] = set()
        dropped = 0
        for entry in raw.get("checks") or []:
            if not isinstance(entry, dict):
                continue
            check = Check.from_dict(entry)
            # A weight-0 check does not enter the result at all.  Checks inside a
            # module carry no relative weight now -- the module's weight divides
            # equally among them -- so a row declared weight 0 would take a full
            # share of that weight, which is the opposite of what declaring it zero
            # meant.  Dropping it here rather than in the scorer is what makes it a
            # deletion instead of an exemption: it is absent from `behavioural.json`,
            # from the module's pass/fail counts, and from the report, so no reader
            # has to know a second rule to add the table up.
            #
            # Dropped at collection and not at emission on purpose.  These rows are
            # written from several dozen sites spread over most of the task drivers --
            # and that is only the ones a grep can see, since a weight also arrives
            # from a table or a loop variable -- many of them inside loops shared with
            # scoring checks, so editing each one risks changing a check that does
            # score.  The module still runs exactly as it did; only the row is gone.
            if check.weight <= 0:
                dropped += 1
                continue
            # `required` is cleared for the same reason and at the same place.  It
            # belongs to the audit stage, where a criterion can fail the gate on
            # its own; inside a behavioural module it meant "one failure here zeroes
            # the module", and nothing implements that now.  The kwarg is still
            # written from 18 driver files across 14 of the 20 tasks -- and a grep
            # undercounts, since the value also arrives from a variable -- so
            # clearing it here is what keeps it out of `behavioural.json`.  Left in, it
            # is a flag on a check that reads as a live rule to anyone who finds it,
            # including a future scorer.
            check.required = False
            # The module names its checks; the suite namespaces them, so two
            # modules may both have a `build` check without colliding in the
            # merged report.
            check.unit = module.id
            check.id = f"{module.id}/{check.id}"
            if check.id in seen:
                # A duplicate id would let a module inflate its own denominator.
                continue
            seen.add(check.id)
            collected.append(check)
        if dropped:
            # Said out loud, because "the module recorded 224 checks and the report
            # shows none" is otherwise indistinguishable from a module that failed to
            # write its result.
            unit.metadata["unscored_observations"] = dropped
        self.result.checks.extend(collected)
        return collected

    # -- the suite -----------------------------------------------------------

    def _save(self, started: float) -> None:
        """Hand the partial result to whoever knows where it goes.

        Best-effort on purpose.  This runs between modules on the way to a complete
        result, and the complete one is written again by the caller afterwards; a
        checkpoint that raised would turn a stage that was going to finish into one
        that died in the bookkeeping.  The metadata is refreshed on each pass so a
        checkpoint read off disk mid-stage describes itself rather than describing
        whatever the counts were when the first module ended.
        """
        if self._checkpoint is None:
            return
        self.result.duration_sec = time.time() - started
        self.result.metadata.update(self._counts())
        try:
            self._checkpoint(self.result)
        except Exception as exc:                      # noqa: BLE001
            self.log(f"  checkpoint failed ({type(exc).__name__}: {exc}); "
                     f"the stage continues")

    def _counts(self) -> dict[str, object]:
        return {
            "modules": len(self.suite.modules),
            "modules_ok": sum(1 for u in self.result.units if u.status == "ok"),
            "modules_unreached": sum(1 for u in self.result.units
                                     if u.status == "unreached"),
            "checks": len(self.result.checks),
            "checks_passed": sum(1 for c in self.result.checks if c.ok),
            "repo": str(self.repo),
        }

    def run(self) -> StageResult:
        started = time.time()
        self.work.mkdir(parents=True, exist_ok=True)
        (self.work / "shared").mkdir(parents=True, exist_ok=True)
        # Named for the stage the checks will be filed under, not for this class.
        # Stage 1's scan runs on this same runner (`scan.py` relabels the result
        # before calling in), and the line used to say "behavioural suite" in the
        # middle of a stage-1 log, directly above per-module "33/54 passed"
        # counts.  A reader looking for what stage 2 measured found a stage-1
        # number wearing stage 2's name.
        self.log(f"{self.result.stage} suite: {len(self.suite.modules)} "
                 f"module(s), repo={self.repo}")
        if not self.repo.is_dir():
            self.result.status = "error"
            self.result.metadata["error"] = f"no submission at {self.repo}"
            self.result.duration_sec = time.time() - started
            return self.result

        # Every declared module, before any of them runs.  The suite is killed from
        # outside at the stage timeout -- `ladder.run_stage` calls `docker kill` and
        # the container's PID 1 goes with it -- and the result was written once, after
        # this loop, so that kill discarded every module that had already finished.
        # lang04/max lost ten measured modules that way: build 4/4, structure 14/17,
        # provenance 4/6 and seven more that had each spent their own budget and
        # published a real number.  The stage reported `left no result` and the run was
        # refused a score it had largely earned.
        #
        # Seeded rather than appended as they finish, so the file is never a partial
        # suite wearing a complete suite's shape: at every instant it lists all N
        # modules, the finished ones carrying measurements and the rest saying the
        # stage clock had not reached them.  A denominator assembled from whoever
        # happened to finish is the one thing this must not produce -- a submission
        # that hangs module 2 would otherwise have modules 3..N vanish from the
        # arithmetic instead of scoring zero in it.
        for module in self.suite.modules:
            if self.result.unit(module.id) is None:
                self.result.units.append(Unit(
                    id=module.id, title=module.title, weight=module.weight,
                    status="unreached",
                    summary=("the stage ended before this module was reached; it "
                             "recorded nothing and keeps its weight"),
                ))
        self._save(started)

        for module in self.suite.modules:
            try:
                self.run_module(module)
            except Exception as exc:  # a runner bug must not lose the other modules
                unit = self.result.unit(module.id)
                if unit is None:
                    unit = Unit(id=module.id, title=module.title,
                                weight=module.weight)
                    self.result.units.append(unit)
                unit.status = "error"
                unit.summary = f"the runner raised {type(exc).__name__}: {exc}"
                self.log(f"  [{module.id}] RUNNER ERROR: {exc}")
            # After each module, so the next one's overrun cannot cost this one its
            # result.  Inside the loop and outside the `try`, because a module that
            # made the runner raise has still just changed the result and the change
            # is worth as much as a clean one.
            self._save(started)

        self.result.duration_sec = time.time() - started
        self.result.metadata.update(self._counts())
        return self.result


def _tail(path: Path, limit: int = 1500) -> str:
    try:
        data = path.read_bytes()[-limit:]
    except OSError:
        return "(no output captured)"
    return data.decode("utf-8", "replace")


def _signame(num: int) -> str:
    """``9`` -> ``"SIGKILL"``, and ``""`` for anything this platform does not name.

    Worth the two lines: SIGKILL on a module is nearly always the OOM killer, and
    a report that says "signal 9" makes the reader look that up before they can
    start on the actual question.
    """
    try:
        return signal.Signals(num).name
    except (ValueError, AttributeError):
        return ""


def _infra_dir() -> Path:
    """The directory holding the ``swerefactor`` package, for a module's PYTHONPATH."""
    return Path(__file__).resolve().parent.parent
