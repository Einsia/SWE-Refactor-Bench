"""Stage 3: what counts as a break.

Adjudication is the whole difficulty of this stage.  Accept too much and no
submission survives a round, so the sixty points measure nothing; accept too
little and the round is decoration.  These tests pin the four conditions --
passes on the original, fails on the submission, reproduces, in scope -- and the
cases where each of them is what saves the submission.

Everything here runs offline: the adversary and the adjudicator are scripted
drivers, and a candidate test is a shell script whose exit status depends on the
tree it was pointed at.
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from swerefactor import verification, agentloop, models
from swerefactor.verification import (BROKEN, INVALID, REJECTED, ROUND_ERROR,
                                   SURVIVED, UNDECIDED, UPHELD, Adjudicator,
                                   Candidate, CandidateRunner, RoundRunner, Stage)
from swerefactor.config import Adversary, Probe, ScopeRules
from swerefactor.contract import CONTRACT_ENV
from swerefactor.tools import toolbox_for

# How a task says "run this candidate against this tree".  Real tasks say cargo,
# pytest, ctest, mvn; here it is sh, which keeps the test hermetic and fast.
COMMAND = ["sh", "-c", 'sh "$SRB_CANDIDATE"']


@pytest.fixture
def trees(tmp_path: Path) -> tuple[Path, Path]:
    """Two trees differing in one promised behaviour and one incidental one."""
    original, submission = tmp_path / "orig", tmp_path / "sub"
    original.mkdir()
    submission.mkdir()
    (original / "tool").write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        '  --version) echo "tool 1.0";;\n'
        '  --json) echo \'{"a":1,"b":2}\';;\n'
        '  *) echo "usage: tool [--version|--json]";;\n'
        'esac\n')
    (submission / "tool").write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        '  --version) echo "tool 1.0";;\n'
        # Key order differs -- incidental, and the deny list says so.
        '  --json) echo \'{"b":2,"a":1}\';;\n'
        # The usage text is gone entirely -- a real defect.
        '  *) echo "ERROR: unknown option";;\n'
        'esac\n')
    for tree in (original, submission):
        (tree / "tool").chmod(0o755)
    return original, submission


@pytest.fixture
def runner(trees, tmp_path: Path) -> CandidateRunner:
    original, submission = trees
    return CandidateRunner(COMMAND, original, submission, tmp_path / "work",
                           timeout_sec=30.0)


HELP = '"$SRB_TARGET/tool" --help | grep -q usage\n'          # discriminates
KEYS = '"$SRB_TARGET/tool" --json | grep -q \'{"a":1\'\n'      # out of scope
BOTH = '"$SRB_TARGET/tool" --version | grep -q "1.0"\n'        # finds nothing
DEAD = 'exit 1\n'                                              # broken test

# A tree that could not be built.  Which tree it happens on cannot be asked
# directly -- the staged paths are salted precisely so a candidate cannot tell --
# so these decide the same way a real script would: by what the tree does.  The
# original prints usage, the submission does not, and the fault follows.
#
# 71 is EX_UNAVAILABLE, and it is the code fifteen of the twenty tasks return for
# "the tree does not compile".
SUB_FAULT = '''
if "$SRB_TARGET/tool" --help | grep -q usage; then exit 0; fi
exit 71
'''
ORIG_FAULT = '''
if "$SRB_TARGET/tool" --help | grep -q usage; then exit 71; fi
exit 0
'''


def candidate(name: str, code: str) -> Candidate:
    return Candidate(name=name, filename=f"{name}.sh", code=code)


def scope(**kw) -> ScopeRules:
    kw.setdefault("allow", ["documented command-line behaviour and its output"])
    kw.setdefault("deny", ["key ordering in JSON output", "timing"])
    kw.setdefault("reruns", 2)
    return ScopeRules.from_dict(kw)


def judge(in_scope: bool, reason: str = "ruled", times: int = 1) -> models.Driver:
    turn = {"tool_calls": [{"name": "rule", "arguments": {
        "in_scope": in_scope, "reason": reason}}]}
    return models.build("scripted", "judge", options={"turns": [turn] * times})


# --------------------------------------------------------------------------- #
# the mechanical half: three conditions that need no model
# --------------------------------------------------------------------------- #


def test_a_test_that_fails_on_the_original_is_invalid(runner):
    """The common case. A broken test is not a defect found."""
    verdict = Adjudicator(runner, scope(), judge(True)).adjudicate(
        candidate("broken", DEAD), "the tool is broken")
    assert verdict.decision == INVALID
    assert "does not pass on the original" in verdict.reason


def test_a_test_that_passes_on_both_is_invalid(runner):
    verdict = Adjudicator(runner, scope(), judge(True)).adjudicate(
        candidate("nothing", BOTH), "surely something")
    assert verdict.decision == INVALID
    assert "passes on the submission too" in verdict.reason


def test_a_real_defect_inside_scope_is_upheld(runner):
    verdict = Adjudicator(runner, scope(), judge(True)).adjudicate(
        candidate("help", HELP), "--help no longer prints usage")
    assert verdict.decision == UPHELD
    assert verdict.reproduced == 2
    assert verdict.in_scope is True


def test_an_unstable_test_is_rejected(runner, tmp_path):
    """A flake that costs the submission ten points is our bug, not theirs."""
    counter = tmp_path / "runs"
    counter.write_text("")
    cand = candidate("flaky", f'''
printf x >> "{counter}"
n=$(wc -c < "{counter}")
# Behave like a real discriminating test for the first pair of runs, then pass
# everywhere: the outcome changes between runs, which is what we want caught.
if [ "$n" -gt 3 ]; then exit 0; fi
''' + HELP)
    verdict = Adjudicator(runner, scope(reruns=3), judge(True)).adjudicate(
        cand, "sometimes it fails")
    assert verdict.decision == REJECTED
    assert "not stable" in verdict.reason
    assert verdict.reproduced < verdict.reruns_required


def test_reruns_can_be_switched_off_for_a_deterministic_task(runner):
    verdict = Adjudicator(runner, scope(require_deterministic=False),
                          judge(True)).adjudicate(candidate("help", HELP), "gone")
    assert verdict.decision == UPHELD
    assert verdict.reproduced == 1


def test_a_timeout_on_the_original_reads_as_not_passing(runner):
    runner.timeout_sec = 0.4
    verdict = Adjudicator(runner, scope(), judge(True)).adjudicate(
        candidate("slow", "sleep 30\n"), "it hangs")
    assert verdict.decision == INVALID
    assert "timed out" in verdict.reason


def test_a_run_that_timed_out_is_recorded_as_a_run(runner):
    """A run that did not end in an exit code is still a run that happened.

    These two outcomes -- the timeout, and the command not starting at all -- used
    to return without being appended to the candidate's runs.  The tool's reply to
    the model said "FAIL, timed out" while the round's report showed a candidate
    with nothing under it, and everything that reasons over ``runs`` was reasoning
    over an empty list.
    """
    runner.timeout_sec = 0.4
    cand = candidate("slow", "sleep 30\n")
    run = runner.execute(cand, "original")
    assert run.timed_out and not run.passed
    assert cand.runs == [run], "the timed-out run is missing from the candidate"
    assert cand.latest("original") is run
    assert cand.to_dict()["runs"][0]["timed_out"] is True


def test_a_command_that_cannot_start_is_recorded_as_a_run(runner, tmp_path):
    """The same, for the branch where the exec itself is refused."""
    missing = tmp_path / "not-an-interpreter.sh"
    missing.write_text("#!/usr/bin/no-such-interpreter\n")
    missing.chmod(0o755)
    runner.command = [str(missing)]
    cand = candidate("dead-on-arrival", BOTH)
    run = runner.execute(cand, "original")
    assert not run.passed and run.exit_code == -1
    assert "cannot run candidate command" in run.stderr
    assert cand.runs == [run]


# --------------------------------------------------------------------------- #
# a tree that could not be tested at all
# --------------------------------------------------------------------------- #
#
# "Fails on the submission" is two different observations sharing one exit status:
# the submission behaved differently, and the submission could not be built.  The
# second is stage 2's finding and is already scored there; read as the first it
# charges ten points, six rounds over, for a tree that does not compile.
#
# fw04 wrote the case down in its own script and worked around it by sharing a Go
# module cache it would rather have isolated -- a per-task workaround is all that is
# available to a script that sees one tree, because failing both roles inverts the
# error into six survivals and pays the whole sixty for nothing.  Only the
# adjudicator sees both runs, so only the adjudicator can call it neither.


def test_a_submission_that_cannot_be_built_is_not_a_defect_found(runner):
    """The case this exists for, and the expensive direction of the error.

    Up to 6 x 10 = 60 points, charged by the behavioural stage for something it
    does not own.  The candidate is honest here -- it really does exit 0 on one tree and
    non-zero on the other -- so nothing about its outcome distinguishes it from a
    real break except the number it exited with.
    """
    verdict = Adjudicator(runner, scope(), judge(True)).adjudicate(
        candidate("wont-build", SUB_FAULT), "the tool is gone")
    assert verdict.decision == INVALID, "a build failure became a finding"
    assert verdict.decision != UPHELD
    assert "submission tree could not be tested" in verdict.reason
    assert "71" in verdict.reason
    # Whoever reads this has to be told where the finding does belong, or the
    # obvious next step is to make stage 3 charge for it somewhere else.
    assert "rather than a verdict about behaviour" in verdict.reason


def test_an_original_that_cannot_be_built_is_a_fault_not_a_broken_test(runner):
    """Same decision as before, and a different sentence.

    This side was already INVALID, by the route that says the candidate "is a broken
    test rather than a defect found".  True of a candidate that asserts the wrong
    thing; false of one whose tree would not compile, and the adversary reading it
    spends the rest of an hour's budget rewriting a test that was fine.
    """
    verdict = Adjudicator(runner, scope(), judge(True)).adjudicate(
        candidate("orig-wont-build", ORIG_FAULT), "it is broken")
    assert verdict.decision == INVALID
    assert "original tree could not be tested" in verdict.reason
    assert "broken test" not in verdict.reason


def test_a_fault_during_the_reruns_is_not_read_as_a_flake(runner, tmp_path):
    """The other direction: an unstable outcome is REJECTED, which is a survival.

    So a tree that stops building partway through the reruns would pay the
    submission ten points for the fault, rather than charge it ten. Neither is
    right -- the candidate established nothing either way.
    """
    counter = tmp_path / "runs"
    counter.write_text("")
    cand = candidate("late-fault", f'''
printf x >> "{counter}"
n=$(wc -c < "{counter}")
# Discriminate for the first pair of runs the way a real break does, then start
# failing to build. 3 is the first run of the second pair: original, submission,
# original.
if [ "$n" -gt 2 ]; then exit 71; fi
''' + HELP)
    verdict = Adjudicator(runner, scope(reruns=3), judge(True)).adjudicate(
        cand, "sometimes it fails")
    assert verdict.decision == INVALID
    assert verdict.decision != REJECTED, "a fault was paid for as a flake"
    assert "not stable" not in verdict.reason
    assert "could not be tested" in verdict.reason
    # What did hold still gets said: a re-run needs to know the break was seen once.
    assert verdict.reproduced == 1
    assert "held 1 time(s) of 3" in verdict.reason


def test_a_timeout_is_not_an_infrastructure_fault(runner):
    """A candidate that hangs against one tree and not the other is an asymmetry
    the adversary is entitled to, so -1 is deliberately outside the fault range."""
    runner.timeout_sec = 0.4
    cand = candidate("slow", "sleep 30\n")
    run = runner.execute(cand, "original")
    assert run.timed_out and run.exit_code == -1
    assert run.fault is False
    assert "infrastructure_fault" not in run.to_dict()


@pytest.mark.parametrize("code", [1, 2, 3, 4, 5])
def test_the_candidates_own_verdict_is_never_a_fault(runner, code):
    """1 is its assertions failing; 2-5 are pytest's own troubles, which belong to
    whoever wrote the candidate and not to the tree it ran against."""
    run = runner.execute(candidate("exits", f"exit {code}\n"), "submission")
    assert run.fault is False, f"exit {code} read as an infrastructure fault"


def test_a_fault_is_marked_in_the_transcript_and_a_pass_is_not(runner):
    """``infrastructure_fault`` is written only when true, so that a reader
    searching a round's JSON for faults can search for the key itself."""
    ok = runner.execute(candidate("fine", BOTH), "original")
    assert ok.passed and "infrastructure_fault" not in ok.to_dict()
    bad = runner.execute(candidate("faulty", "exit 71\n"), "original")
    assert bad.to_dict()["infrastructure_fault"] is True
    assert bad.to_dict()["exit_code"] == 71


