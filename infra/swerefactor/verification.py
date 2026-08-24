"""Stage 3: pay the submission for surviving attack.

Six models, an hour each, both trees in front of them, one job -- write a
behavioural test the original repository passes and the submission fails.  Every
model that cannot do it is worth ten points.

Why this and not more of stage 2
--------------------------------
A fixed suite has a fixed ceiling.  Whatever it checks can be worked towards, and
a submission that clears it has demonstrated exactly that it clears it.  An
adversary reading the original has no such ceiling: it writes the test after
seeing what was built, so there is nothing to have aimed at.  Surviving twelve
independent attempts is a claim about the migration rather than about the suite.

Why adjudication is the whole difficulty
----------------------------------------
An adversary allowed to assert anything always wins.  Iteration order of a hash
set, a timestamp, the wording of an error message, a path separator, how fast
something runs -- none of these were ever promised, and a round that accepts them
scores every submission zero and distinguishes nothing.  So a break counts only
when four things hold:

  1. the candidate passes on the original -- otherwise it is a broken test, not a
     found defect;
  2. it fails on the submission;
  3. it does both again, ``reruns`` times, identically -- one run cannot tell a
     divergence from a flake, and a flake that costs ten points is our bug;
  4. it stays inside the public contract the task declared, which is a judgement
     and is made as one, by a model with the scope rules in front of it.

The first three are mechanical and run first, because they are cheap and they
throw out most of what arrives.

Why the adversary gets to run things
------------------------------------
An adversary with no feedback is guessing, and a guess that fails on both trees
says nothing about the submission.  So it can propose a candidate and see both
outcomes, as many times as its budget allows.  This is the same access the
benchmark's own authors had; the round is meant to be hard, not blind.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import agentloop, models
from .config import Adversary, Probe, ScopeRules
from .contract import REAP_GRACE_SEC, reap_group, submission_env
from .result import Check, StageResult, utcnow
from .tools import Toolbox, toolbox_for

DEFAULT_BUDGET_SEC = 3600.0
DEFAULT_CANDIDATE_TIMEOUT = 300.0

#: Exit codes from ``run-candidate.sh`` that mean the tree could not be tested, as
#: opposed to the candidate having something to report about it.  ``sysexits.h``'s
#: range, which is what the twenty scripts already use and agree on: 64 for a
#: harness contract that changed under them, 70 for the driver itself failing, 71
#: for "the tree does not compile" in fifteen of them, 72 for a build that produced
#: no usable entry point, 73 and 74 for a tree that would not be set up or started.
#:
#: A candidate's own verdict lives below this range -- 0 passed, 1 its assertions
#: failed -- and so do pytest's own troubles, 2 through 5, which are the adversary's
#: problem and not the tree's.  Nothing in the twenty tasks returns 64-78 to mean a
#: test failed, which is what makes reading the number safe; a task that needs a
#: different set says so in ``[scope].fault_exit_codes``.
FAULT_EXIT_CODES: tuple[int, ...] = tuple(range(64, 79))

#: What an interpreter says when it cannot start what it was given.  Matched on the
#: message rather than on the exit code alone because the shells disagree about the
#: code: bash answers 127 for a script it cannot open, dash answers 2.  Anchored to
#: a *non-zero exit with nothing on stdout* as well, so a test whose own output
#: happens to contain one of these phrases cannot be mistaken for a launch failure.
NEVER_STARTED_SIGNS = (
    "no such file",          # bash: run-candidate.sh: No such file or directory
    "cannot open",           # dash: 0: cannot open run-candidate.sh
    "not found",             # sh: 1: some-interpreter: not found
    "permission denied",
)


def _never_started(run: "Execution") -> bool:
    """Whether this run failed before the candidate command began executing."""
    if run.passed or run.timed_out or (run.stdout or "").strip():
        return False
    stderr = (run.stderr or "").lower()
    return any(sign in stderr for sign in NEVER_STARTED_SIGNS)

#: How a round ended.
SURVIVED = "survived"
BROKEN = "broken"
ROUND_ERROR = "error"

#: What adjudication decided about a submitted break.  ``UNDECIDED`` is not a
#: ruling: it says the adjudicator could not be reached or would not answer, so
#: nothing was learned about this break and the round has to be re-run.  It is
#: kept apart from ``REJECTED`` because both would otherwise be a survival, and a
#: survival is ten points -- paid, in that case, for an outage.
UPHELD = "upheld"
REJECTED = "rejected"
INVALID = "invalid"
UNDECIDED = "undecided"

@dataclass
class Execution:
    """One run of one candidate against one tree."""

    target: str            # "original" | "submission"
    exit_code: int
    passed: bool
    duration_sec: float
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    #: Whether anything was still running in the candidate's process group after it
    #: exited.  Recorded rather than merely swept, because a candidate that leaves a
    #: server behind is a candidate whose next run may be talking to the previous
    #: one -- and if that ever produces a break, this is the field that explains it.
    reaped: bool = False
    #: The infrastructure exit codes for this task, from ``[scope].fault_exit_codes``.
    #: Carried on the run rather than consulted at the far end, because whoever asks
    #: "was this a fault?" is several calls away from the probe that declared it.
    fault_codes: tuple[int, ...] = FAULT_EXIT_CODES

    @property
    def fault(self) -> bool:
        """Did the tree fail to be testable, rather than the candidate fail?

        A candidate reports its own verdict in exit 0 or 1, and pytest's own
        troubles in 2 through 5.  Everything the scripts use above that says the
        tree could not be built, booted or reached -- ``exit 71`` for "the tree does
        not compile" in fifteen tasks, 74 for "would not start", 64 for a harness
        contract that changed underneath it.  None of those is an observation about
        behaviour, and the whole of stage 3 is a comparison of behaviour.

        Derived rather than stored, so that it cannot disagree with the exit code it
        describes.  A timeout is deliberately not a fault: ``exit_code`` is -1 there
        and a candidate that hangs against one tree and not the other is a real
        asymmetry the adversary is entitled to.
        """
        return self.exit_code in self.fault_codes

    def to_dict(self) -> dict[str, Any]:
        out = {"target": self.target, "exit_code": self.exit_code,
               "passed": self.passed, "timed_out": self.timed_out,
               "duration_sec": round(self.duration_sec, 2),
               "reaped": self.reaped,
               "stdout": self.stdout[-2000:], "stderr": self.stderr[-2000:]}
        # Only when true.  A key that is absent on every ordinary run is one a
        # reader searching a transcript for faults can grep for.
        if self.fault:
            out["infrastructure_fault"] = True
        return out


@dataclass
class Candidate:
    """A test an adversary wrote, and how it behaved on each tree."""

    name: str
    filename: str
    code: str
    rationale: str = ""
    contract: str = ""
    runs: list[Execution] = field(default_factory=list)
    path: Path | None = None

    def latest(self, target: str) -> Execution | None:
        for run in reversed(self.runs):
            if run.target == target:
                return run
        return None

    @property
    def discriminates(self) -> bool:
        """Passes the original, fails the submission -- the shape of a break."""
        orig, sub = self.latest("original"), self.latest("submission")
        return bool(orig and sub and orig.passed and not sub.passed)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "filename": self.filename,
                "rationale": self.rationale[:2000], "contract": self.contract[:1000],
                "code": self.code[:20000],
                "runs": [r.to_dict() for r in self.runs]}


@dataclass
class Adjudication:
    """Whether a submitted break counts, and why."""

    decision: str          # upheld | rejected | invalid | undecided
    reason: str
    #: Filled by the mechanical stage, before any model is asked.
    reproduced: int = 0
    reruns_required: int = 0
    in_scope: bool | None = None
    scope_reason: str = ""
    #: Set when ``decision == UNDECIDED``: what went wrong reaching the
    #: adjudicator, in one sentence.  A reader of the result should not have to
    #: infer an outage from the wording of ``reason``.
    error: str = ""

    @property
    def undecided(self) -> bool:
        return self.decision == UNDECIDED

    def to_dict(self) -> dict[str, Any]:
        out = {"decision": self.decision, "reason": self.reason,
               "reproduced": self.reproduced,
               "reruns_required": self.reruns_required}
        if self.in_scope is not None:
            out["in_scope"] = self.in_scope
            out["scope_reason"] = self.scope_reason
        if self.error:
            out["error"] = self.error
        return out


@dataclass
class Round:
    """One adversary's attempt on this submission."""

    adversary: str
    model: str
    outcome: str
    reason: str = ""
    candidates: list[Candidate] = field(default_factory=list)
    claimed: str = ""
    adjudication: Adjudication | None = None
    elapsed_sec: float = 0.0
    usage: dict[str, Any] = field(default_factory=dict)
    transcript: str = ""
    loop_outcome: str = ""
    error: str = ""
    #: Whether the round's tool calls were getting through.  In the report because
    #: a round that could not read a file looks exactly like a round that read
    #: everything and found nothing, once both are reduced to an outcome.
    tool_calls: int = 0
    tool_errors: int = 0
    #: Turns re-sent after a retryable model failure.  A round is worth 10 points
    #: and its budget is wall-clock, so a round that spent minutes in backoff had
    #: less time to attack than its `elapsed_sec` suggests -- and a survival is
    #: paid for as a model that found nothing.
    resumes: int = 0

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "adversary": self.adversary, "model": self.model,
            "outcome": self.outcome, "reason": self.reason[:2000],
            "candidates_written": len(self.candidates),
            "tool_calls": self.tool_calls, "tool_errors": self.tool_errors,
            "resumes": self.resumes,
            "elapsed_sec": round(self.elapsed_sec, 1),
            "usage": self.usage, "transcript": self.transcript,
            "loop_outcome": self.loop_outcome,
        }
        if self.claimed:
            out["claimed"] = self.claimed
        if self.adjudication:
            out["adjudication"] = self.adjudication.to_dict()
        if self.error:
            out["error"] = self.error
        # Only the decisive candidate in full; the rest by name, or a round that
        # wrote twenty tests would bury the one that mattered.
        decisive = next((c for c in self.candidates if c.name == self.claimed), None)
        if decisive:
            out["candidate"] = decisive.to_dict()
        out["candidate_names"] = [c.name for c in self.candidates]
        return out


