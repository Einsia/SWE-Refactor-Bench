"""Whether the agent loop's account of itself reaches the host.

The loop narrates every decision it makes to stderr: which round started, what it
exited, whether a round was dropped as wedged and resumed, how many wedges have
accumulated, and which of the four exits ended the phase.  Harbor runs the script
as a setup command and captures that stream into its own machinery, which never
writes it to the trial directory -- so on a finished run, ``agent/`` held
``events-N.jsonl`` and ``rc-N.txt`` and no explanation of how they came to be.

The cost showed up while supervising a live fleet.  Five runs had ``rc-1.txt`` = 124
as their newest host-side file and nothing after it for twenty minutes.  Whether
that was a wedged round being resumed on schedule or a dead container could not be
read from the trial directory at all; it took ``docker exec`` into each container to
find round 2 mid-write.  A container that has finished cannot be exec'd into, so for
any completed run the answer would simply have been unavailable.

So the loop's stderr is appended to ``$LOG_DIR/loop.log``, which ``mirror()``
already copies to the bind-mounted ``$MIRROR_DIR`` after every round.

Driven in a container, because ``/workspace/repo`` is hard-coded in four places and
this box's root filesystem belongs to everyone.  ``codex`` is a stub: the subject is
what the loop records about a round, not what a model does inside one.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

LOOP = Path(__file__).resolve().parents[1] / "swerefactor" / "harness" / "srb-agent-loop.sh"
IMAGE = os.environ.get("SRB_TEST_IMAGE", "swerefactor/infra:1")

#: Exits 0 having written the sentinel, so the phase ends after one round without
#: waiting on anything.  Writes to stderr too -- that stream must stay in
#: stderr-N.log rather than being folded into the loop's own account.
STUB_DONE = """#!/bin/sh
echo '{"type":"item.completed"}'
echo 'codex-stub: this line belongs to the round, not the loop' >&2
for a in "$@"; do
  case "$prev" in -o) printf 'SRB_TASK_COMPLETE\\n' > "$a" ;; esac
  prev="$a"
done
exit 0
"""

#: Exits 124 the way `timeout` does, having written nothing: the gateway never
#: answered, which is the one thing the wedge counter is for.  The third ends the
#: phase.
STUB_WEDGE = """#!/bin/sh
echo 'codex-stub: wedged' >&2
exit 124
"""

#: Exits 124 having answered first -- a round the per-round cap cut off while the
#: model was working.  Indistinguishable from STUB_WEDGE by exit code alone, which
#: is how a working phase came to be stopped as an infrastructure fault.
STUB_PRODUCTIVE_WEDGE = """#!/bin/sh
echo '{"type":"turn.started"}'
echo '{"type":"item.completed","item":{"type":"command_execution"}}'
echo 'codex-stub: answered, then the cap fired' >&2
exit 124
"""

#: The same round, but it costs its whole budget in real seconds: it answers and then
#: blocks until `timeout` delivers the 124 itself, rather than imitating the exit
#: code and returning at once.
#:
#: STUB_PRODUCTIVE_WEDGE is enough to ask how such a round is *classified*, and that
#: question does not involve the clock.  It is not enough to ask about a figure
#: measured after the round: four rounds at 0s each leave the deadline exactly where
#: it started, so all four correctly report the full budget still left and a stale
#: reading is indistinguishable from a fresh one.
#:
#: ``/bin/sleep`` by absolute path, because ``_run_loop`` shadows ``sleep`` with a
#: no-op on PATH to make the loop's own 60s backoffs free; this one has to be the
#: real thing.  ``/bin/echo`` rather than the builtin for the events, so they reach
#: the file before SIGTERM arrives: a builtin's output to a redirected file is
#: block-buffered and dies unflushed, which would present as a round that answered
#: nothing -- the very case this stub exists to be distinguished from.
STUB_SLOW_PRODUCTIVE = """#!/bin/sh
/bin/echo '{"type":"turn.started"}'
/bin/echo '{"type":"item.completed","item":{"type":"command_execution"}}'
/bin/echo 'codex-stub: answered, then the cap fired' >&2
/bin/sleep 3600
"""

#: A round that reached nothing and says so nine times: the thread has outgrown what
#: the gateway will accept, so every request is dropped mid-upload and retried.  The
#: two events before the errors are emitted locally, before any socket opens, so
#: nothing here is evidence that the gateway answered -- and the errors are evidence
#: that it did not.
#:
#: Verbatim from lang04/max, which spent from round 39 to its wall this way: 14
#: rounds, 1800s each, zero work items, wedge counter never moving.
STUB_UNSENDABLE_THREAD = """#!/bin/sh
echo '{"type":"thread.started","thread_id":"019fddb1"}'
echo '{"type":"turn.started"}'
i=1
while [ "$i" -le 9 ]; do
  echo '{"type":"error","message":"Reconnecting... '"$i"'/10 (stream disconnected'\\