def test_a_task_can_declare_its_own_fault_vocabulary(trees, tmp_path):
    """The default is the twenty scripts' vocabulary, not a law about exit codes.

    A candidate command answering in another one says so in
    ``[scope].fault_exit_codes``, and then that list is the whole of it: 71 stops
    being a fault, because a task that lists 9 has said what its own runner means.
    """
    original, submission = trees
    mine = CandidateRunner(COMMAND, original, submission, tmp_path / "w9",
                           timeout_sec=30.0, fault_codes=(9,))
    assert mine.execute(candidate("nine", "exit 9\n"), "original").fault is True
    assert mine.execute(candidate("sev", "exit 71\n"), "original").fault is False

    theirs = CandidateRunner(COMMAND, original, submission, tmp_path / "wd",
                             timeout_sec=30.0)
    assert theirs.execute(candidate("nine", "exit 9\n"), "original").fault is False


def test_an_undeclared_vocabulary_means_the_default_not_nothing(trees, tmp_path):
    """``fault_exit_codes`` is absent from all twenty probe.toml files, so the
    empty list is the case that actually runs. Passed through as an empty tuple it
    would mean *nothing* is a fault, which is exactly the behaviour this replaced --
    every build failure a divergence again, and no test failing to say so."""
    original, submission = trees
    (tmp_path / "prompt.txt").write_text("attack {{task}}", encoding="utf-8")
    probe = Probe(task="t", scope=scope(), root=tmp_path, prompt="prompt.txt",
                  candidate_command=COMMAND,
                  adversaries=[Adversary(id="a1", model="m", driver="scripted",
                                         budget_sec=60.0)])
    assert probe.scope.fault_exit_codes == []
    stage = verification.build(probe, original, submission, tmp_path / "work")
    assert stage.runner.fault_codes == verification.FAULT_EXIT_CODES
    assert 71 in stage.runner.fault_codes


def test_the_fault_range_is_sysexits_and_excludes_the_verdicts(runner):
    """The range itself, stated once so that widening it downwards fails here.

    64-78 is sysexits.h. The tasks use 64, 65 and 70-74 within it; the point of the
    bound is the other end -- 0 and 1 are the candidate's answer, and a fault range
    that reached either would make every candidate undecidable and every round a
    survival worth ten points.
    """
    codes = verification.FAULT_EXIT_CODES
    assert min(codes) == 64 and max(codes) == 78
    assert not {0, 1, 2, 3, 4, 5} & set(codes)
    for used in (64, 65, 70, 71, 72, 73, 74):
        assert used in codes