class CandidateRunner:
    """Executes a candidate test against a tree, via the task's own command.

    The command comes from ``probe.toml`` because "run this test" is cargo here,
    pytest there, ctest, mvn.  It is handed the candidate and the target through
    the environment and judged on its exit status: zero passed, anything else did
    not.

    The one thing it is not handed through the environment is which of the two
    trees this is.  That arrives as the command's last argument, so that a task
    script which forgets to scrub leaks nothing: a child inherits the environment
    by default and argv not at all.

    Both trees are copied before anything runs.  A candidate is code the adversary
    wrote, executed against the submission -- letting that touch the graded tree,
    or the frozen original, would put the outcome of stage 2 at the mercy of stage
    3.
    """

    def __init__(self, command: list[str], original: Path, submission: Path,
                 work: Path, *, timeout_sec: float = DEFAULT_CANDIDATE_TIMEOUT,
                 network_allowed: bool = False,
                 fault_codes: tuple[int, ...] = FAULT_EXIT_CODES) -> None:
        if not command:
            raise ValueError("probe.toml: candidate_command is empty, so no "
                             "candidate can be run")
        self.command = command
        self.timeout_sec = timeout_sec
        self.network_allowed = network_allowed
        self.fault_codes = fault_codes
        self.work = work
        self.work.mkdir(parents=True, exist_ok=True)
        # Which tree is which must not be legible to a candidate.  A candidate
        # that reads the role instead of the behaviour satisfies every mechanical
        # condition -- passes on the original, fails on the submission, and does
        # it identically three times -- while establishing nothing, and the only
        # thing standing between that and ten points is an adjudicator noticing.
        # So the role is not written down anywhere the candidate can reach: the
        # per-run salt makes the directory names unguessable even to a candidate
        # that hard-codes a token from a previous run's logs.
        self.salt = os.urandom(16).hex()
        self.tokens = {role: self._token(role)
                       for role in ("original", "submission")}
        # Two tiers, and the second is why the first exists.  `pristine` is copied
        # from the task's trees once and handed to nobody; `trees` is what a
        # candidate runs in and what the toolbox reads, and it is restored from
        # `pristine` between rounds.
        #
        # Before that it was one tier, staged once here, and `_stage_tree` returned
        # early if the directory already existed -- so all six rounds ran in the
        # same two directories and every write a candidate made was still there for
        # the next adversary.  The score depends on this: a round is upheld when a
        # candidate passes on the original and fails on the submission, so one
        # candidate deleting a source file from the submission tree leaves the
        # remaining five rounds discriminating against damage the first one did.
        # Each is a real, reproducible, in-scope-looking break, and five of them are
        # 50 points off a submission that was never measured. The reverse pays too:
        # a candidate that repairs whatever the round was probing makes later
        # rounds pass on both trees and find nothing.
        #
        # It is not only candidates -- `toolbox_for` in `build` is handed these same
        # paths, so a contaminated tree is also what the next adversary *reads*
        # while deciding what to attack.
        self.pristine = {
            "original": self._stage_tree(original, "original"),
            "submission": self._stage_tree(submission, "submission"),
        }
        self.trees = {role: self.work / "trees" / self.tokens[role]
                      for role in self.pristine}
        # Restored, not created: the working paths keep the token as their basename,
        # which is the only name for a tree a candidate is given and what several
        # tasks key their per-role build cache on (`STATE="$WORK/$TOKEN"`).
        self.reset()
        self.candidates_dir = self.work / "candidates"
        self.candidates_dir.mkdir(parents=True, exist_ok=True)
        self.runs = 0
        self._check_command_can_start()

    def _check_command_can_start(self) -> None:
        """Refuse a command that will not launch, before any round is spent on it.

        The command runs with ``cwd`` set to a staged copy of the tree, so a token
        naming a script relatively is looked for at the root of the repository under
        test.  lang07 shipped ``["bash", "run-candidate.sh"]``, which meant every
        candidate in every round came back "did not start" -- not as an error the
        harness noticed, but as a candidate that does not pass on the original, so
        no candidate could ever count and all six rounds paid their ten points.

        Refusing here rather than mid-round is what makes it visible: ``build``
        raises, ``cli`` writes a failed stage result, and somebody reads it.  The
        alternative -- noticing after the fact -- has to guess from an exit code,
        and the shells disagree about which one to use (bash 127, dash 2).

        A token is only checked when it looks like a path: something with a slash or
        a suffix.  ``bash``, ``pytest``, ``cargo`` are found on PATH and are not
        files here.
        """
        for token in self.command[1:]:
            if token.startswith("-"):
                continue
            # A token with whitespace or shell punctuation in it is a fragment for
            # an interpreter to parse, not a filename -- `sh -c 'pytest test.py'`
            # is one argument and no path.  Checking it as a path would refuse a
            # command that works.
            if any(ch in token for ch in " \t\n'\"$;|&<>()"):
                continue
            looks_like_path = "/" in token or ("." in Path(token).name)
            if not looks_like_path:
                continue
            if token.startswith("/"):
                if not Path(token).exists():
                    raise ValueError(
                        f"probe.toml: candidate_command names {token!r}, which "
                        f"does not exist, so no candidate can be run")
                continue
            missing = [role for role, tree in self.trees.items()
                       if not (tree / token).exists()]
            if missing:
                raise ValueError(
                    f"probe.toml: candidate_command names {token!r} relatively. "
                    f"The command runs with its working directory set to a copy of "
                    f"the tree under test, and there is no {token!r} there "
                    f"({', '.join(missing)}), so every candidate would come back "
                    f"as not having started -- which reads as a candidate that "
                    f"does not pass, and pays the submission all of its points "
                    f"untested. Give the absolute path.")

    def _token(self, role: str) -> str:
        return hashlib.sha256(f"{self.salt}:{role}".encode()).hexdigest()[:16]

    def _stage_tree(self, src: Path, name: str) -> Path:
        """The pristine copy, made once.  Nothing the submission controls runs here."""
        dst = self.work / "pristine" / self.tokens[name]
        if dst.exists():
            return dst
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dst, symlinks=True,
                        ignore=shutil.ignore_patterns(".git"))
        return dst

    def reset(self) -> None:
        """Restore both working trees from the pristine copies.

        Called once at construction and again before each round, which is the
        granularity that protects the score: a candidate cannot reach across into
        another adversary's round.  Within a round the trees are shared, and
        deliberately -- a candidate cannot learn which tree it is on from state it
        left in that tree, since each role has its own, so nothing there can
        manufacture a "passes on the original, fails on the submission" that was not
        already true.  What it can do is make its own rerun disagree with its first
        run, and an unstable candidate is rejected rather than upheld.

        Copies preserve mtimes, so a task's build cache under ``$WORK/$TOKEN`` --
        outside the tree, and deliberately shared across rounds -- stays valid
        across a reset.  The alternative, letting a build carry over inside the
        tree, is what allowed the contamination in the first place.

        Raises on failure rather than continuing: a round that silently ran in a
        tree the previous round had written to is the bug this method exists to
        prevent, and a dirty tree that reports itself clean is worse than a round
        recorded as an error.
        """
        for role, source in self.pristine.items():
            dst = self.trees[role]
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                shutil.rmtree(dst)
                if dst.exists():
                    raise OSError(
                        f"could not clear the staged {role} tree at {dst}; a round "
                        f"run here would be measuring whatever the last one left")
            shutil.copytree(source, dst, symlinks=True)

    def save(self, candidate: Candidate) -> Path:
        path = self.candidates_dir / candidate.filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(candidate.code, encoding="utf-8")
        candidate.path = path
        return path

    def execute(self, candidate: Candidate, target: str) -> Execution:
        if candidate.path is None:
            self.save(candidate)
        tree = self.trees[target]
        self.runs += 1
        # `submission_env`, not `dict(os.environ)`: a candidate is a program written
        # by an adversary and run against the submission, so it gets the same
        # treatment stage 2 gives a build.  The eight `CONTRACT_ENV` names are the
        # grading apparatus -- among them SRB_RESULT, the file a score is read out
        # of, and SRB_SUITE_DIR, the hidden tests -- and stage 3 inherits whatever
        # the process that launched it was holding.  Popping three names by hand
        # covered the two that name the role and missed the six that name the
        # machinery.  SRB_WORK and SRB_ORIGINAL are in that set and are re-set
        # below, which `extra` is for: it applies after the scrub and is not
        # filtered, so a value passed on purpose is one grep away from its reason.
        env = submission_env(extra={
            "SRB_CANDIDATE": str(candidate.path),
            "SRB_CANDIDATE_NAME": candidate.name,
            "SRB_TARGET": str(tree),
            "SRB_TARGET_TOKEN": self.tokens[target],
            "SRB_WORK": str(self.work / "run"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
            "TZ": "UTC",
            "LC_ALL": "C.UTF-8",
        })
        # Anything naming the role is removed, including whatever the process
        # that started the stage happened to be holding.  Stage 2 sets both of
        # these legitimately for its own differential; stage 3 has no use for
        # either, and a candidate comparing $SRB_TARGET against $SRB_ORIGINAL
        # learns the role in one line.
        for leaked in ("SRB_TARGET_NAME", "SRB_TARGET_ROLE", "SRB_ORIGINAL"):
            env.pop(leaked, None)
        if not self.network_allowed:
            # Not a sandbox, a statement of intent: a candidate that reaches the
            # network is not measuring the migration.
            env["SRB_NETWORK"] = "denied"
        (self.work / "run").mkdir(parents=True, exist_ok=True)
        # The task's script does need the role: on some tasks the two trees
        # genuinely cannot be installed the same way, and fw01 picks a wheelhouse
        # by it.  It travels as the script's last argument rather than in the
        # environment, because a child process inherits the environment
        # automatically and inherits argv never.  Forgetting to scrub therefore
        # leaks nothing; a task would have to pass it on deliberately.
        argv = list(self.command) + [target]
        start = time.monotonic()
        # Its own process group, so that what the candidate leaves running can be
        # found after it exits -- the same treatment `behavioural.py` gives a module,
        # and for a sharper reason here.  `subprocess.run(timeout=...)` kills the
        # command and nothing it started, and a candidate for these tasks routinely
        # starts a server: it binds a port, the script curls it, and if the script
        # exits without stopping it the server keeps the port.  The next candidate
        # binds nothing, curls the same port, and is answered by the *previous*
        # tree's server -- which passes on the original and fails on the submission
        # for a reason that has nothing to do with either.  Two rounds of that is
        # twenty points.
        try:
            proc = subprocess.Popen(
                argv, cwd=str(tree), env=env, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, start_new_session=True,
            )
        # Every outcome is recorded, including the two that are not the command
        # exiting.  They used to return without appending, which left a timed-out or
        # unstartable candidate showing no runs at all: the tool's own message said
        # "FAIL, timed out" while the round's report said the candidate had never
        # been run, and anything reasoning over ``runs`` -- ``latest``,
        # ``discriminates``, the gate on whether this round attacked anything --
        # could not see it.
        except OSError as exc:
            run = Execution(target=target, exit_code=-1, passed=False,
                            duration_sec=time.monotonic() - start,
                            stderr=f"cannot run candidate command: {exc}",
                            fault_codes=self.fault_codes)
            candidate.runs.append(run)
            return run

        timed_out = False
        swept = 0
        try:
            out, err = proc.communicate(timeout=self.timeout_sec)
        except subprocess.TimeoutExpired:
            timed_out = True
            # Reap first, then read: the pipes are held open by every process in the
            # group, so collecting output before signalling them waits for the whole
            # group to close its copy -- which is exactly what has not happened.
            # Whatever the candidate managed to print before it hung is the only
            # account of what it was doing, and it is what the adversary is told.
            swept = reap_group(proc.pid)
            try:
                out, err = proc.communicate(timeout=REAP_GRACE_SEC)
            except subprocess.TimeoutExpired:
                proc.kill()
                out, err = "", ""
        # Unconditional, and after the wait rather than only on the timeout path: a
        # candidate that exited 0 in half a second is as able to have left a server
        # behind as one that ran out of time.  `killpg` on a group with no members
        # left raises and reports zero, so the clean case costs one failed syscall.
        # Counted with the timeout path's sweep rather than instead of it -- that one
        # has already emptied the group by the time this runs, so reading only this
        # call would report the hung candidate as having left nothing behind.
        reaped = reap_group(proc.pid) > 0 or swept > 0
        if timed_out:
            run = Execution(target=target, exit_code=-1, passed=False,
                            duration_sec=time.monotonic() - start,
                            stdout=_text(out), stderr=_text(err),
                            timed_out=True, reaped=reaped,
                            fault_codes=self.fault_codes)
        else:
            run = Execution(target=target, exit_code=proc.returncode,
                            passed=proc.returncode == 0,
                            duration_sec=time.monotonic() - start,
                            stdout=out or "", stderr=err or "", reaped=reaped,
                            fault_codes=self.fault_codes)
        candidate.runs.append(run)
        return run

    def try_both(self, candidate: Candidate) -> tuple[Execution, Execution]:
        return (self.execute(candidate, "original"),
                self.execute(candidate, "submission"))

    def cleanup(self) -> None:
        shutil.rmtree(self.work / "trees", ignore_errors=True)
        shutil.rmtree(self.work / "pristine", ignore_errors=True)


def _text(raw: Any) -> str:
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    return str(raw)


class Adjudicator:
    """Decides whether a claimed break counts.

    Mechanical first: passes on the original, fails on the submission, and does
    both again ``reruns`` times.  That is cheap and it discards most of what
    arrives -- tests that never worked, tests that fail on both trees, tests whose
    outcome changes between runs.

    Then scope, which is not mechanical.  "This asserts on something the task
    promised" is a reading of the task, so it is read, by a model given the
    task's own allow and deny statements.  A candidate that survives both halves
    is a break, and the submission loses those ten points.
    """

    def __init__(self, runner: CandidateRunner, scope: ScopeRules,
                 driver: models.Driver | None = None,
                 log_dir: Path | None = None) -> None:
        self.runner = runner
        self.scope = scope
        self.driver = driver
        self.log_dir = log_dir

    def adjudicate(self, candidate: Candidate, claim: str) -> Adjudication:
        orig = candidate.latest("original")
        sub = candidate.latest("submission")
        if orig is None or sub is None:
            orig, sub = self.runner.try_both(candidate)

        # Before either verdict branch, because a tree that could not be tested has
        # not answered the question and both branches would answer it anyway.  On the
        # original side the old code reached the right decision by the wrong route,
        # calling a build failure "a broken test"; on the submission side it reached
        # the wrong one, reading `exit 71` as a divergence and charging ten points
        # six times over for a submission that does not compile -- which is stage
        # 2's finding and already scored there.  fw04's script says so in its own
        # comment, and works around it by sharing a module cache it would rather
        # isolate.
        #
        # The submission side is the half no per-tree script can fix: it sees one
        # tree, so failing both roles inverts the error into six survivals and pays
        # the full 30 for nothing.  Deciding it needs both runs, which is here.
        for run, side in ((orig, "original"), (sub, "submission")):
            if run.fault:
                return Adjudication(
                    decision=INVALID,
                    reason=(f"the {side} tree could not be tested: "
                            f"run-candidate.sh exited {run.exit_code}, which this "
                            f"task declares as an infrastructure fault rather than "
                            f"a verdict about behaviour. Nothing was learned about "
                            f"the submission from this candidate"),
                    reruns_required=self.scope.reruns,
                )

        if not orig.passed:
            return Adjudication(
                decision=INVALID,
                reason=("the candidate does not pass on the original repository "
                        f"(exit {orig.exit_code}"
                        + (", timed out" if orig.timed_out else "")
                        + "), so it is a broken test rather than a defect found"),
                reruns_required=self.scope.reruns,
            )
        if sub.passed:
            return Adjudication(
                decision=INVALID,
                reason="the candidate passes on the submission too",
                reruns_required=self.scope.reruns,
            )

        reproduced = 1
        if self.scope.require_deterministic and self.scope.reruns > 1:
            for _ in range(self.scope.reruns - 1):
                again_o = self.runner.execute(candidate, "original")
                again_s = self.runner.execute(candidate, "submission")
                # Same reason as above, and the opposite direction of error: an
                # unstable outcome is REJECTED, which is a survival worth ten
                # points, so a tree that stopped building midway through the reruns
                # would pay the submission for the fault instead of charging it.
                if again_o.fault or again_s.fault:
                    faulted = "original" if again_o.fault else "submission"
                    code = (again_o if again_o.fault else again_s).exit_code
                    return Adjudication(
                        decision=INVALID,
                        reason=(f"the break held {reproduced} time(s) of "
                                f"{self.scope.reruns} and then the {faulted} tree "
                                f"could not be tested (exit {code}), so the "
                                f"remaining reruns say nothing about whether it "
                                f"reproduces"),
                        reproduced=reproduced,
                        reruns_required=self.scope.reruns,
                    )
                if again_o.passed and not again_s.passed:
                    reproduced += 1
                else:
                    return Adjudication(
                        decision=REJECTED,
                        reason=(f"the outcome is not stable: it held {reproduced} "
                                f"time(s) of {self.scope.reruns} required "
                                f"(original exit {again_o.exit_code}, submission "
                                f"exit {again_s.exit_code}). An unstable test "
                                f"cannot distinguish a defect from a flake"),
                        reproduced=reproduced,
                        reruns_required=self.scope.reruns,
                    )

        in_scope, why = self.scope_check(candidate, claim)
        if in_scope is None:
            # The mechanical half held -- this break passes on the original and
            # fails here, reproducibly -- and the scope half was never answered.
            # Neither upholding nor rejecting is a finding: one would cost ten
            # points on half a review, the other would pay ten for an outage.
            return Adjudication(decision=UNDECIDED,
                                reason=f"no scope ruling was obtained: {why}",
                                reproduced=reproduced,
                                reruns_required=self.scope.reruns,
                                error=why)
        if not in_scope:
            return Adjudication(decision=REJECTED,
                                reason=f"out of scope: {why}",
                                reproduced=reproduced,
                                reruns_required=self.scope.reruns,
                                in_scope=False, scope_reason=why)
        return Adjudication(
            decision=UPHELD,
            reason=(f"passes on the original and fails on the submission, "
                    f"reproduced {reproduced}/{self.scope.reruns}, within scope"),
            reproduced=reproduced, reruns_required=self.scope.reruns,
            in_scope=True, scope_reason=why,
        )

    def scope_check(self, candidate: Candidate,
                    claim: str) -> tuple[bool | None, str]:
        """Is this asserting on something the task actually promised?

        ``None`` means no ruling was obtained, which is different from a ruling of
        "out of scope" and is not a verdict on the submission.  The line is
        whether the adjudicator was reachable: an endpoint that is down is a
        different run's problem and the round should be repeated, while a model
        that answers and declines to use the tool would decline again, so calling
        that undecided would ask for a re-run that cannot succeed.
        """
        if not self.scope.allow and not self.scope.deny:
            return True, "no scope rules declared, so nothing is out of scope"
        if self.driver is None:
            # No adjudicator model available.  Upholding on the mechanical half
            # alone would let a timing assertion cost ten points, so the break
            # is rejected and the reason says which half was missing.
            return False, ("no adjudicator model was available to judge scope, "
                           "and a break is not upheld on the mechanical checks "
                           "alone")
        spec = models.ToolSpec(
            name="rule",
            description="Record your scope ruling.",
            schema={
                "type": "object",
                "properties": {
                    "in_scope": {"type": "boolean"},
                    "reason": {"type": "string",
                               "description": "One or two sentences, naming the "
                                              "specific assertion you ruled on."},
                },
                "required": ["in_scope", "reason"],
            },
        )
        prompt = _scope_prompt(candidate, claim, self.scope)
        transcript = (str(self.log_dir / f"adjudication-{candidate.name}.jsonl")
                      if self.log_dir else None)
        empty = Toolbox(roots={})
        try:
            loop = agentloop.run(
                self.driver, system=ADJUDICATOR_SYSTEM, opening=prompt,
                # One question with a short written answer; the cap here is
                # deliberate, unlike the rounds, which have no turn limit.
                toolbox=empty, submit=spec, max_turns=4,
                # And no resumes, for the reason the turn cap is here: this is not
                # one of the hour-scale model runs the resume loop exists to
                # protect.  An outage here already has a designed outcome a few
                # lines below -- UNDECIDED, and the round is repeated -- and
                # resuming does not change it while the endpoint stays down, it
                # only arrives at it some minutes later, once per claimed
                # candidate.  The transient case is covered a layer down:
                # `models.Driver.complete` has already retried and backed off
                # before anything reaches here, which is what the comment under
                # `loop.harness_fault` means by "retries are already spent".
                max_resumes=0,
                transcript=transcript,
            )
        except models.ModelError as exc:
            return None, f"the scope ruling could not be obtained: {exc}"
        if loop.harness_fault:
            # Retries are already spent by this point: models.Driver.complete
            # backs off and retries the retryable statuses, and this is what is
            # left when that did not work.
            return None, (f"the adjudicator could not be reached: "
                          f"{loop.error or loop.outcome}")
        if not loop.ok:
            return False, (f"the adjudicator did not return a ruling "
                           f"({loop.outcome})")
        payload = loop.payload or {}
        return bool(payload.get("in_scope")), str(payload.get("reason") or "")


ADJUDICATOR_SYSTEM = """\
You are ruling on one question: does this test assert on behaviour the task
actually promised, or on an incidental property of one implementation?

A repository was migrated -- rewritten in another language, framework, platform or
build system. Someone has written a test that the original passes and the rewrite
fails, and wants it to count as a defect. Sometimes it is one. Sometimes it is a
difference that was never part of the contract and could not have been preserved
without reimplementing the original's internals, which is the opposite of what
was asked.

Rule in scope when the assertion is on something a user of this repository would
be entitled to rely on: documented behaviour, the public interface, output
content, exit statuses, the shape of the installed artifacts, compatibility the
task named.

Rule out of scope when the assertion is on something that merely happened to be
true of the original: iteration order where none was specified, exact wording of
diagnostics, timing or performance, memory addresses, internal file layout,
private symbols, the identity of an intermediate representation.

The task's own statements below take precedence over these general instructions.
Judge the assertion the test actually makes, not the intent described for it.
"""


def _scope_prompt(candidate: Candidate, claim: str, scope: ScopeRules) -> str:
    parts = [
        "The task declares this scope.",
        "",
        "IN SCOPE (behaviour the migration was required to preserve):",
    ]
    parts += [f"  - {a}" for a in scope.allow] or ["  (nothing stated)"]
    parts += ["", "OUT OF SCOPE (not promised; do not uphold a break on these):"]
    parts += [f"  - {d}" for d in scope.deny] or ["  (nothing stated)"]
    parts += [
        "",
        f"The test is named {candidate.name!r}.",
        "",
        "What its author says it establishes:",
        claim.strip() or "(nothing said)",
        "",
        "What its author says it asserts on, in terms of the public contract:",
        candidate.contract.strip() or "(nothing said)",
        "",
        "The test itself:",
        "```",
        candidate.code[:20000],
        "```",
    ]
    sub = candidate.latest("submission")
    if sub:
        parts += ["", "How it fails on the submission:", "```",
                  (sub.stdout[-1500:] + "\n" + sub.stderr[-1500:]).strip(), "```"]
    parts += ["", "Rule on the assertion the code makes. Call `rule` once."]
    return "\n".join(parts)


class RoundRunner:
    """One adversary's hour: propose, run, iterate, claim or concede."""

    def __init__(self, task: str, prompt: str, runner: CandidateRunner,
                 adjudicator: Adjudicator, toolbox: Toolbox,
                 scope: ScopeRules, *, log_dir: Path | None = None) -> None:
        self.task = task
        self.prompt = prompt
        self.runner = runner
        self.adjudicator = adjudicator
        self.toolbox = toolbox
        self.scope = scope
        self.log_dir = log_dir

    def run(self, adversary: Adversary, driver: models.Driver) -> Round:
        state: dict[str, Any] = {"candidates": [], "claim": None}
        rnd = Round(adversary=adversary.id, model=adversary.model,
                    outcome=ROUND_ERROR)

        # Both trees back to how the task shipped them, before this adversary reads
        # a line or runs a candidate.  Six rounds share two directories, so without
        # this the round begins in whatever the previous one left behind -- see
        # `CandidateRunner.reset`.
        #
        # Returned as a round error rather than raised, and this is the whole reason
        # the ladder can afford to be strict about it: an error here costs this
        # round's ten points and, since a stage with an errored round is no longer a
        # publishable total, asks for the run again.  Raising would take the other
        # five rounds with it. What must not happen is the third thing -- carrying
        # on in a tree that is not clean and paying out a number for it.
        try:
            self.runner.reset()
        except OSError as exc:
            rnd.error = str(exc)
            rnd.reason = (f"the round could not be given a clean tree to run in, so "
                          f"nothing it found would be about the submission: {exc}")
            return rnd

        propose = models.ToolSpec(
            name="try_test",
            description=(
                "Write a candidate test and run it against both repositories. "
                "You are told the outcome on each. Use this as often as you need: "
                "a candidate that fails on both trees is a broken test, and a "
                "candidate that passes on both has found nothing."),
            schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string",
                             "description": "Short identifier, e.g. "
                                            "'utf8-boundary'."},
                    "filename": {"type": "string",
                                 "description": "Filename for the test, with the "
                                                "extension the runner expects."},
                    "code": {"type": "string",
                             "description": "The complete test. It must exit "
                                            "non-zero to signal failure."},
                    "rationale": {"type": "string",
                                  "description": "What behaviour you expect to "
                                                 "diverge, and why."},
                },
                "required": ["name", "filename", "code"],
            },
        )
        submit = models.ToolSpec(
            name="report",
            description=(
                "End the round. Either claim a candidate as a genuine defect, or "
                "report that you found none. This is your last call either way: "
                "the round stops here and the rest of your budget goes unused, so "
                "do not call it while you still have something to try. A claim "
                "the review throws out counts as nothing found and costs you "
                "every minute you had left."),
            schema={
                "type": "object",
                "properties": {
                    "found": {"type": "boolean"},
                    "candidate": {"type": "string",
                                  "description": "Name of the candidate you "
                                                 "claim, if found is true."},
                    "claim": {"type": "string",
                              "description": "What the submission gets wrong, in "
                                             "terms of behaviour."},
                    "contract": {"type": "string",
                                 "description": "Which part of the task's public "
                                                "contract this test asserts on. "
                                                "A claim outside the declared "
                                                "scope will be rejected."},
                    "notes": {"type": "string",
                              "description": "If found is false: what you tried, "
                                             "and where you looked."},
                },
                "required": ["found"],
            },
        )

        def dispatch(name: str, args: dict[str, Any]) -> str:
            if name == "try_test":
                return self._try(state, args)
            return f"ERROR: no such tool {name!r}"

        def on_report(payload: dict[str, Any]) -> str | None:
            if not payload.get("found"):
                return None
            wanted = str(payload.get("candidate") or "")
            names = [c.name for c in state["candidates"]]
            if wanted not in names:
                return (f"There is no candidate named {wanted!r}. You have: "
                        f"{', '.join(names) or '(none)'}. Write it with try_test "
                        f"first, then report it.")
            cand = next(c for c in state["candidates"] if c.name == wanted)
            if not cand.discriminates:
                orig, sub = cand.latest("original"), cand.latest("submission")
                return (f"{wanted!r} does not currently pass on the original and "
                        f"fail on the submission (original: "
                        f"{'pass' if orig and orig.passed else 'fail'}, "
                        f"submission: "
                        f"{'pass' if sub and sub.passed else 'fail'}), so it "
                        f"cannot be upheld. Fix it or report that you found "
                        f"nothing.")
            return None

        transcript = (str(self.log_dir / f"round-{adversary.id}.jsonl")
                      if self.log_dir else None)
        opening = self._opening(adversary)
        # The toolbox is shared by every round in the stage, so its counters are
        # cumulative; only a delta describes this adversary.
        reads_before, calls_before = len(self.toolbox.reads), self.toolbox.calls
        loop = agentloop.run(
            driver, system=ADVERSARY_SYSTEM, opening=opening,
            toolbox=self.toolbox, submit=submit,
            budget_sec=adversary.budget_sec,
            transcript=transcript,
            extra_tools=[propose], extra_dispatch=dispatch,
            on_submit=on_report,
        )
        rnd.candidates = list(state["candidates"])
        rnd.elapsed_sec = loop.elapsed_sec
        rnd.usage = loop.usage
        rnd.transcript = loop.transcript
        rnd.loop_outcome = loop.outcome
        rnd.error = loop.error
        rnd.tool_calls = loop.tool_calls
        rnd.tool_errors = loop.tool_errors
        rnd.resumes = loop.resumes

        if loop.harness_fault:
            rnd.outcome = ROUND_ERROR
            rnd.reason = f"the round could not be completed: {loop.error}"
            return rnd

        payload = loop.payload or {}
        if not loop.ok:
            # Out of time, or walked away, with nothing claimed.  The submission
            # was attacked for the full budget and held, so this is a survival --
            # but only if it was actually attacked.  "Spent its budget" would carry
            # the premise that a round which spends it is probing something, and a
            # round whose every tool call failed spends the same budget and probes
            # nothing: paying it 10 points reports the harness's own breakage as a
            # property of the submission.  Measured: one graded round made 90 tool
            # calls, every one of them refused, ran zero candidates, and would have
            # been scored as a survival.
            unattacked = self._not_attacked(loop, state)
            if unattacked:
                rnd.outcome = ROUND_ERROR
                rnd.reason = (
                    f"the round could not attack the submission ({unattacked}), "
                    f"so it establishes nothing about it and pays no points; "
                    f"the round ended as {loop.outcome}")
                return rnd
            rnd.outcome = SURVIVED
            rnd.reason = {
                agentloop.TIME_LIMIT: (
                    f"the adversary used its full budget "
                    f"({adversary.budget_sec:g}s) without claiming a defect"),
                agentloop.ABANDONED: (
                    "the adversary stopped without claiming a defect"),
            }.get(loop.outcome, f"round ended as {loop.outcome}")
            rnd.reason += "; " + self._activity(reads_before, calls_before,
                                                len(state["candidates"]))
            return rnd

        if not payload.get("found"):
            # Reporting "no defect" is the same claim about the submission whether
            # the round ran out of turns or said so deliberately, so it needs the
            # same premise: that the round could reach the trees.  A round refused
            # by its own toolbox reports finding nothing because it looked at
            # nothing.
            unattacked = self._not_attacked(loop, state)
            if unattacked:
                rnd.outcome = ROUND_ERROR
                rnd.reason = (
                    f"the adversary reported finding no defect, but "
                    f"{unattacked}, so the report is not evidence about the "
                    f"submission and pays no points")
                return rnd
            rnd.outcome = SURVIVED
            rnd.reason = (str(payload.get("notes") or "")
                          or "the adversary reported finding no defect")
            rnd.reason += "; " + self._activity(reads_before, calls_before,
                                                len(state["candidates"]))
            return rnd

        name = str(payload.get("candidate") or "")
        cand = next((c for c in state["candidates"] if c.name == name), None)
        if cand is None:
            rnd.outcome = SURVIVED
            rnd.reason = f"claimed a candidate ({name!r}) that was never written"
            return rnd
        cand.contract = str(payload.get("contract") or "")
        rnd.claimed = name
        verdict = self.adjudicator.adjudicate(cand, str(payload.get("claim") or ""))
        rnd.adjudication = verdict
        if verdict.undecided:
            # Not a survival: the adversary made its case and the harness failed
            # to judge it.  ROUND_ERROR is the outcome that pays nothing and asks
            # for the round to be run again.
            rnd.outcome = ROUND_ERROR
            rnd.error = verdict.error
            rnd.reason = (f"claimed {name!r}, and the adjudication did not "
                          f"complete -- {verdict.reason}")
        elif verdict.decision == UPHELD:
            rnd.outcome = BROKEN
            rnd.reason = (str(payload.get("claim") or "")
                          or "a defect was found and upheld")
        else:
            rnd.outcome = SURVIVED
            rnd.reason = (f"claimed {name!r}, not upheld -- {verdict.reason}")
        return rnd

    #: Above this share of failed tool calls, the toolbox was refusing the round
    #: rather than the round making ordinary mistakes.  The graded failures that
    #: prompted this were at 100%, 72%, 61% and 56%.
    MAX_TOOL_ERROR_RATE = 0.5

    def _not_attacked(self, loop: agentloop.LoopResult,
                      state: dict[str, Any]) -> str:
        """Why this round could not probe, or "" if it could.

        This is about the harness failing the adversary, not about the adversary's
        judgement, its diligence or its luck.  One thing proves it: the toolbox
        refused most of what the round asked of it.  That is the shape every
        measured failure had -- the round kept calling and kept being told no.

        Two things are deliberately NOT conditions, because a round can reach
        either of them without having been failed by anything:

        *Having written no candidate.*  A round can reach its time limit with the
        first candidate still running, and a round can read both trees carefully
        and conclude from the source that the migration is sound.  A correct
        migration is supposed to produce exactly that report.

        *Having made no tool call at all.*  Two different runs land here.  One
        never got a turn -- the clock took it first -- and the budget outcome
        already describes that.  The other took turns and chose not to look, which
        is a real problem but a different one: it is the prompt or the model
        failing, not the toolbox, and treating it as a harness fault would ask for
        a re-run that has nothing to fix.  Both are visible in the report, where
        ``tool_calls: 0`` next to a survival is legible to a reader without
        silently zeroing a score.
        """
        if loop.tool_calls:
            rate = loop.tool_errors / loop.tool_calls
            if rate > self.MAX_TOOL_ERROR_RATE:
                return (f"{loop.tool_errors} of its {loop.tool_calls} tool calls "
                        f"failed ({rate:.0%}), so the toolbox was refusing it")
        runs = [run for cand in state["candidates"] for run in cand.runs]
        if runs and all(_never_started(run) for run in runs):
            # Every candidate the round wrote failed the same way, and that way is
            # the interpreter saying it could not start the command at all.  This is
            # not a tool error -- ``try_test`` answered every call, with the truthful
            # news that the candidate did not pass -- so the rate above sees a
            # healthy round.  Nor is it a candidate being wrong: a wrong candidate
            # is run and fails its assertions.
            #
            # ``CandidateRunner`` refuses this at construction now, so reaching here
            # means something the static check cannot see: a script that exists but
            # whose own interpreter does not, a toolchain missing from the image.
            # Both are the harness failing the round, and neither is a property of
            # the submission.
            codes = sorted({run.exit_code for run in runs})
            return (f"none of the {len(runs)} candidate run(s) started (exit "
                    f"{codes}), so nothing was ever executed against either tree")
        return ""

    def _activity(self, reads_before: int, calls_before: int,
                  candidates: int) -> str:
        """What this round actually did, stated as numbers including zero.

        A survival without a claim is scored as "the submission was attacked for
        the full budget and held", and the comment above that branch says the
        reason string is what keeps a thrashing adversary visible rather than
        hidden inside a score.  It did not achieve that: the only activity signal
        was `"; N candidate(s) tried"`, appended *only when N was non-zero*, so a
        round that read nothing and wrote nothing produced a reason identical to
        one whose clause someone had simply forgotten to add.  An absence cannot
        be audited by reading.

        Measured cost of that being invisible: in one graded stage 3 three
        adversaries could not read a single file -- their tool name collided at the
        gateway -- and each still paid `points_per_survived_model`, 90/90 and 21/21
        calls refused, zero tests run.  With the numbers printed, that round says
        "read 0 file(s), 90 tool call(s), wrote 0 candidate(s)" and the reader can
        see it never attacked anything.

        No score changes here, deliberately: the outcome stays SURVIVED and this
        only writes down evidence the round already had.  Whether a
        never-attacked round should pay is a grading-policy question, and not one
        to settle silently inside a reason string.
        """
        reads = len(self.toolbox.reads) - reads_before
        calls = self.toolbox.calls - calls_before
        return (f"read {reads} file(s), {calls} tool call(s), "
                f"wrote {candidates} candidate(s)")

    def _try(self, state: dict[str, Any], args: dict[str, Any]) -> str:
        name = str(args.get("name") or f"candidate-{len(state['candidates']) + 1}")
        code = str(args.get("code") or "")
        if not code.strip():
            return "ERROR: code was empty."
        filename = str(args.get("filename") or "").strip() or f"{name}.py"
        if "/" in filename or filename.startswith("."):
            return "ERROR: filename must be a bare name, no directories."
        cand = Candidate(name=name, filename=filename, code=code,
                         rationale=str(args.get("rationale") or ""))
        existing = next((c for c in state["candidates"] if c.name == name), None)
        if existing:
            state["candidates"].remove(existing)
        state["candidates"].append(cand)
        self.runner.save(cand)
        orig, sub = self.runner.try_both(cand)
        lines = [
            f"candidate {name!r} ({filename})",
            f"  on original:   {'PASS' if orig.passed else 'FAIL'} "
            f"(exit {orig.exit_code}{', timed out' if orig.timed_out else ''}, "
            f"{orig.duration_sec:.1f}s)",
            f"  on submission: {'PASS' if sub.passed else 'FAIL'} "
            f"(exit {sub.exit_code}{', timed out' if sub.timed_out else ''}, "
            f"{sub.duration_sec:.1f}s)",
        ]
        # The fault branch comes first, and it is the reason this is not just an
        # adjudication concern.  Before, a tree that would not build reported here as
        # `FAIL`, so a submission-side fault reached the adversary as "this
        # discriminates ... then report it" -- the harness inviting a report about a
        # build failure -- and an original-side one as "most often the test itself is
        # wrong", sending the adversary to debug a candidate that was fine.  Either
        # way it spends the rest of an hour's budget on it.
        #
        # Naming the role costs nothing here: these lines already say "on original"
        # and "on submission", because the adversary is told which tree is which.
        # The salt exists to keep that from the *candidate*, which never sees this.
        faulted = [label for run, label in ((orig, "original"), (sub, "submission"))
                   if run.fault]
        if faulted:
            lines.append(
                f"  -> the {' and '.join(faulted)} tree could not be tested "
                f"(infrastructure fault, not a verdict). This candidate cannot "
                f"count either way and reporting it would establish nothing. If it "
                f"is the submission's own build that fails, that belongs to the "
                f"behavioural stage and is already scored there.")
        elif orig.passed and not sub.passed:
            lines.append("  -> this discriminates. Check that it asserts on "
                         "promised behaviour, then report it.")
        elif not orig.passed:
            lines.append("  -> it does not pass on the original, so it cannot "
                         "count. Most often the test itself is wrong.")
        elif sub.passed:
            lines.append("  -> both pass; nothing found here.")
        for run, label in ((orig, "original"), (sub, "submission")):
            tail = ((run.stdout or "")[-1200:] + "\n"
                    + (run.stderr or "")[-1200:]).strip()
            if tail:
                lines += [f"  --- {label} output ---", _indent(tail)]
        return "\n".join(lines)

    def _opening(self, adversary: Adversary) -> str:
        from .audit import render_prompt
        text = render_prompt(self.prompt, task=self.task, gates=[],
                             toolbox=self.toolbox)
        scope = ["", "SCOPE -- a test outside this will not be counted:", ""]
        scope += ["IN SCOPE:"] + [f"  - {a}" for a in self.scope.allow or
                                 ["(nothing stated)"]]
        scope += ["", "OUT OF SCOPE:"] + [f"  - {d}" for d in self.scope.deny or
                                          ["(nothing stated)"]]
        # A round may be pointed at part of the surface.  Six unguided rounds
        # converge on the same few obvious endpoints; the assignment is what makes
        # six rounds cover a repository instead of repeating each other.  It
        # narrows where to look and changes nothing about what counts, so it goes
        # after the scope, which it cannot widen.
        focus = str((adversary.metadata or {}).get("focus") or "").strip()
        if focus:
            scope += [
                "",
                "WHERE TO SPEND THIS ROUND:",
                f"  {focus}",
                "  Other rounds are covering other parts of the surface. Start "
                "here. If you exhaust it, or become convinced there is nothing "
                "here, look wherever you think is weakest -- this is an "
                "assignment, not a restriction.",
            ]
        scope += [
            "",
            f"Your budget is {adversary.budget_sec:g} seconds. There is no limit on "
            f"turns or on candidates: the clock is the only thing you can run out "
            f"of, and `report` is the only other way this round can end.",
            f"A candidate must reproduce {self.scope.reruns} times to be upheld, "
            f"so a test whose outcome varies between runs will not count.",
            "",
            # Measured across 30 rounds: 55 calls passed a root's name as the whole
            # path and 6 spelled it 'original/x'.  The task prompts say "your tools
            # address these by name", which is what invites it.  Two wasted calls a
            # round is small, but the fix is one line and it belongs where the roots
            # are named.  Said as what the spellings mean rather than as what they
            # do: 'original/src' is refused only because it resolves under the
            # submission and is not there, and a submission is free to contain a
            # directory called 'original', which would serve it instead.
            "The colon is what names a root: `original:src/main.c`. Written without "
            "one -- `original`, or `original/src` -- it is not the original tree at "
            "all: a path with no root, and `.`, are read in the submission.",
        ]
        return text + "\n" + "\n".join(scope)