' before completion: error sending request for url (http://gw:8888/v1/responses))"}'
  i=$(( i + 1 ))
done
echo 'codex-stub: never sent a byte' >&2
exit 124
"""

#: Alternates: odd rounds die instantly having reached nothing, even rounds work.
#:
#: Every other stub in this file fails the SAME WAY FOREVER, which is the one shape
#: that cannot see whether a counter survives a recovery -- if it never resets, a
#: forever-failing fixture reaches the cap on schedule and passes.  So this one
#: recovers between failures, and the question becomes whether the loop noticed.
#:
#: The odd rounds emit nothing at all, so events-N.jsonl is empty and
#: reached_gateway stays 0: that is the path to the fast-fail counter rather than to
#: the gateway backoff above it.  The even rounds emit a work item and append to the
#: tree (so the fingerprint moves and the idle counter clears) but never write the
#: sentinel, so the phase keeps going instead of ending on round 2.
STUB_FAST_FAIL_THEN_WORKS = """#!/bin/sh
n=$(( $(cat /t/n 2>/dev/null || echo 0) + 1 ))
echo "$n" > /t/n
if [ $(( n % 2 )) = 1 ]; then
  echo 'codex-stub: died before the socket opened' >&2
  exit 1
fi
echo '{"type":"turn.started"}'
echo '{"type":"item.completed","item":{"type":"command_execution"}}'
printf 'work-%s\\n' "$n" >> /workspace/repo/f
echo 'codex-stub: ported a module' >&2
exit 0
"""

#: The same failure with no recovery in it: a missing key, a bad flag, an unreachable
#: gateway -- something no number of retries changes.  Reaches nothing, so this is the
#: fast-fail path and not the gateway backoff.
STUB_FAST_FAIL_ALWAYS = """#!/bin/sh
echo 'codex-stub: died before the socket opened' >&2
exit 1
"""


def _docker_ok() -> bool:
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "image", "inspect", IMAGE],
                          capture_output=True).returncode == 0


pytestmark = pytest.mark.skipif(
    not _docker_ok(), reason=f"needs docker and {IMAGE}")


def _run_loop(tmp_path: Path, stub: str, **env) -> tuple[int, Path, Path]:
    """Run the real script in a container; return rc and the two log dirs."""
    logs, mirror, bin_ = tmp_path / "logs", tmp_path / "mirror", tmp_path / "bin"
    for d in (logs, mirror, bin_):
        d.mkdir(parents=True, exist_ok=True)
    (bin_ / "codex").write_text(stub, encoding="utf-8")
    (bin_ / "codex").chmod(0o755)
    (tmp_path / "instruction.md").write_text("port it\n", encoding="utf-8")

    settings = {
        "SRB_AGENT_LOG_DIR": "/t/logs",
        "SRB_AGENT_MIRROR_DIR": "/t/mirror",
        "SRB_INSTRUCTION": "/t/instruction.md",
        "CODEX_HOME": "/t/codex-home",
        "SRB_AGENT_DEADLINE_SEC": "600",
        "SRB_AGENT_MAX_ROUNDS": "4",
        "SRB_AGENT_MAX_ROUND_SEC": "30",
        "SRB_AGENT_MIN_ROUND_SEC": "0",
        "SRB_AGENT_MAX_WEDGES": "3",
    }
    settings.update(env)
    flags: list[str] = []
    for k, v in settings.items():
        flags += ["-e", f"{k}={v}"]

    # `sleep 0` replaces the loop's backoffs: it waits 60s between wedges, and a
    # test that actually waited three minutes would be one nobody runs.
    proc = subprocess.run(
        ["docker", "run", "--rm", "--network", "none",
         "-v", f"{tmp_path}:/t", "-v", f"{LOOP}:/loop.sh:ro",
         *flags, "--entrypoint", "bash", IMAGE, "-c",
         "mkdir -p /workspace/repo /t/codex-home/sessions "
         "&& printf 'x' > /workspace/repo/f "
         "&& printf '#!/bin/sh\\nexit 0\\n' > /usr/local/bin/sleep "
         "&& chmod 755 /usr/local/bin/sleep "
         "&& export PATH=/t/bin:$PATH && bash /loop.sh"],
        capture_output=True, text=True, timeout=300)
    return proc.returncode, logs, mirror


def test_the_loop_writes_its_account_where_the_mirror_finds_it(tmp_path):
    """One clean round: loop.log exists on both sides and names the round."""
    rc, logs, mirror = _run_loop(tmp_path, STUB_DONE)
    assert (logs / "loop.log").is_file(), f"rc={rc}, no loop.log in LOG_DIR"
    # The mirrored copy is the one a supervisor on the host can read at all.
    assert (mirror / "loop.log").is_file(), f"rc={rc}, loop.log never mirrored"
    text = (mirror / "loop.log").read_text(errors="replace")
    assert "round 1 starting" in text, text
    assert "round 1 exited rc=0" in text, text
    assert "declared completion" in text, text


def test_a_dropped_round_says_so_and_says_it_is_resuming(tmp_path):
    """The signal whose absence cost twenty minutes of guessing.

    rc=124 under the per-round cap is a wedged round the loop drops and resumes,
    which from the host looks exactly like a dead container: rc-N.txt = 124 and
    then silence until the next round finishes.  The distinction has to be written
    down, including the running wedge count, because the third one ends the phase.
    """
    rc, _, mirror = _run_loop(tmp_path, STUB_WEDGE,
                              SRB_AGENT_MAX_SESSION_ROTATIONS="0")
    text = (mirror / "loop.log").read_text(errors="replace")
    assert "produced nothing" in text and "was dropped" in text, text
    assert "wedge 1/3" in text, text
    assert "wedge 3/3" in text, text
    # And the phase ends as infrastructure, not as the model running out of time.
    # Rotations are off so the cap ends the phase here: with them on, the cap is a
    # recovery point and the ending moves to the exhaustion test below.  What this
    # test is about -- that a dropped round is written down with its running count,
    # and that the ending is attributed to the gateway rather than to the clock --
    # is the same either way.
    assert (mirror / "stop-reason.txt").read_text().strip() == "infrastructure"
    assert "treating as an infrastructure fault" in text, text


def test_a_capped_round_that_answered_is_not_a_wedge(tmp_path):
    """The counter must prove its claim before it advances.

    rc=124 under the per-round cap has two causes that share an exit code: a
    gateway that stopped answering, and a model that was still working when the cap
    fired.  Only the first is a wedge.  Counting the second ended lang01/medium as
    an infrastructure fault 95 minutes into a 40-hour budget, after rounds that had
    written 406, 478 and 394 events -- recorded, each time, as "produced nothing".

    Four rounds all answer and all get capped here.  None may count, so the phase
    must never reach the cap of three, and must not be labelled infrastructure.
    """
    _, _, mirror = _run_loop(tmp_path, STUB_PRODUCTIVE_WEDGE)
    text = (mirror / "loop.log").read_text(errors="replace")
    assert "still answering when the" in text, text
    assert "not counted as a wedge" in text, text
    # These rounds answered late; they did not produce nothing.  Both sentences exist
    assert "produced nothing" not in text, text
    # Four capped rounds, and the phase still has not been called an infrastructure
    # fault -- the failure mode this guards is exactly that verdict.
    reason = mirror / "stop-reason.txt"
    assert not reason.is_file() or reason.read_text().strip() != "infrastructure", \
        f"a phase whose every round answered was stopped as infrastructure: {text}"


def test_a_capped_round_that_only_failed_is_a_wedge(tmp_path):
    """The other half of the same question, and the half that was wrong.

    The test above requires that a round which *answered* not be counted.  Getting
    that right by asking "did anything come back at all?" also stops counting the
    rounds the counter exists for, because a failure comes back too: an error event
    is a request that did not complete, which is the evidence for a wedge and not
    against it.

    lang04/max is what that cost.  From round 39 to its 30-hour wall, every round
    emitted `thread.started`, `turn.started` and nine `Reconnecting... N/10 (stream
    disconnected before completion)` -- a 52MB thread the gateway would no longer
    accept.  Fourteen rounds, 1800s each, not one work item, and the wedge counter
    sat at 0: fourteen hours spent retrying a request that could not be sent, on a
    phase that was then labelled as having spent its budget.

    Three such rounds here must reach the cap and end the phase as infrastructure --
    which is what it was.  Under the shared flag this asserts nothing at all: the
    phase runs all four rounds, writes no stop-reason, and passes only the parts of
    the older test that were true anyway.

    Rotations are disabled so the cap ends the phase rather than escalating to a
    fresh thread; that escalation is the next test's subject, and this one is about
    which rounds the counter is willing to count.
    """
    _, _, mirror = _run_loop(tmp_path, STUB_UNSENDABLE_THREAD,
                             SRB_AGENT_MAX_SESSION_ROTATIONS="0")
    text = (mirror / "loop.log").read_text(errors="replace")
    assert "produced nothing for" in text, \
        f"a round that never reached the gateway was not counted as a wedge: {text}"
    # Nothing answered and nothing was still answering, so neither sentence about a
    # late reply belongs on these rounds.
    assert "still answering when the" not in text, text
    assert "wedge 3/3" in text, text
    reason = mirror / "stop-reason.txt"
    assert reason.is_file(), f"no stop-reason after three wedged rounds: {text}"
    assert reason.read_text().strip() == "infrastructure", \
        f"stop-reason is {reason.read_text().strip()!r}: {text}"


def test_the_wedge_cap_tries_a_fresh_thread_before_giving_up(tmp_path):
    """Two failures produce identical rounds and only one is survivable.

    "The gateway is not answering" and "the gateway will not accept this thread"
    both look like rc=124 with no work in the stream.  The second is recoverable by
    retiring the thread, which is what the rotation branch is for -- but that branch
    reads error text, and a gateway that drops the stream mid-upload rather than
    refusing the request outright never emits any of it.  lang04/max sent a 52MB
    thread fourteen times and not one context error, so nothing tried the one move
    that would have changed the request.

    Reaching the wedge cap is the evidence that licenses trying it: one dropped
    round is a transient and must not cost a working thread its plan, but three in a
    row with nothing in them is already enough to end the phase, so it is enough to
    rotate.  Three wedges here must retire the thread and keep going, and the
    counter must start the fresh thread at zero, because what it measures -- whether
    the endpoint answers -- is not what just changed.
    """
    _, _, mirror = _run_loop(tmp_path, STUB_UNSENDABLE_THREAD)
    text = (mirror / "loop.log").read_text(errors="replace")
    assert "starting a new thread (rotation 1/3" in text, \
        f"the wedge cap gave up without trying a fresh thread: {text}"
    # Counted to the cap, then reset for the new thread rather than carrying over.
    assert "wedge 3/3" in text, text
    assert "(wedge 1/3" in text.split("rotation 1/3")[1], \
        f"the fresh thread inherited the old thread's wedge count: {text}"
    # Four rounds is not enough to exhaust three rotations, so the phase is still
    # going: the cap is a recovery point now, not a verdict.
    reason = mirror / "stop-reason.txt"
    assert not reason.is_file() or reason.read_text().strip() != "infrastructure", \
        f"gave up as infrastructure with rotations still available: {text}"


def test_a_wedged_phase_still_ends_once_the_rotations_are_spent(tmp_path):
    """Making the cap survivable must not make it unreachable.

    Rotating at the wedge cap moved the ending, and an ending that moved is worth a
    test of its own: a gateway that answers nothing must still stop the phase, and
    stop it as an infrastructure fault rather than draining the whole deadline into
    rounds that produce nothing.  Two tests above assert the recovery; this one
    asserts the floor under it.

    One wedge per thread and two rotations reaches the exhausted state in three
    rounds rather than nine, so the case is cheap enough to keep.  The count belongs
    in the message because "wedged" and "wedged after everything else was tried" are
    different diagnoses for whoever reads the log.
    """
    _, _, mirror = _run_loop(tmp_path, STUB_WEDGE,
                             SRB_AGENT_MAX_WEDGES="1",
                             SRB_AGENT_MAX_SESSION_ROTATIONS="2")
    text = (mirror / "loop.log").read_text(errors="replace")
    assert "rotation 1/2" in text, f"first rotation never happened: {text}"
    assert "rotation 2/2" in text, f"second rotation never happened: {text}"
    assert (mirror / "stop-reason.txt").read_text().strip() == "infrastructure", \
        f"rotations were spent and the phase did not end: {text}"
    assert "after 2 thread rotation(s)" in text, \
        f"the ending does not say the rotations were tried: {text}"


def test_a_fast_failure_that_clears_does_not_count_against_the_next_one(tmp_path):
    """A recovery has to clear the streak, or the counter's own claim is false.

    The counter says "consecutive" in the sentence it stops the phase with, and the
    stop-reason it writes is `infrastructure` -- the verdict that says the image or
    the key is broken and the run is worth relaunching from scratch.  Neither claim
    survives a round that worked in between, so the reset has to sit where a
    successful round reaches it: `fast_fails=0` at the bottom of the round body is
    below the rc=0 branch, which has already `continue`d back to the top.  Without a
    reset the rc=0 branch reaches, five short failures anywhere in a 30-hour run end
    it, with the model working and the deadline unspent.

    This is the argument the file already makes about `wedges`, twenty lines above
    where it is made: "Putting this after the rc=0 branch would leave a success
    unable to reset it, and the counter would then pool wedges that were minutes and
    several working rounds apart into a false 'the gateway has stopped answering'."
    Every word of it is true of `fast_fails`, which is why both are asserted.

    MAX_FAST_FAILS=2 against an alternating stub, so the third round is the one that
    decides it.  Broken: round 1 counts 1/2, round 2 works and clears nothing, round
    3 counts 2/2 and the phase ends as infrastructure on its second failure ever.
    Fixed: round 2 clears it, so the alternation runs the round budget out instead.
    """
    # MIN_ROUND_SEC has to be set: the shared driver pins it to 0, and `round_sec <
    # 0` is false of every round, so with the default this fixture never reaches the
    # branch it is about and passes against the unfixed loop.
    _, logs, mirror = _run_loop(tmp_path, STUB_FAST_FAIL_THEN_WORKS,
                               SRB_AGENT_MIN_ROUND_SEC="20",
                               SRB_AGENT_MAX_FAST_FAILS="2",
                               SRB_AGENT_MAX_ROUNDS="6")
    text = (mirror / "loop.log").read_text(errors="replace")
    reason = mirror / "stop-reason.txt"
    got = reason.read_text().strip() if reason.is_file() else ""
    assert got != "infrastructure", (
        "a phase whose every other round worked was ended as an infrastructure "
        f"fault:\n{text}")
    # The cap's own sentence, which must never be reached here.  Asserted as well as
    # the stop-reason because a later edit could keep the bad arithmetic and only
    # rename the verdict.
    assert "fast failure 2/2" not in text, text
    # And it has to get past round 3 under its own steam: without this, a loop that
    # died on round 2 for some unrelated reason would also satisfy the two above.
    rounds = len(list(logs.glob("rc-*.txt")))
    assert rounds >= 5, (
        f"only {rounds} round(s) ran, so the alternation never reached the round "
        f"the counter decides:\n{text}")


def test_fast_failures_with_no_recovery_between_them_still_end_the_phase(tmp_path):
    """The other direction, which nothing tested before this pair.

    The test above asserts the cap does NOT fire.  On its own that is satisfied by a
    loop whose cap fires never -- reset the counter unconditionally every round and
    the whole suite stays green while a run with a missing key retries a usage error
    for its entire budget and reports `deadline`.  So the bound has to be seen firing
    at least once, on the input it exists for: the same failure with nothing working
    in between.

    Two rounds, both dead in 0s, no recovery: the cap must be reached and the phase
    must end as `infrastructure`, because that is what it is.
    """
    _, logs, mirror = _run_loop(tmp_path, STUB_FAST_FAIL_ALWAYS,
                               SRB_AGENT_MIN_ROUND_SEC="20",
                               SRB_AGENT_MAX_FAST_FAILS="2",
                               SRB_AGENT_MAX_ROUNDS="6")
    text = (mirror / "loop.log").read_text(errors="replace")
    assert "fast failure 2/2" in text, \
        f"two dead rounds in a row never reached the cap:\n{text}"
    reason = mirror / "stop-reason.txt"
    assert reason.is_file(), f"no stop-reason after the cap fired:\n{text}"
    assert reason.read_text().strip() == "infrastructure", \
        f"stop-reason is {reason.read_text().strip()!r}:\n{text}"
    # And it stopped rather than running the budget out.
    rounds = len(list(logs.glob("rc-*.txt")))
    assert rounds == 2, f"the cap did not stop the phase; {rounds} rounds ran:\n{text}"


def test_the_round_keeps_its_own_stderr(tmp_path):
    """The redirect must not swallow what codex says.

    ``exec 2>>`` applies to the whole script, so a round whose stderr was not
    explicitly redirected would land in loop.log -- mixing a 20k-line model
    transcript into the file that is supposed to be readable, and emptying the
    per-round log that a failing round is diagnosed from.
    """
    _, logs, mirror = _run_loop(tmp_path, STUB_DONE)
    round_err = (logs / "stderr-1.log").read_text(errors="replace")
    assert "belongs to the round" in round_err, round_err
    loop_log = (mirror / "loop.log").read_text(errors="replace")
    assert "belongs to the round" not in loop_log, loop_log


def test_a_second_phase_appends_behind_a_stamp(tmp_path):
    """A phase resumed from `docker commit` inherits the file; both are history.

    The boundary has to be visible, or the two phases' rounds read as one run whose
    round numbers restart -- which is how a resumed phase gets mistaken for a loop
    that went backwards.
    """
    _run_loop(tmp_path, STUB_DONE)
    first = (tmp_path / "mirror" / "loop.log").read_text(errors="replace")
    _run_loop(tmp_path, STUB_DONE, SRB_AGENT_FIRST_ROUND_ENDED_TURN="1")
    both = (tmp_path / "mirror" / "loop.log").read_text(errors="replace")
    assert len(both) > len(first), "second phase overwrote the first's account"
    assert both.count("srb-agent loop starting") == 2, both
    # And the terminal-state file is this phase's, not the inherited one.
    assert (tmp_path / "mirror" / "stop-reason.txt").read_text().strip() == "completed"


def test_the_budget_a_dropped_round_reports_is_the_budget_after_it(tmp_path):
    """"N still left" must be measured after the round, not before it.

    `remaining` is computed at the top of each iteration, and both drop branches used
    to print it -- after the round had spent up to MAX_ROUND_SEC of it.  So the figure
    was always one round stale, and on a fleet whose rounds all hit the cap it read as
    a budget that was not being consumed: fw07/xhigh's rounds 1, 2 and 3 each reported
    the number from before themselves.

    The loop's control was never affected; the deadline test recomputes from the clock
    every iteration.  What the stale figure cost was supervision -- it is the number a
    reader uses to decide whether a run still has time to recover, and a monitor was
    quoting it.

    Each round is compared against the figure *that same round* announced when it
    started, which the loop prints itself: "budget Bs of Rs left".  The stale reading
    is exactly R, so the assertion is that the two differ by about a round.

    A decreasing-sequence assertion looks like it would catch this and does not: the
    stale figure is recomputed at the top of every iteration, so it falls by a round
    each time too -- just one round behind.  Nothing here is compared against 600
    either: pairing the loop's own two prints keeps the test from re-deriving the
    arithmetic it is checking.

    The rounds have to cost real seconds for any of this to be measurable -- see
    STUB_SLOW_PRODUCTIVE -- so the cap is lowered to 5s and this test spends about 20
    of them in `timeout`.  It is the only one here that waits on a clock.
    """
    _, _, mirror = _run_loop(tmp_path, STUB_SLOW_PRODUCTIVE,
                             SRB_AGENT_MAX_ROUND_SEC="5")
    text = (mirror / "loop.log").read_text(errors="replace")
    announced = [int(m) for m in re.findall(r"budget \d+s of (\d+)s left", text)]
    reported = [int(m) for m in re.findall(r"(\d+)s still left", text)]
    assert len(reported) >= 3, f"expected several capped rounds to report a budget: {text}"
    assert len(announced) == len(reported), (
        f"{len(announced)} round(s) started but {len(reported)} reported a budget; "
        f"the two lists must pair up per round\n{text}"
    )
    spent = [a - r for a, r in zip(announced, reported)]
    assert all(s >= 4 for s in spent), (
        f"rounds capped at 5s reported {spent}s spent between starting and being "
        f"dropped; a round reporting 0 is printing the figure from before itself\n"
        f"announced={announced} reported={reported}\n{text}"
    )


def test_the_wedge_allowance_scales_with_the_budget(tmp_path):
    """Patience is a fraction of the deadline, not a fixed number of rounds.

    A wedge costs MAX_ROUND_SEC seconds, so a constant cap fixes the waiting-out
    budget at that many round-caps of wall however long the run is -- and the runs
    that reach it are the long ones, which have the most unspent budget to give up.
    A phase ended that way publishes a transient gateway outage as the model's score.

    Here the deadline is 200x the round cap, so the derived allowance is 30 -- and
    the four rounds this loop is allowed to take all wedge without the phase being
    called an infrastructure fault.  A constant cap of three would have ended it on
    round three.
    Passing MAX_WEDGES empty rather than omitting it because ``_run_loop`` pins it.
    Passing MAX_WEDGES empty rather than omitting it because ``_run_loop`` pins it.
    """
    _, _, mirror = _run_loop(tmp_path, STUB_WEDGE, SRB_AGENT_MAX_WEDGES="",
                             SRB_AGENT_DEADLINE_SEC="6000",
                             SRB_AGENT_MAX_ROUND_SEC="30")
    text = (mirror / "loop.log").read_text(errors="replace")
    # 15% of 6000s is 900s; at 30s a wedge that is 30, not 3.
    assert "wedge 1/30" in text, text
    # Four wedges, more than a constant cap of three allows, and the phase stands.
    assert "wedge 4/30" in text, text
    assert "treating as an infrastructure fault" not in text, text
    reason = mirror / "stop-reason.txt"
    assert not reason.is_file() or reason.read_text().strip() != "infrastructure", \
        f"a phase with 96% of its budget left was stopped as infrastructure: {text}"


def test_a_short_budget_keeps_the_old_allowance(tmp_path):
    """The floor: no task gets less patience than the constant it replaced.

    The derivation is only ever allowed to add.  A 5.7-hour task -- the shortest
    declared wall in the suite -- derives 1, so the floor has to carry it back to 3,
    and the third wedge must still end the phase exactly as before.
    """
    _, _, mirror = _run_loop(tmp_path, STUB_WEDGE, SRB_AGENT_MAX_WEDGES="",
                             SRB_AGENT_DEADLINE_SEC="600",
                             SRB_AGENT_MAX_ROUND_SEC="30",
                             SRB_AGENT_MAX_SESSION_ROTATIONS="0")
    text = (mirror / "loop.log").read_text(errors="replace")
    assert "wedge 1/3" in text, text
    assert "wedge 3/3" in text, text
    # Rotations off: this test is about the floor the derivation may not go below,
    # so the cap has to be the ending or there is nothing here to read.
    assert (mirror / "stop-reason.txt").read_text().strip() == "infrastructure"


CLAUDE_LOOP = LOOP.parent / "srb-claude-loop.sh"


def _wedge_derivation(script: Path) -> str:
    """The MAX_WEDGES block of a loop script, normalised to its arithmetic.

    Compared as text rather than run: the two loops drive different clients, so
    there is no shared harness to execute both under, but the *policy* has to be
    one policy.
    """
    text = script.read_text(encoding="utf-8")
    block = re.search(
        r"^MAX_WEDGES=.*?(?:^fi$|\n\n)", text, re.MULTILINE | re.DOTALL
    )
    assert block, f"{script.name} has no MAX_WEDGES block"
    return re.sub(r"\s+", " ", block.group(0)).strip()


def test_both_loops_derive_the_same_wedge_patience():
    """A wedge allowance must not depend on which client drove the run.

    The fraction landed on the codex loop first and the claude loop kept the flat
    constant 3, so the same transient outage ended a 30-hour claude run at minute
    90 and a 30-hour codex run at hour 4.5.  Nothing in either client makes that
    difference correct, and a score published from it reads as the model's.
    """
    assert _wedge_derivation(LOOP) == _wedge_derivation(CLAUDE_LOOP), (
        "the two loops derive MAX_WEDGES differently; a gateway outage would end "
        "one client's run and be waited out by the other's"
    )