# --------------------------------------------------------------------------- #
# the scope half: the reading that keeps the round winnable
# --------------------------------------------------------------------------- #


def test_an_out_of_scope_divergence_is_rejected(runner):
    """JSON key order really does differ here -- and was never promised."""
    verdict = Adjudicator(runner, scope(),
                          judge(False, "asserts on key ordering")).adjudicate(
        candidate("keys", KEYS), "the JSON keys come out in a different order")
    assert verdict.decision == REJECTED
    assert "out of scope" in verdict.reason
    assert verdict.in_scope is False
    assert "key ordering" in verdict.scope_reason


def test_without_an_adjudicator_a_break_is_not_upheld(runner):
    """The mechanical half alone would let a timing assertion cost ten points."""
    verdict = Adjudicator(runner, scope(), driver=None).adjudicate(
        candidate("help", HELP), "usage text gone")
    assert verdict.decision == REJECTED
    assert "no adjudicator model" in verdict.reason


def test_no_declared_scope_means_nothing_is_out_of_scope(runner):
    """A task that declares no contract gets no scope protection, by design."""
    rules = ScopeRules.from_dict({"reruns": 1})
    verdict = Adjudicator(runner, rules, driver=None).adjudicate(
        candidate("help", HELP), "usage text gone")
    assert verdict.decision == UPHELD
    assert "no scope rules declared" in verdict.scope_reason


def test_an_adjudicator_that_never_rules_does_not_uphold(runner):
    silent = models.build("scripted", "judge", options={"turns": [
        {"text": "I would rather not say."}, {"text": "Still thinking."}]})
    verdict = Adjudicator(runner, scope(), silent).adjudicate(
        candidate("help", HELP), "usage text gone")
    assert verdict.decision == REJECTED
    assert "did not return a ruling" in verdict.reason
    # A model that answers and declines the tool declines again on a re-run, so
    # this is a ruling of sorts.  Contrast the outage below.
    assert verdict.decision != UNDECIDED


class _Down(models.Driver):
    """An adjudicator endpoint that is down for every attempt, retries included."""

    name = "down"

    def __init__(self, retries: int = 0) -> None:
        super().__init__("down-model", retries=retries)
        self.attempts = 0

    def _once(self, system, messages, tools):
        self.attempts += 1
        raise models.ModelError("down: HTTP 503 from the adjudicator",
                                retryable=True, status=503)


def test_an_unreachable_adjudicator_is_undecided_not_a_rejection(runner):
    """The break held up mechanically and the scope half was never answered.

    Rejecting would be a survival, and a survival is ten points -- paid, in this
    case, for someone else's outage.
    """
    verdict = Adjudicator(runner, scope(), _Down()).adjudicate(
        candidate("help", HELP), "usage text gone")
    assert verdict.decision == UNDECIDED
    assert verdict.undecided
    assert verdict.in_scope is None          # not a scope finding either way
    assert "503" in verdict.error            # machine-readable, not only prose
    assert verdict.error in verdict.to_dict()["error"]
    assert verdict.reproduced == 2           # the mechanical half did run


def test_an_outage_and_a_real_out_of_scope_ruling_are_distinguishable(runner):
    """The bug this replaced: both were REJECTED, differing only in prose."""
    ruled = Adjudicator(runner, scope(), judge(False, "asserts on key ordering")
                        ).adjudicate(candidate("keys", KEYS), "key order differs")
    outage = Adjudicator(runner, scope(), _Down()).adjudicate(
        candidate("help", HELP), "usage text gone")
    assert (ruled.decision, ruled.in_scope) == (REJECTED, False)
    assert (outage.decision, outage.in_scope) == (UNDECIDED, None)
    assert "error" not in ruled.to_dict()
    assert "error" in outage.to_dict()


def test_the_retries_are_spent_before_an_outage_is_called_undecided(runner):
    """Undecided means the backoff already ran and did not help.

    Two retries rather than the default four: the point is that they happen, and
    the backoff is real time.
    """
    down = _Down(retries=2)
    verdict = Adjudicator(runner, scope(), down).adjudicate(
        candidate("help", HELP), "usage text gone")
    assert verdict.decision == UNDECIDED
    assert down.attempts == 3                # the first try plus both retries
    assert down.calls == 0                   # none of them returned a turn


def test_the_adjudicator_does_not_resume_a_down_endpoint(runner):
    """One request, and the ruling is undecided -- no loop-level resume here.

    `agentloop.run` re-sends a turn after a retryable failure, which is what keeps
    an hour of an adversary's investigation from being lost to a gateway.  The
    adjudicator is not that kind of run: it asks one short question, an outage has
    the designed outcome asserted above, and resuming does not change that outcome
    while the endpoint stays down -- it only reaches it some minutes later, once
    per claimed candidate.  So `scope_check` passes `max_resumes=0`.

    Asserted on the attempt count rather than on elapsed time, because the failure
    this pins arrived as a hung test suite rather than as a wrong answer: with the
    resume default in force this call sleeps through the loop's whole backoff
    ladder, and every assertion above still passes afterwards.
    """
    down = _Down()                           # retries=0: the driver layer is spent
    verdict = Adjudicator(runner, scope(), down).adjudicate(
        candidate("help", HELP), "usage text gone")
    assert verdict.decision == UNDECIDED
    assert down.attempts == 1


# --------------------------------------------------------------------------- #
# the trees a candidate runs against
# --------------------------------------------------------------------------- #


def test_candidates_run_against_copies_not_the_graded_tree(runner, trees):
    """Stage 3 must not be able to change what stage 2 measured."""
    original, submission = trees
    runner.execute(candidate("vandal", 'rm -f "$SRB_TARGET/tool"\n'), "submission")
    assert (submission / "tool").exists()
    assert runner.trees["submission"] != submission
    assert not (runner.trees["submission"] / "tool").exists()  # the copy took it


def test_the_original_copy_is_staged_and_readable(runner):
    assert (runner.trees["original"] / "tool").exists()


def test_a_candidates_damage_does_not_survive_into_the_next_round(runner, trees):
    """Six rounds share two directories, so the copy is not enough on its own.

    The test above establishes that stage 3 cannot reach the graded tree.  What it
    does not establish is that a round cannot reach the *next round's* tree: copies
    made once, in the constructor, would leave each of the six rounds running in
    whatever the round before it left behind.
    """
    runner.execute(candidate("vandal", 'rm -f "$SRB_TARGET/tool"\n'), "submission")
    assert not (runner.trees["submission"] / "tool").exists()

    runner.reset()

    assert (runner.trees["submission"] / "tool").exists(), \
        "the next round would begin in the wreckage of this one"
    # And restored from a copy the submission never ran in, so a vandal that
    # rewrote a file rather than deleting it is undone too.
    assert (runner.trees["submission"] / "tool").read_text() == \
        (trees[1] / "tool").read_text()


def test_a_vandalised_tree_cannot_hand_the_next_round_a_break(runner):
    """The mechanical half of a break is satisfiable by damage, not just by defects.

    ``BOTH`` finds nothing on a clean pair -- it checks ``--version``, which the two
    trees answer identically.  Run after a previous round deleted the tool from the
    submission copy, it passes on the original and fails on the submission, twice,
    for a reason no adjudicator has any way to see: the claim is truthful about the
    tree it was run in.  Ten points, and then ten more for the next round, off a
    submission that was never measured.

    Asserted through a whole round rather than on ``reset`` directly, because the
    property is that the round does the restoring -- an adversary does not get to
    choose whether its tree was clean.
    """
    runner.execute(candidate("vandal", 'rm -f "$SRB_TARGET/tool"\n'), "submission")
    assert not (runner.trees["submission"] / "tool").exists()

    rnd = play(runner, [
        try_test("version", BOTH, rationale="does --version still work"),
        report(found=True, candidate="version",
               claim="the tool no longer answers --version"),
        report(found=False, notes="it turns out to pass on both"),
    ])

    # The claim does not even reach the adjudicator: in a clean tree ``BOTH`` passes
    # on the submission too, so the report tool refuses it and the round is sent back
    # to look for something real.  In the dirty tree it discriminated, twice, and
    # would have gone to a judge with a truthful claim in front of it.
    assert rnd.outcome == SURVIVED, rnd.reason
    assert rnd.adjudication is None
    assert (runner.trees["submission"] / "tool").exists()
    version = next(c for c in rnd.candidates if c.name == "version")
    assert not version.discriminates
    assert version.latest("submission").passed