ADVERSARY_SYSTEM = """\
You are trying to find something a repository migration got wrong.

You have the original repository and a rewrite of it. Your job is to write a test
that the original passes and the rewrite fails -- evidence that the rewrite is
incomplete in a way its own test suite missed.

How to spend your time well:

Read the original first, and look for what is easy to leave out. Whole-repository
rewrites do not usually fail on the main path, which is what gets tested and
demonstrated; they fail at the edges. Error handling for malformed input. Options
that interact. Boundary values. Unicode. Empty and enormous inputs. Behaviour
under a configuration nobody uses. Features documented but rarely exercised. The
second entry point. Then check whether the rewrite handles them the same way.

Run your ideas. A test that fails on both trees is wrong about the original, and a
test that passes on both has found nothing -- in either case you learn that in one
call, and you have budget for many.

Assert on what was promised. A divergence you find in iteration order, in the
wording of an error message, in timing, or in an internal detail will be thrown
out on review, and you will have spent your budget on nothing. The scope you are
held to is stated in your instructions; work inside it.

Report once, at the end. `report` ends the round either way it is used, and the
budget you had left ends with it: a claim made halfway through spends the half you
had not used yet. Nothing is gained by claiming early -- a candidate that
discriminates goes on discriminating, and it is reviewed after the round whether
you claim it at minute ten or minute fifty. So keep working while you still have
ideas, and reach for `report` when you have a claim you believe is in scope, or
when the clock is nearly out.

Before you claim, read the scope again and find the line your test asserts on. A
test that discriminates for a reason the scope excludes is thrown out, and the
round is over at that point -- with whatever you had not tried yet still untried.
If two readings of your own result are open and one of them is an internal detail,
that is the reading review will take.

If you cannot find a real defect, say so. Reporting nothing found is the correct
answer when the rewrite is sound -- but arrive there by running out of ideas, not
by spending the round on one claim you were unsure of.
"""