def test_a_round_that_cannot_be_given_a_clean_tree_is_an_error_not_a_survival(
        runner, monkeypatch):
    """Fail closed: the one outcome that must not happen is carrying on dirty.

    A round recorded as an error costs its ten points and, under the scorer's
    "status is not verdict" rule, makes the stage unpublishable -- so the run is
    asked for again.  Paying the submission for a round that ran in a tree the
    harness could not vouch for is the alternative this rules out.
    """
    def refuse(*a, **kw):
        raise OSError("Device or resource busy")

    monkeypatch.setattr(verification.shutil, "rmtree", refuse)
    rnd = play(runner, [report(found=False, notes="never got to look")])

    assert rnd.outcome == ROUND_ERROR
    assert "Device or resource busy" in rnd.error
    assert "clean tree" in rnd.reason
    assert rnd.candidates == [], "a round that never started ran a candidate"


def test_a_reset_keeps_the_tree_path_and_the_mtimes(runner):
    """Two things several tasks depend on, which a naive re-stage would break.

    The working path keeps the role token as its basename, because that token is
    also ``$SRB_TARGET_TOKEN`` and at least eight tasks build into
    ``STATE="$WORK/$TOKEN"``; a fresh directory per round would cold-start every one
    of those with the network denied.  And the copy preserves mtimes, so a build
    cached outside the tree -- shared across rounds on purpose -- is not invalidated
    by the restore of the sources it was built from.
    """
    before = {role: (tree, (tree / "tool").stat().st_mtime_ns)
              for role, tree in runner.trees.items()}
    runner.reset()
    for role, (path, mtime) in before.items():
        assert runner.trees[role] == path
        assert path.name == runner.tokens[role]
        assert (path / "tool").stat().st_mtime_ns == mtime, \
            f"the {role} tree came back with fresh mtimes; every task's cache is cold"


# --------------------------------------------------------------------------- #
# what a candidate is not allowed to be able to know
# --------------------------------------------------------------------------- #
# A candidate that reads which tree it is on satisfies every mechanical condition
# for a break -- it passes on the original, fails on the submission, and
# reproduces exactly -- while establishing nothing whatsoever about the
# migration.  The only thing standing between that and ten points would be an
# adjudicator noticing, so the ability is removed rather than policed.


#: Exits immediately, having left something behind that will act later.  This is
#: what a candidate that starts a server and forgets to stop it looks like from
#: outside: the script is done, the port is not free, and the file this one writes
#: stands in for the next candidate's request being answered by it.
def _leaks_a_process(marker: Path) -> str:
    return (f'(sleep 1; : > "{marker}") >/dev/null 2>&1 &\n'
            f'exit 0\n')


def test_what_a_candidate_leaves_running_is_swept_when_it_exits(runner, tmp_path):
    """The candidate exited in milliseconds; the thing it started had a second to go.

    ``subprocess.run(timeout=...)`` kills the command and nothing the command
    started, and these tasks' candidates routinely start a server: it binds a port,
    the script curls it, and if the script exits without stopping it the port stays
    held.  The next candidate binds nothing, curls the same port, and is answered by
    the *previous* tree's server -- so it passes on the original and fails on the
    submission for a reason that is about neither.  Two rounds of that is twenty
    points.

    The marker file is the leftover acting after its candidate is gone, which is the
    property; a pid check would pass against a zombie.
    """
    marker = tmp_path / "the-leftover-was-still-alive"
    run = runner.execute(candidate("leaky", _leaks_a_process(marker)), "original")

    assert run.passed and not run.timed_out
    assert run.reaped is True, "nothing was swept, so nothing was noticed"
    time.sleep(1.5)
    assert not marker.exists(), "the leftover outlived the candidate that started it"


def test_a_candidate_that_leaves_nothing_behind_is_not_reported_as_reaped(runner):
    """The negative half: `reaped` has to mean something to be worth publishing.

    It is on the record for one reason -- a break whose candidate left a process
    running is a break to distrust -- and a flag that is true for every run says
    nothing about any of them.
    """
    run = runner.execute(candidate("clean", BOTH), "original")
    assert run.passed
    assert run.reaped is False


def test_a_timed_out_candidate_still_yields_its_output(runner, tmp_path):
    """A leftover holding the pipes must not cost the harness its diagnosis.

    Every process in the group inherits the pipe, so reading before signalling waits
    for a group that is precisely what has not finished.  The output written before
    the hang is the only account of what the candidate was doing, and it is what
    reaches the adversary as the tool's reply.
    """
    runner.timeout_sec = 0.5
    run = runner.execute(candidate("hangs", 'echo "got this far"\nsleep 30\n'),
                         "original")
    assert run.timed_out and not run.passed
    assert "got this far" in run.stdout
    assert run.reaped is True


DISCRIMINATE_ENV = (
    'for v in SRB_TARGET_NAME SRB_TARGET_ROLE SRB_ORIGINAL; do\n'
    '  eval "seen=\\${$v-}"\n'
    '  if [ -n "$seen" ]; then echo "LEAKED $v=$seen"; exit 17; fi\n'
    'done\n'
    'exit 0\n'
)


def test_the_role_is_not_in_the_candidates_environment(runner):
    """`assert os.environ["SRB_TARGET_NAME"] == "original"` must not work."""
    for target in ("original", "submission"):
        run = runner.execute(candidate("probe-env", DISCRIMINATE_ENV), target)
        assert run.passed, f"a discriminator leaked on {target}: {run.stdout}"


def test_the_role_is_scrubbed_even_when_the_stage_inherited_it(runner, monkeypatch):
    """Stage 2 sets both of these for its own differential; stage 3 must not
    pass them on just because they were in the ambient environment."""
    monkeypatch.setenv("SRB_TARGET_NAME", "original")
    monkeypatch.setenv("SRB_ORIGINAL", "/somewhere/original")
    for target in ("original", "submission"):
        run = runner.execute(candidate("probe-inherited", DISCRIMINATE_ENV), target)
        assert run.passed, f"an inherited discriminator survived: {run.stdout}"


def test_the_grading_contract_is_not_in_the_candidates_environment(runner,
                                                                  monkeypatch):
    """A candidate is a program the submission's attacker wrote; it gets stage 2's scrub.

    Three names were popped by hand here -- the two that name the role, and
    ``SRB_ORIGINAL``.  That covered the leak this section is about and missed the
    rest of the same set: among them ``SRB_RESULT``, the file a stage's score is read
    out of, and ``SRB_SUITE_DIR``, the hidden tests.  Stage 3 inherits whatever
    launched it, and on a real run that is a container holding all eight.
    """
    # Every name in the set except the one stage 3 re-sets on purpose, which the
    # test below is about.  Taken from the set rather than listed, so a name added
    # to the contract is checked here without anyone remembering to.
    scrubbed = sorted(CONTRACT_ENV - {"SRB_WORK"})
    probe = "".join(
        f'if [ -n "${{{name}-}}" ]; then echo "LEAKED {name}"; exit 17; fi\n'
        for name in scrubbed)
    for name in CONTRACT_ENV:
        monkeypatch.setenv(name, f"/graders/{name.lower()}")

    for target in ("original", "submission"):
        run = runner.execute(candidate("probe-contract", probe), target)
        assert run.passed, f"on {target}: {run.stdout}"


def test_the_two_names_the_candidate_does_get_are_still_set(runner):
    """The scrub is by exact name, so it also removes what stage 3 means to pass.

    ``SRB_WORK`` is in ``CONTRACT_ENV`` and is re-set afterwards on purpose: it is
    the scratch directory the tasks' candidate scripts build in, and a task keying
    its cache on ``$WORK/$TOKEN`` needs both halves.  Pinned so that a future
    tightening of the scrub cannot quietly take them out.
    """
    run = runner.execute(candidate("needs-work", (
        'test -n "$SRB_WORK" || { echo "no SRB_WORK"; exit 1; }\n'
        'test -d "$SRB_WORK" || { echo "SRB_WORK is not a directory"; exit 1; }\n'
        'test "$(basename "$SRB_TARGET")" = "$SRB_TARGET_TOKEN" || '
        '{ echo "the token does not name the tree"; exit 1; }\n'
    )), "submission")
    assert run.passed, run.stdout


def test_the_staged_tree_paths_do_not_name_the_role(runner):
    """`os.getcwd().endswith("original")` must not work either: cwd *is* the
    tree, so a path component naming the role is the same leak."""
    for target, tree in runner.trees.items():
        assert "original" not in str(tree), tree
        assert "submission" not in str(tree), tree
    assert runner.trees["original"] != runner.trees["submission"]


def test_the_role_token_is_stable_within_a_run_and_not_across_runs(runner, trees,
                                                                  tmp_path):
    """A candidate needs a stable label to correlate two failures in one
    comparison; it must not be able to hard-code one it saw in another run."""
    assert runner.tokens["original"] == runner._token("original")
    other = CandidateRunner(COMMAND, *trees, tmp_path / "work2", timeout_sec=30.0)
    assert other.tokens["original"] != runner.tokens["original"]


def test_the_task_scripts_take_the_role_as_an_argument_and_keep_it():
    """Every task's run-candidate.sh needs the role -- it selects a toolchain --
    and every one of them has to take it from argv and stop it there.

    Reading it from argv is the point.  A script that took it from the
    environment would leak it to the candidate by doing nothing at all, because
    a child inherits the environment and inherits argv never."""
    tasks = sorted((Path(__file__).resolve().parents[2] / "tasks").iterdir())
    assert tasks, "no tasks found"
    seen = 0
    for task in tasks:
        script = task / "tests" / "verification" / "run-candidate.sh"
        if not script.is_file():
            continue
        seen += 1
        text = script.read_text(encoding="utf-8")
        code = [ln for ln in text.splitlines() if not ln.strip().startswith("#")]
        body = "\n".join(code)

        assert re.search(r'ROLE="\$\{1[:?]', body), \
            f"{task.name}: does not read the role from argv"
        reads = body.replace("unset SRB_TARGET_ROLE", "").replace("-u SRB_TARGET_ROLE", "")
        assert "SRB_TARGET_ROLE" not in reads, \
            f"{task.name}: still reads the role from the environment"
        # And it must not put it into the environment on the way out.
        for line in code:
            stripped = line.strip()
            if stripped.startswith("unset ") or stripped.startswith("env -u"):
                continue
            for leak in ("SRB_TARGET_ROLE=", "SRB_TARGET_NAME=", "SRB_ROLE="):
                assert leak not in stripped, \
                    f"{task.name}: exports the role: {stripped}"
    assert seen, "no run-candidate.sh found under any task"


# --------------------------------------------------------------------------- #
# a whole round
# --------------------------------------------------------------------------- #


def rounds_for(runner, *, rules=None, ruling=True, judge_driver=None) -> RoundRunner:
    rules = rules or scope()
    toolbox = toolbox_for(runner.trees["original"], runner.trees["submission"])
    adj = judge_driver if judge_driver is not None else judge(ruling, "ruled",
                                                             times=4)
    return RoundRunner("t", "Attack {{task}} at {{workspace}}.", runner,
                       Adjudicator(runner, rules, adj), toolbox, rules)


def play(runner, turns, *, rules=None, ruling=True, budget_sec=60.0,
         judge_driver=None) -> verification.Round:
    adversary = Adversary(id="a1", model="m", driver="scripted",
                          budget_sec=budget_sec, options={})
    driver = models.build("scripted", "m", options={"turns": turns})
    return rounds_for(runner, rules=rules, ruling=ruling,
                      judge_driver=judge_driver).run(adversary, driver)


def try_test(name, code, **extra):
    args = {"name": name, "filename": f"{name}.sh", "code": code}
    args.update(extra)
    return {"tool_calls": [{"name": "try_test", "arguments": args}]}


def report(**args):
    return {"tool_calls": [{"name": "report", "arguments": args}]}


def test_a_round_that_finds_and_claims_a_defect_breaks_through(runner):
    rnd = play(runner, [
        try_test("help", HELP, rationale="--help should still print usage"),
        report(found=True, candidate="help", claim="--help no longer prints usage",
               contract="documented command-line behaviour"),
    ])
    assert rnd.outcome == BROKEN
    assert rnd.adjudication.decision == UPHELD
    assert rnd.claimed == "help"
    assert rnd.candidates[0].contract == "documented command-line behaviour"


def test_a_claim_the_adjudicator_could_not_judge_is_an_error_not_a_survival(runner):
    """The round did its part.  An unjudged claim is a re-run, not ten points."""
    rnd = play(runner, [
        try_test("help", HELP, rationale="--help should still print usage"),
        report(found=True, candidate="help", claim="--help no longer prints usage",
               contract="documented command-line behaviour"),
    ], judge_driver=_Down())
    assert rnd.outcome == ROUND_ERROR
    assert rnd.outcome != SURVIVED
    assert rnd.adjudication.decision == UNDECIDED
    assert rnd.claimed == "help"
    assert "503" in rnd.error
    assert "did not complete" in rnd.reason


def test_an_unjudged_claim_keeps_the_candidate_in_the_report(runner):
    """Whoever re-runs this needs the test that was written, not just its name."""
    rnd = play(runner, [
        try_test("help", HELP, rationale="--help should still print usage"),
        report(found=True, candidate="help", claim="--help no longer prints usage",
               contract="documented command-line behaviour"),
    ], judge_driver=_Down())
    out = rnd.to_dict()
    assert out["outcome"] == ROUND_ERROR
    assert out["claimed"] == "help"
    assert out["candidate"]["code"] == HELP          # the decisive one, in full
    assert out["adjudication"]["decision"] == UNDECIDED
    assert "503" in out["adjudication"]["error"]


def test_a_round_that_reports_nothing_is_a_survival(runner):
    rnd = play(runner, [
        report(found=False, notes="looked at the CLI, everything matched"),
    ])
    assert rnd.outcome == SURVIVED
    assert "looked at the CLI" in rnd.reason
    assert rnd.adjudication is None


def test_an_out_of_scope_claim_is_a_survival_with_the_reason_kept(runner):
    rnd = play(runner, [
        try_test("keys", KEYS),
        report(found=True, candidate="keys", claim="key order changed",
               contract="output format"),
    ], ruling=False)
    assert rnd.outcome == SURVIVED
    assert rnd.adjudication.decision == REJECTED
    assert "out of scope" in rnd.reason
    assert rnd.claimed == "keys"  # what was tried stays on the record