def _indent(text: str, prefix: str = "      ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


class Stage:
    """Runs every round and turns the outcomes into checks the scorer reads.

    One check per adversary, ``pass`` for a survival.  The scorer pays for passes,
    so the arithmetic the user specified -- ten points per model that found
    nothing -- is the pooled weight of this stage and needs no special case.
    """

    def __init__(self, probe: Probe, runner: CandidateRunner,
                 adjudicator: Adjudicator, toolbox: Toolbox, prompt: str,
                 *, log_dir: Path | None = None) -> None:
        self.probe = probe
        self.rounds = RoundRunner(probe.task, prompt, runner, adjudicator,
                                  toolbox, probe.scope, log_dir=log_dir)
        self.runner = runner

    def run(self, driver_factory, *, started: str = "") -> StageResult:
        result = StageResult(stage="verification", task=self.probe.task,
                             status="ok", started_at=started or utcnow())
        outcomes: list[Round] = []
        for adversary in self.probe.adversaries:
            try:
                driver = driver_factory(adversary)
                rnd = self.rounds.run(adversary, driver)
            except models.ModelError as exc:
                rnd = Round(adversary=adversary.id, model=adversary.model,
                            outcome=ROUND_ERROR, error=str(exc),
                            reason=f"the round could not start: {exc}")
            outcomes.append(rnd)
            result.add(self._check(rnd))

        errored = [r for r in outcomes if r.outcome == ROUND_ERROR]
        if errored and len(errored) == len(outcomes):
            result.status = "error"
            result.note("no verification round completed")
        elif errored:
            result.note(
                f"{len(errored)} of {len(outcomes)} rounds did not complete "
                f"({', '.join(r.adversary for r in errored)}); they pay nothing "
                f"and are not counted as survivals"
            )
        broken = [r for r in outcomes if r.outcome == BROKEN]
        result.metadata.update({
            "rounds": [r.to_dict() for r in outcomes],
            "adversaries": len(outcomes),
            "survived": sum(1 for r in outcomes if r.outcome == SURVIVED),
            "broken": len(broken),
            "errored": len(errored),
            "candidate_runs": self.runner.runs,
            "scope": {"allow": self.probe.scope.allow,
                      "deny": self.probe.scope.deny,
                      "reruns": self.probe.scope.reruns},
        })
        return result

    def _check(self, rnd: Round) -> Check:
        verdict = {SURVIVED: "pass", BROKEN: "fail", ROUND_ERROR: "error"}[rnd.outcome]
        check = Check(
            id=rnd.adversary,
            verdict=verdict,
            summary=rnd.reason[:400],
            weight=1.0,
            unit=rnd.adversary,
            duration_sec=round(rnd.elapsed_sec, 1),
            metadata={"kind": "round", "model": rnd.model,
                      "candidates": len(rnd.candidates),
                      "loop_outcome": rnd.loop_outcome},
        )
        if rnd.claimed:
            check.metadata["claimed"] = rnd.claimed
        if rnd.adjudication:
            check.metadata["adjudication"] = rnd.adjudication.to_dict()
        if rnd.outcome == BROKEN:
            cand = next((c for c in rnd.candidates if c.name == rnd.claimed), None)
            if cand:
                sub = cand.latest("submission")
                check.detail = (
                    f"candidate {cand.name!r} ({cand.filename})\n"
                    f"{cand.rationale}\n\n"
                    f"failure on submission:\n"
                    + ((sub.stdout[-1500:] + "\n" + sub.stderr[-1500:]).strip()
                       if sub else "")
                )
                check.evidence = [{"path": f"candidates/{cand.filename}",
                                   "note": "the candidate that was upheld"}]
        elif rnd.error:
            check.detail = rnd.error
        return check


def build(probe: Probe, original: Path, submission: Path, work: Path, *,
          adjudicator_driver: models.Driver | None = None,
          log_dir: Path | None = None) -> Stage:
    """Assemble stage 3 from a task's probe configuration."""
    prompt_path = probe.root / probe.prompt
    try:
        prompt = prompt_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FileNotFoundError(
            f"{probe.task}: verification prompt not readable at {prompt_path}: {exc}"
        ) from exc
    meta = probe.metadata or {}
    runner = CandidateRunner(
        probe.candidate_command, original, submission, work,
        timeout_sec=float(meta.get("candidate_timeout_sec",
                                   DEFAULT_CANDIDATE_TIMEOUT)),
        network_allowed=probe.scope.network_allowed,
        # An empty list means "the default", not "nothing is a fault".  A task that
        # declared no codes and got an empty tuple would read every build failure as
        # a divergence again, which is the behaviour this replaced.
        fault_codes=(tuple(probe.scope.fault_exit_codes)
                     or FAULT_EXIT_CODES),
    )
    toolbox = toolbox_for(
        runner.trees["original"], runner.trees["submission"],
        allowed_commands=meta.get("allowed_commands") or (),
        run_timeout_sec=float(meta.get("run_timeout_sec", 300.0)),
    )
    adjudicator = Adjudicator(runner, probe.scope, adjudicator_driver,
                              log_dir=log_dir)
    return Stage(probe, runner, adjudicator, toolbox, prompt, log_dir=log_dir)