def test_claiming_a_candidate_that_was_never_written_is_sent_back(runner):
    rnd = play(runner, [
        report(found=True, candidate="imaginary", claim="it is broken"),
        report(found=False, notes="on reflection, nothing"),
    ])
    assert rnd.outcome == SURVIVED
    assert "on reflection" in rnd.reason


def test_claiming_a_candidate_that_does_not_discriminate_is_sent_back(runner):
    rnd = play(runner, [
        try_test("same", BOTH),
        report(found=True, candidate="same", claim="surely something"),
        report(found=False, notes="it passes on both"),
    ])
    assert rnd.outcome == SURVIVED
    assert "passes on both" in rnd.reason


def test_a_round_is_bounded_by_the_clock_not_a_candidate_count(runner):
    # A round's only allowance is budget_sec, so a 21st candidate is admitted on
    # the same terms as the first.  No count bounds it.
    tries = [try_test(f"c{i:02d}", BOTH) for i in range(21)]
    rnd = play(runner, tries + [report(found=False, notes="nothing held up")])
    assert rnd.outcome == SURVIVED
    assert len(rnd.candidates) == 21


def test_an_explicit_turn_cap_still_ends_a_loop(runner):
    """The stages no longer cap turns, but the loop honours one from the one
    caller that sets it: the scope adjudicator's short sub-conversation."""
    read_only = toolbox_for(runner.trees["original"],
                            runner.trees["submission"])
    turns = [{"tool_calls": [{"name": "list_dir",
                              "arguments": {"path": "."}}]} for _ in range(3)]
    driver = models.build("scripted", "m", options={"turns": turns})
    submit = models.ToolSpec("report", "end the run", {"type": "object"})
    res = agentloop.run(driver, "system", "opening", read_only, submit,
                        max_turns=2)
    assert res.outcome == agentloop.TURN_LIMIT
    assert res.turns == 2
def test_a_survival_states_how_many_candidates_it_wrote(runner):
    """The non-zero half of the activity clause, which zero alone cannot pin.

    This arrived as `..._out_of_turns_is_a_survival_that_says_so`, resting on
    `play(max_turns=2)`.  The stages no longer cap turns -- that cap and the
    candidate limit were both retired, and `test_an_explicit_turn_cap_still_ends_a_loop`
    above now covers the one caller that still sets one -- so the turn limit is no
    longer a route to a survival and the test was rewritten onto one that is.  The
    property it was carrying is untouched: a round that wrote candidates says how
    many, which is what makes the zero in the test below legible as a measurement
    rather than a clause someone forgot.
    """
    rnd = play(runner, [try_test("a", BOTH), try_test("b", BOTH),
                        report(found=False, notes="nothing held up")])
    assert rnd.outcome == SURVIVED
    assert "wrote 2 candidate(s)" in rnd.reason


def test_a_survival_states_its_activity_including_zero(runner):
    """Zero has to be printed, not left out.

    A count appended only when it is non-zero makes the round that did nothing read
    exactly like a round whose clause nobody wrote, and an absence cannot be
    audited.  The distinction matters because it is the one that separates an
    adversary who looked and found nothing from an adversary who never looked, and
    both collect `points_per_survived_model`.
    """
    rnd = play(runner, [report(found=False, notes="nothing here")])
    assert rnd.outcome == SURVIVED
    assert "read 0 file(s)" in rnd.reason
    assert "wrote 0 candidate(s)" in rnd.reason


def test_the_activity_count_is_per_round_not_cumulative(runner):
    """The toolbox is shared by every round in the stage, so counters add up.

    Reporting the absolute would credit each adversary with its predecessors'
    reads, which is worse than reporting nothing: the second round of a broken
    stage would look busy.
    """
    rounds = rounds_for(runner)
    rounds.toolbox.reads.update({"a.rs", "b.rs"})
    rounds.toolbox.calls = 7
    before_reads, before_calls = len(rounds.toolbox.reads), rounds.toolbox.calls
    rounds.toolbox.reads.add("c.rs")
    rounds.toolbox.calls += 2
    assert rounds._activity(before_reads, before_calls, 0) == (
        "read 1 file(s), 2 tool call(s), wrote 0 candidate(s)")


def test_a_round_out_of_time_is_a_survival(runner):
    rnd = play(runner, [try_test("a", BOTH)], budget_sec=0.0)
    assert rnd.outcome == SURVIVED
    assert "full budget" in rnd.reason
    assert rnd.candidates == []


def test_the_adversary_is_told_the_outcome_on_each_tree(runner):
    """Without this feedback the round is guessing, so it is load-bearing."""
    rounds = rounds_for(runner)
    state = {"candidates": [], "claim": None}
    out = rounds._try(state, {"name": "help", "filename": "help.sh", "code": HELP})
    assert "on original:   PASS" in out
    assert "on submission: FAIL" in out
    assert "this discriminates" in out


def test_a_test_broken_on_both_trees_is_reported_as_such(runner):
    rounds = rounds_for(runner)
    state = {"candidates": [], "claim": None}
    out = rounds._try(state, {"name": "dead", "filename": "dead.sh", "code": DEAD})
    assert "does not pass on the original" in out


def test_the_adversary_is_not_invited_to_report_a_build_failure(runner):
    """The adjudicator refusing the claim is not enough on its own.

    This text is what the adversary acts on. A submission-side fault reaching it as
    "this discriminates ... then report it" is the harness asking for a claim it will
    then throw away, and the round's hour goes on it.
    """
    rounds = rounds_for(runner)
    state = {"candidates": [], "claim": None}
    out = rounds._try(state, {"name": "wont-build", "filename": "wb.sh",
                              "code": SUB_FAULT})
    assert "could not be tested" in out
    assert "then report it" not in out
    assert "cannot count either way" in out
    # And where it does belong, so the round does not go looking for another way to
    # charge for it.
    assert "behavioural stage" in out


def test_an_original_side_fault_does_not_send_the_adversary_to_debug_it(runner):
    """A tree that did not build is not the candidate's fault, so this side cannot
    be told what the other side is told.  "Most often the test itself is wrong" is a
    true generalisation and the wrong advice here: nothing is wrong with the
    candidate."""
    rounds = rounds_for(runner)
    state = {"candidates": [], "claim": None}
    out = rounds._try(state, {"name": "orig-wb", "filename": "owb.sh",
                              "code": ORIG_FAULT})
    assert "original tree could not be tested" in out
    assert "the test itself is wrong" not in out


def test_a_genuinely_broken_candidate_still_gets_the_old_advice(runner):
    """The control for the two above: the fault branch must not swallow the case it
    sits in front of. ``exit 1`` is a candidate whose assertions failed, and being
    told the test is probably wrong is the right thing to tell an adversary."""
    rounds = rounds_for(runner)
    state = {"candidates": [], "claim": None}
    out = rounds._try(state, {"name": "dead", "filename": "dead.sh", "code": DEAD})
    assert "could not be tested" not in out
    assert "the test itself is wrong" in out


def test_rewriting_a_candidate_replaces_it_rather_than_duplicating(runner):
    rounds = rounds_for(runner)
    state = {"candidates": [], "claim": None}
    rounds._try(state, {"name": "x", "filename": "x.sh", "code": BOTH})
    rounds._try(state, {"name": "x", "filename": "x.sh", "code": HELP})
    assert [c.name for c in state["candidates"]] == ["x"]
    assert state["candidates"][0].discriminates


def test_a_driver_failure_makes_the_round_an_error_not_a_survival(runner):
    """A model that could not be reached is not evidence the submission is sound."""
    rnd = play(runner, [])
    assert rnd.outcome == ROUND_ERROR
    assert "could not be completed" in rnd.reason


# --------------------------------------------------------------------------- #
# a round the toolbox refused
# --------------------------------------------------------------------------- #

# Stage 3 scores a survival, so an adversary that could not run and one that ran
# properly and found nothing produce the same ten points.  That is what kept the
# read_file collision invisible: three graded rounds spent their budget being told
# "path must be a non-empty string" and were recorded as evidence the migration
# was sound.  A round has to be able to say it never got to look.


def dead_call(name="fetch_source"):
    """A tool call that will come back as an ERROR string.

    ``read_file`` is the name the toolbox no longer answers to, which is what a
    gateway-rewritten call looked like from this side; an unknown argument gets
    the same refusal from a tool that does exist.
    """
    return {"tool_calls": [{"name": name,
                            "arguments": {"file_path": "x", "unknowable": 1}}]}


def test_a_round_whose_every_tool_call_failed_is_an_error_not_a_survival(runner):
    """The measured case: 90 calls, 89 refusals, zero tests, ten points."""
    rnd = play(runner, [dead_call(), dead_call(), dead_call(), dead_call(),
                        report(found=False, notes="I could not read anything")])
    assert rnd.outcome == ROUND_ERROR
    assert rnd.outcome != SURVIVED
    assert "refusing it" in rnd.reason
    # The submit call is not counted: it is the answer, not a probe.
    assert rnd.tool_errors == 4 and rnd.tool_calls == 4


def test_a_round_calling_a_tool_that_does_not_exist_is_not_a_survival(runner):
    """A name the toolbox does not answer to is the collision's exact signature."""
    rnd = play(runner, [dead_call("read_file"), dead_call("read_file"),
                        report(found=False, notes="the tool did not work")])
    assert rnd.outcome == ROUND_ERROR
    assert "refusing it" in rnd.reason


def test_a_round_that_probed_successfully_and_found_nothing_still_survives(runner):
    """The condition is a refusal rate, not the presence of any failed call.

    A round that mistypes one argument, is told which word was wrong, corrects it
    and goes on to read both trees has been attacked properly.  Scoring that as an
    error would ask for a re-run that has nothing to fix.
    """
    rnd = play(runner, [dead_call(),
                        try_test("a", BOTH), try_test("b", BOTH),
                        report(found=False, notes="read both trees, they match")])
    assert rnd.outcome == SURVIVED
    assert "read both trees" in rnd.reason
    assert rnd.tool_errors == 1 and rnd.tool_calls == 3   # 1 refused, 2 try_test
    assert rnd.tool_errors / rnd.tool_calls <= RoundRunner.MAX_TOOL_ERROR_RATE


def test_a_round_that_never_called_a_tool_is_left_alone_but_says_so(runner):
    """Zero probes is not treated as a harness fault, and stays visible.

    Two unrelated runs reach zero tool calls: one the clock took before its first
    turn, one that took turns and chose not to look.  The second is a real problem
    -- with the prompt or the model, not the toolbox -- and a re-run would not fix
    it, so the gate does not claim it.  What it must not do is hide it: the count
    is in the report next to the survival.
    """
    rnd = play(runner, [{"text": "Reading the diff in my head."},
                        report(found=False, notes="looks fine to me")])
    assert rnd.outcome == SURVIVED
    assert rnd.tool_calls == 0
    assert rnd.to_dict()["tool_calls"] == 0


@pytest.mark.parametrize("command", [
    ["sh", "-c", 'sh "$SRB_CANDIDATE"'],           # every test in this file
    ["sh", "-c", "pytest test_candidate.py"],      # a path inside a shell fragment
    ["cargo", "test", "--offline"],                # a flag, and no path at all
    ["python3", "-m", "pytest"],                   # a module name, not a file
])
def test_the_launch_check_does_not_refuse_a_command_that_works(trees, tmp_path,
                                                               command):
    """The guard's cost: it must not reject commands that run today.

    It reads tokens looking for a script named relatively, and a shell fragment can
    contain something that looks exactly like one -- `pytest test_candidate.py` is a
    single argument for `sh -c`, and there is no file by that name anywhere.
    """
    original, submission = trees
    CandidateRunner(command, original, submission, tmp_path / f"ok{len(command)}",
                    timeout_sec=30.0)


def test_a_relative_script_is_refused_before_a_round_is_spent_on_it(trees,
                                                                    tmp_path):
    """lang07's exact shape, rejected where it is cheapest to reject.

    ``candidate_command`` runs with its working directory set to a copy of the tree
    under test, so ``["bash", "run-candidate.sh"]`` asks for a file at the root of
    the repository, where it is not.  Every candidate then comes back as not having
    started, which the round reads as a candidate that does not pass -- so no
    candidate can count, all six rounds report nothing found, and the submission is
    paid sixty points untested.

    Raising here makes ``build`` fail and ``cli`` write a failed stage result, which
    is a thing somebody reads.
    """
    original, submission = trees
    (original / "run-candidate.sh").unlink(missing_ok=True)
    with pytest.raises(ValueError) as caught:
        CandidateRunner(["bash", "run-candidate.sh"], original, submission,
                        tmp_path / "refused", timeout_sec=30.0)
    message = str(caught.value)
    assert "relatively" in message and "absolute path" in message


@pytest.fixture
def unlaunchable(trees, tmp_path: Path) -> CandidateRunner:
    """A runner whose command passes the static check and still cannot start.

    The script is where the command says it is, absolutely, so construction is
    happy.  Its shebang names an interpreter the machine does not carry, which is
    the case a static check cannot see: a toolchain absent from the image looks
    exactly like this.  The kernel then refuses the exec and every run fails before
    the candidate is reached.
    """
    original, submission = trees
    script = tmp_path / "run-candidate.sh"
    script.write_text("#!/usr/bin/no-such-interpreter\necho unreachable\n")
    script.chmod(0o755)
    return CandidateRunner([str(script)], original, submission,
                           tmp_path / "unlaunchable", timeout_sec=30.0)


def test_a_round_whose_candidates_never_launched_is_an_error_not_a_survival(
        unlaunchable):
    """try_test answered every call; what it said was that nothing ran.

    This is invisible to the tool-error rate -- the tool worked -- and invisible to
    the candidate count, which is not zero.  Measured on lang07, which named its
    script relatively: 127 on both trees for every candidate, no tool errors, six
    rounds reporting nothing found, sixty points for an attack that never ran.
    """
    rnd = play(unlaunchable, [
        try_test("help", HELP), try_test("both", BOTH),
        report(found=False, notes="neither test showed anything"),
    ])
    assert rnd.outcome == ROUND_ERROR
    assert rnd.outcome != SURVIVED
    assert "started" in rnd.reason
    # Not the rate condition: the tool itself was working throughout.
    assert rnd.tool_errors == 0 and rnd.tool_calls == 2


def test_a_candidate_that_runs_and_fails_is_not_mistaken_for_one_that_never_ran(
        runner):
    """The neighbouring case, and the one the guard must not claim.

    A test that runs and fails its assertions is the single most common thing an
    adversary produces, and it is a normal part of a round that found nothing.  If
    the guard fired on it, every honest round would be re-run forever.
    """
    rnd = play(runner, [try_test("dead", DEAD), try_test("also-dead", DEAD),
                        report(found=False, notes="my tests were wrong")])
    assert rnd.outcome == SURVIVED
    assert "my tests were wrong" in rnd.reason
    codes = {run.exit_code for cand in rnd.candidates for run in cand.runs}
    assert codes == {1}, f"expected a plain test failure, got {codes}"


def test_a_refused_round_that_nonetheless_found_a_defect_still_breaks_through(runner):
    """The gate is on survivals and empty reports, never on a real break.

    A round that fought the toolbox the whole way and still produced a test that
    discriminates has proved the defect; the refusals are then only an explanation
    of how little budget it had left.
    """
    rnd = play(runner, [
        dead_call(), dead_call(), dead_call(),
        try_test("help", HELP, rationale="--help should still print usage"),
        report(found=True, candidate="help", claim="--help no longer prints usage",
               contract="documented command-line behaviour"),
    ])
    assert rnd.outcome == BROKEN
    assert rnd.adjudication.decision == UPHELD


def test_the_round_records_the_call_counts_the_judgement_rests_on(runner):
    """Whoever reads the report has to be able to check the gate's arithmetic."""
    rnd = play(runner, [dead_call(), dead_call(),
                        report(found=False, notes="nothing")])
    out = rnd.to_dict()
    assert out["tool_calls"] == 2
    assert out["tool_errors"] == 2
    assert out["outcome"] == ROUND_ERROR


# --------------------------------------------------------------------------- #
# the stage, as the scorer reads it
# --------------------------------------------------------------------------- #


def stage_for(runner, tmp_path, count=3, rules=None) -> Stage:
    rules = rules or scope()
    probe = Probe(task="t", scope=rules, root=tmp_path,
                  candidate_command=COMMAND,
                  adversaries=[Adversary(id=f"a{i}", model=f"m{i}",
                                         driver="scripted", budget_sec=60.0)
                               for i in range(1, count + 1)])
    toolbox = toolbox_for(runner.trees["original"], runner.trees["submission"])
    return Stage(probe, runner, Adjudicator(runner, rules, judge(True, times=8)),
                 toolbox, "attack {{task}}")


def test_the_stage_writes_one_pass_per_surviving_adversary(runner, tmp_path):
    stage = stage_for(runner, tmp_path)

    def factory(adv):
        return models.build("scripted", adv.model, options={"turns": [
            report(found=False, notes=f"{adv.id} found nothing")]})

    res = stage.run(factory)
    assert len(res.checks) == 3
    assert {c.verdict for c in res.checks} == {"pass"}
    assert res.status == "ok"
    assert res.metadata["survived"] == 3
    assert res.metadata["broken"] == 0
    assert all(c.metadata["kind"] == "round" for c in res.checks)
    assert all(c.weight == 1.0 for c in res.checks)
    assert [c.id for c in res.checks] == ["a1", "a2", "a3"]


def test_a_break_is_one_failing_check_and_the_rest_still_pass(runner, tmp_path):
    stage = stage_for(runner, tmp_path)

    def factory(adv):
        turns = ([try_test("help", HELP, rationale="--help must still print usage"),
                  report(found=True, candidate="help", claim="usage text is gone",
                         contract="documented command-line behaviour")]
                 if adv.id == "a2" else
                 [report(found=False, notes="nothing")])
        return models.build("scripted", adv.model, options={"turns": turns})

    res = stage.run(factory)
    verdicts = {c.id: c.verdict for c in res.checks}
    assert verdicts == {"a1": "pass", "a2": "fail", "a3": "pass"}
    assert res.metadata["broken"] == 1
    broke = next(c for c in res.checks if c.id == "a2")
    assert broke.evidence and broke.evidence[0]["path"] == "candidates/help.sh"
    assert "must still print usage" in broke.detail   # the reader gets the why
    assert broke.summary == "usage text is gone"


def test_a_round_that_could_not_run_is_an_error_not_a_pass(runner, tmp_path):
    """Ten points for a model we failed to call would be points for nothing."""
    stage = stage_for(runner, tmp_path)

    def factory(adv):
        if adv.id == "a2":
            raise models.ModelError("no credentials for m2")
        return models.build("scripted", adv.model, options={"turns": [
            report(found=False, notes="nothing")]})

    res = stage.run(factory)
    verdicts = {c.id: c.verdict for c in res.checks}
    assert verdicts == {"a1": "pass", "a2": "error", "a3": "pass"}
    assert res.status == "ok"          # two rounds did run
    assert res.metadata["errored"] == 1
    assert any("did not complete" in n for n in res.notes)


def test_a_round_whose_adjudication_failed_pays_nothing(runner, tmp_path):
    """An adjudicator outage is not a survival: nothing was contested."""
    rules = scope()
    probe = Probe(task="t", scope=rules, root=tmp_path,
                  candidate_command=COMMAND,
                  adversaries=[Adversary(id=f"a{i}", model=f"m{i}",
                                         driver="scripted", budget_sec=60.0)
                               for i in (1, 2)])
    tb = toolbox_for(runner.trees["original"], runner.trees["submission"])
    stage = Stage(probe, runner, Adjudicator(runner, rules, _Down()), tb, "attack")

    def factory(adv):
        if adv.id == "a1":                      # finds a real break, unjudgeable
            return models.build("scripted", adv.model, options={"turns": [
                try_test("help", HELP, rationale="--help prints usage"),
                report(found=True, candidate="help",
                       claim="--help no longer prints usage",
                       contract="documented command-line behaviour")]})
        return models.build("scripted", adv.model, options={"turns": [
            report(found=False, notes="nothing")]})

    res = stage.run(factory)
    verdicts = {c.id: c.verdict for c in res.checks}
    assert verdicts == {"a1": "error", "a2": "pass"}
    assert res.status == "ok"                   # a2 is a real result
    assert res.metadata["errored"] == 1
    assert res.metadata["survived"] == 1        # a1 is not counted as one
    assert any("did not complete" in n for n in res.notes)

    # And the scorer pays one survivor, not two.
    from swerefactor import scoring
    from swerefactor.config import ScoringPolicy
    verdict = scoring.Verdict(task="t")
    scoring.grade_verification(res, verdict, ScoringPolicy())
    assert verdict.adversaries_survived == 1
    assert verdict.verification_points == pytest.approx(10.0)
    assert any("could not be completed" in n for n in verdict.notes)


def test_no_round_completing_makes_the_whole_stage_an_error(runner, tmp_path):
    stage = stage_for(runner, tmp_path)

    def factory(adv):
        raise models.ModelError("the endpoint is down")

    res = stage.run(factory)
    assert res.status == "error"
    assert {c.verdict for c in res.checks} == {"error"}
    assert any("no verification round completed" in n for n in res.notes)


def test_the_stage_records_the_scope_it_judged_under(runner, tmp_path):
    """The report has to show why a break was or was not counted."""
    res = stage_for(runner, tmp_path, count=1).run(
        lambda adv: models.build("scripted", adv.model, options={
            "turns": [report(found=False, notes="nothing")]}))
    assert res.metadata["scope"]["reruns"] == 2
    assert res.metadata["scope"]["deny"]
    assert res.metadata["candidate_runs"] == 0


def test_stage_checks_feed_the_scorer_at_ten_points_each(runner, tmp_path):
    """The arithmetic the policy names, end to end: 6 rounds, one break."""
    from swerefactor import scoring
    from swerefactor.config import ScoringPolicy

    stage = stage_for(runner, tmp_path, count=6)

    def factory(adv):
        turns = ([try_test("help", HELP),
                  report(found=True, candidate="help", claim="usage text is gone",
                         contract="documented command-line behaviour")]
                 if adv.id == "a5" else
                 [report(found=False, notes="nothing")])
        return models.build("scripted", adv.model, options={"turns": turns})

    res = stage.run(factory)
    verdict = scoring.Verdict(task="t")
    scoring.grade_verification(res, verdict, ScoringPolicy())
    assert verdict.adversaries_total == 6
    assert verdict.adversaries_survived == 5
    assert verdict.verification_points == pytest.approx(50.0)
    assert len(verdict.verification_breaks) == 1
    assert verdict.verification_breaks[0]["adversary"] == "a5"
