"""Whether the Claude loop can resume a run whose transcript outgrew the window.

The loop's whole purpose is that a failed round costs a round, not a run: it
resumes the same conversation so a dropped stream or a capacity refusal does not
restart the work from nothing.  There is one failure where resuming is the wrong
move, and it is the one that cost the most.  Once a session's transcript passes
the model's context window, ``--continue`` replays the exact request the gateway
just refused; the round fails in a second, and it *reached* the gateway on the
way, so it lands in the transient-backoff branch and waits to try again -- where
waiting cannot help, because nothing about the request changes on its own.

Measured on 2026-08-08 against dsv4-flash, whose window is 278528 tokens, read
from the raw ``events-N.jsonl`` stream -- the only source that carries every
assistant turn, so a refusal is counted once per refusal.  15 of 20 runs hit it,
each for at least 20 rounds, the worst 34 consecutive.  fw01's round 1 did 232
assistant turns over 1690s and ended on a 400 at 246529 input tokens; rounds 2
to 24 are 6677-byte replays of that refusal, one a second, for 36 minutes.

It is not a run-ending fault, and this file does not claim it is: all 15 ran
between 2 and 7 sessions, because ``--continue`` auto-compacts when the
transcript will not fit and that starts a new session -- fw01's round 25 came
back on a new id and did another 1727s of real work.  What the loop change buys
is when: a rotation on the first refusal rather than the twenty-fifth.

The transcript is also the only thing a fresh session loses -- the work is in the
repository on disk -- so the answer is a new session with the instruction re-read,
not another retry.  These tests pin that: a refusal starts a fresh session, a run
that cannot be kept inside the window ends with its own recorded reason instead of
consuming the deadline, and neither counter that ends a phase on a *fault* is
advanced by an overflow.

Driven in a container, like test_agent_loop_diagnostics.py and for the same
reason: ``/workspace/repo`` is hard-coded and this box's root filesystem belongs
to everyone.  Unlike the codex loop, this one drops to an unprivileged uid before
it runs anything, so the fixture has to leave the bind mount traversable by that
uid.  ``claude`` is a stub -- the subject is which request the loop makes next,
not what a model does inside a round.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

#: Overridable so the suite can be pointed at another copy of the loop -- which is
#: how these tests were shown to fail against the version before the fix, rather
#: than merely to pass against the version after it.
LOOP = Path(os.environ.get(
    "SRB_TEST_CLAUDE_LOOP",
    Path(__file__).resolve().parents[1] / "swerefactor" / "harness" / "srb-claude-loop.sh"))
IMAGE = os.environ.get("SRB_TEST_IMAGE", "swerefactor/infra:1")

#: Every stub records the argv it was handed, in its own file per invocation, so a
#: test can ask what the *next* request looked like -- which is the entire subject
#: here.  Arguments are NUL-separated, not newline-separated: the last argument is
#: a multi-line prompt, so a line-based format cannot be split back into the
#: arguments that produced it, and the assertion that matters most is about the
#: contents of that prompt.  The invocation number comes from counting the files
#: already written, so it needs no state of its own and cannot drift.
_RECORD = r'''
d=${SRB_AGENT_LOG_DIR:-/opt/srb-agent-logs}
n=1; while [ -e "$d/argv-$n.txt" ]; do n=$(( n + 1 )); done
: > "$d/argv-$n.txt"
for a in "$@"; do printf '%s\0' "$a" >> "$d/argv-$n.txt"; done
'''

#: The refusal, quoted from what the gateway actually returned to fw01 on round 1.
#: It arrives as a `result` event with is_error -- the client's own framing for a
#: request the API rejected -- so a loop reading the stream sees an error that
#: reached the gateway, which is precisely why the old code kept retrying it.
_CTX_REFUSAL = (
    r'{"type":"result","subtype":"error_during_execution","is_error":true,'
    r'"result":"API Error: 400 {\"error\":{\"message\":\"litellm.ContextWindowExceededError: '
    r"This model's maximum context length is 278528 tokens. However, you requested 32000 "
    r'output tokens and your prompt contains at least 246529 input tokens, for a total of at '
    r'least 278529 tokens.\"}}"}'
)

#: Every payload goes out through a quoted here-doc, never an `echo '...'`: the
#: real refusal contains "This model's maximum context length", and that
#: apostrophe closes a single-quoted string.  The stub then dies with a shell
#: syntax error, the events file is empty, and the test fails for a reason that
#: has nothing to do with the loop.
def _emit(payload: str) -> str:
    return f"cat <<'SRB_JSON'\n{payload}\nSRB_JSON\n"


#: Refuses every round: the shape of a run that cannot be kept inside the window.
STUB_ALWAYS_OVERFLOWS = f"""#!/bin/bash
{_RECORD}
{_emit(_CTX_REFUSAL)}
exit 1
"""

#: Refuses once, then works.  A fresh session is smaller than the one that
#: overflowed, so this is the ordinary case: one reset and the run carries on.
STUB_OVERFLOWS_THEN_WORKS = f"""#!/bin/bash
{_RECORD}
if [ "$n" = "1" ]; then
{_emit(_CTX_REFUSAL)}
  exit 1
fi
{_emit('{"type":"assistant","message":{"content":[{"type":"text","text":"done"}]}}')}
{_emit('{"type":"result","is_error":false,"result":"SRB_TASK_COMPLETE"}')}
printf 'changed\\n' >> /workspace/repo/f
exit 0
"""

#: Reaches the gateway and fails fast, with no overflow anywhere in it: a capacity
#: refusal that never clears.  Worth retrying -- for a while -- and worth ending
#: the phase over eventually, which is the distinction this stub exists to test.
#: Hoisted out of the f-string below: this payload contains a backslash, and
#: Python 3.10 forbids one inside an f-string expression.
_OVERLOADED = (
    r'{"type":"result","is_error":true,'
    r'"result":"API Error: 529 {\"type\":\"overloaded_error\"}"}'
)
_OVERLOADED_EMIT = _emit(_OVERLOADED)

STUB_GATEWAY_ERROR = f"""#!/bin/bash
{_RECORD}
{_OVERLOADED_EMIT}
exit 1
"""

#: Burns the whole round budget having produced nothing but an error.  This is the
#: shape that hid behind reached_gateway(): an error event is a request that did
#: not complete, so counting it as "still answering" excused a genuinely wedged
#: round and left the wedge counter at zero while the budget drained.  Reported by
#: the session working on the codex loop, which measured 14 such rounds in a row.
STUB_CAPPED_ERRORS_ONLY = f"""#!/bin/bash
{_RECORD}
{_emit('{"type":"result","is_error":true,"result":"stream disconnected before completion"}')}
/usr/bin/sleep 30
exit 0
"""

#: Burns the whole round budget mid-answer.  The counterpart: this one really was
#: answering, and stopping the phase over it once ended a working run 95 minutes
#: into a 40-hour budget.  It must not be counted.
STUB_CAPPED_MID_ANSWER = f"""#!/bin/bash
{_RECORD}
{_emit('{"type":"assistant","message":{"content":[{"type":"text","text":"working"}]}}')}
/usr/bin/sleep 30
exit 0
"""


#: Fails, then works, then fails, then works.  The shape of a real gateway wobble:
#: a 529 clears on its own, the next round resumes the session and does real work.
#: Every fixture above this one fails FOREVER, which is why they only ever exercise
#: the give-up bounds; none of them can see what happens to a counter after a
#: recovery.  The odd rounds reach the gateway and produce nothing (the transient
#: branch); the even ones answer and change the tree, so the idle bound stays clear.
STUB_GATEWAY_ERROR_THEN_WORKS = f"""#!/bin/bash
{_RECORD}
if [ $(( n % 2 )) = "1" ]; then
{_OVERLOADED_EMIT}
  exit 1
fi
{_emit('{"type":"assistant","message":{"model":"glm-5.2","content":[{"type":"text","text":"ported a module"}]}}')}
printf 'work-%s\\n' "$n" >> /workspace/repo/f
exit 0
"""

#: The same alternation for the OTHER counter.  A round that never reaches the
#: gateway at all (empty stream, immediate exit) is a "fast fail", and five of them
#: end the phase as an infrastructure fault -- a verdict that says the run is
#: unrunnable and worth relaunching from a clean image.  Interleaved with real work,
#: that verdict is wrong about the run.
STUB_FAST_FAIL_THEN_WORKS = f"""#!/bin/bash
{_RECORD}
if [ $(( n % 2 )) = "1" ]; then
  echo "connection refused" >&2
  exit 1
fi
{_emit('{"type":"assistant","message":{"model":"glm-5.2","content":[{"type":"text","text":"ported a module"}]}}')}
printf 'work-%s\\n' "$n" >> /workspace/repo/f
exit 0
"""


#: The stall that waiting cannot fix, and the reason the gateway branch grew a
#: rotation.  Measured on lang04 (claude-sonnet-5, effort max, 2026-08-10): 43
#: consecutive rounds, each re-sending ~151K tokens, each thinking ~130s, each
#: coming back as ONE assistant event whose only block is a zero-length signed
#: thinking block, then the CLI's own `Connection closed mid-response`.  No work
#: means the transcript does not grow, so the next round sends the same request and
#: gets the same answer: a fixed point, not a wobble.  Every stub above this one
#: either clears on its own or fails forever no matter what is asked -- neither can
#: show a loop escaping by *changing the request*, which is what this one is for.
#:
#: It answers `--continue` with the stall and a fresh session with real work, which
#: is the measured asymmetry: a probe against the same container, gateway, model and
#: effort returned in 6.6s at 45537 cache_creation tokens while the run's own rounds
#: died at 151K.
_STALL = (
    '{"type":"assistant","message":{"model":"claude-sonnet-5","content":'
    '[{"type":"thinking","thinking":"","signature":"REDACTED"}]}}'
)
_CONN_CLOSED = (
    r'{"type":"result","is_error":true,'
    r'"result":"Connection closed mid-response"}'
)
#: Keyed on the prompt rather than on --continue, because round 1 carries neither:
#: it hands the bare instruction, so a stub that keys on the resume flag would treat
#: round 1 as the fresh session and finish before stalling once.  Matching the
#: gateway cause sentence also pins the other half of the fix -- that a session
#: rotated off a stall is told THAT, and not that it outgrew a window it never
#: reached.
_GW_CAUSE_MARKER = "its requests stopped completing"
STUB_STALLS_UNTIL_FRESH_SESSION = f"""#!/bin/bash
{_RECORD}
if ! printf '%s' "${{@: -1}}" | grep -qF {_GW_CAUSE_MARKER!r}; then
{_emit(_STALL)}
{_emit(_CONN_CLOSED)}
  exit 1
fi
{_emit('{"type":"assistant","message":{"model":"claude-sonnet-5","content":[{"type":"text","text":"read the tree, ported a module"}]}}')}
printf 'work-%s\\n' "$n" >> /workspace/repo/f
{_emit('{"type":"result","is_error":false,"result":"SRB_TASK_COMPLETE"}')}
exit 0
"""

#: Stalls, recovers on a rotation, works, then stalls again -- the shape a long run
#: reaches by construction, because a fresh session starts small and grows back to
#: the size that stalled it.  Every OTHER stall fixture is monotone (fails for ever,
#: or works for ever after one rotation), so none of them can see whether the
#: rotation cap counts per-stall or per-run.  Work rounds exit rc=1 here on purpose:
#: lang04's 99 rounds were 96 rc=1 and 3 rc=124, with no rc=0 at all, so a counter
#: reset placed in the rc=0 block is unreachable for exactly this run shape.
STUB_STALLS_RECOVERS_STALLS = f"""#!/bin/bash
{_RECORD}
if [ "$n" -le 2 ] || [ "$n" -ge 6 ]; then
{_emit(_STALL)}
{_emit(_CONN_CLOSED)}
  exit 1
fi
{_emit('{"type":"assistant","message":{"model":"claude-sonnet-5","content":[{"type":"text","text":"ported a module"}]}}')}
printf 'work-%s\\n' "$n" >> /workspace/repo/f
exit 1
"""

#: The same stall, on every request, fresh sessions included.  A gateway that
#: refuses a 45K request too is genuinely down, and that is what the give-up path
#: is for -- so rotation must be bounded and must not postpone the verdict forever.
STUB_STALLS_FOREVER = f"""#!/bin/bash
{_RECORD}
{_emit(_STALL)}
{_emit(_CONN_CLOSED)}
exit 1
"""


def _docker_ok() -> bool:
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "image", "inspect", IMAGE],
                          capture_output=True).returncode == 0


pytestmark = pytest.mark.skipif(
    not _docker_ok(), reason=f"needs docker and {IMAGE}")


class Phase:
    """One finished agent phase, asked questions rather than grepped."""

    def __init__(self, rc: int, logs: Path, stderr: str):
        self.rc, self.logs, self.launcher_stderr = rc, logs, stderr
        self.log = (logs / "loop.log").read_text(errors="replace") if (logs / "loop.log").exists() else ""

    @property
    def stop_reason(self) -> str:
        f = self.logs / "stop-reason.txt"
        return f.read_text().strip() if f.exists() else ""

    def calls(self) -> list[list[str]]:
        """The argv of each claude invocation, in order."""
        out = []
        for i in range(1, 400):
            f = self.logs / f"argv-{i}.txt"
            if not f.exists():
                break
            # Each argument is NUL-terminated, so the split leaves a trailing empty
            # element -- not an argument, just the last terminator.
            parts = f.read_bytes().split(b"\0")
            out.append([a.decode(errors="replace") for a in parts[:-1]])
        return out


def _run_loop(tmp_path: Path, stub: str, **env) -> Phase:
    """Run the real script in a container; return the phase it produced."""
    logs, mirror, bin_ = tmp_path / "logs", tmp_path / "mirror", tmp_path / "bin"
    for d in (logs, mirror, bin_, tmp_path / "claude-home"):
        d.mkdir(parents=True, exist_ok=True)
    (bin_ / "claude").write_text(stub, encoding="utf-8")
    (bin_ / "claude").chmod(0o755)
    # Copied and made executable rather than bind-mounted from the repo, where it
    # is 0644.  The loop re-execs itself through setpriv to drop privileges, and
    # that runs $0 directly, so the execute bit is load-bearing -- harbor_claude.py
    # chmods 0755 after uploading it for exactly this reason.  Copying keeps the
    # test from having to change the file's mode in the tree to run.
    loop = tmp_path / "loop.sh"
    loop.write_bytes(LOOP.read_bytes())
    loop.chmod(0o755)
    (tmp_path / "instruction.md").write_text(
        "Port the repository. Emit SRB_TASK_COMPLETE when done.\n", encoding="utf-8")
    (tmp_path / "instruction.md").chmod(0o644)
    # This loop drops to an unprivileged uid before it runs anything, and a bind
    # mount created by mktemp is 0700.  Without this the stub is unreachable and
    # every round fails for a reason that has nothing to do with the test.
    for d in (tmp_path, logs, mirror, bin_, tmp_path / "claude-home"):
        d.chmod(0o755)

    settings = {
        "SRB_AGENT_LOG_DIR": "/t/logs",
        "SRB_AGENT_MIRROR_DIR": "/t/mirror",
        "SRB_INSTRUCTION": "/t/instruction.md",
        "CLAUDE_HOME": "/t/claude-home",
        "SRB_AGENT_UID": "1000",
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

    # `sleep 0` replaces the loop's own backoffs, which are 60-120s and would make
    # this a test nobody runs.  Installed at /usr/local/bin, which precedes
    # /usr/bin on PATH; the stubs that need a real delay call /usr/bin/sleep by
    # absolute path, so the two do not collide.
    proc = subprocess.run(
        ["docker", "run", "--rm", "--network", "none",
         "-v", f"{tmp_path}:/t",
         *flags, "--entrypoint", "bash", IMAGE, "-c",
         "mkdir -p /workspace/repo "
         "&& printf 'x' > /workspace/repo/f "
         "&& printf '#!/bin/sh\\nexit 0\\n' > /usr/local/bin/sleep "
         "&& chmod 755 /usr/local/bin/sleep "
         "&& export PATH=/t/bin:$PATH && bash /t/loop.sh"],
        capture_output=True, text=True, timeout=600)
    return Phase(proc.returncode, logs, proc.stderr)


def test_an_overflow_starts_a_fresh_session_instead_of_replaying_it(tmp_path):
    """The defect itself: round 2 must not resume the session round 1 killed.

    Resuming is what the loop does for every other failure and it is right to.  A
    context-window refusal is the exception, and the whole cost of getting it
    wrong was 23 identical rounds against a request the gateway had already
    refused once.
    """
    p = _run_loop(tmp_path, STUB_OVERFLOWS_THEN_WORKS)
    calls = p.calls()
    assert len(calls) >= 2, f"expected a second round; loop.log:\n{p.log}"

    assert "--continue" not in calls[1], (
        "round 2 resumed the session that had just overflowed:\n"
        + " ".join(calls[1][:12]))
    assert "starts a fresh session" in p.log, p.log

    # Every other flag is passed through: a fresh session must not quietly become
    # a differently-configured measurement.
    for flag in ("-p", "--verbose", "--output-format", "stream-json",
                 "--permission-mode", "bypassPermissions"):
        assert flag in calls[1], f"{flag} lost on the fresh round: {calls[1][:12]}"

    prompt = calls[1][-1]
    assert "grew past the model context window" in prompt, prompt[:400]
    assert "Port the repository." in prompt, (
        "a fresh session was not re-handed the instruction, so it has no task:\n"
        + prompt[-400:])
    assert "Continue from where you left off" not in prompt, (
        "the fresh round was handed the resume prompt, which tells a model with no "
        "memory to carry on from a plan it cannot remember:\n" + prompt[:400])

    assert p.stop_reason == "completed", p.log


def test_a_run_that_cannot_stay_inside_the_window_ends_with_its_own_reason(tmp_path):
    """Bounded, and recorded as what it was.

    The alternative is what happened on 08-08: the phase spends its whole
    deadline, then reports "deadline", and the run is indistinguishable from one
    where a model simply worked slowly.
    """
    p = _run_loop(tmp_path, STUB_ALWAYS_OVERFLOWS,
                  SRB_AGENT_MAX_CTX_RESETS="2", SRB_AGENT_MAX_ROUNDS="8")
    assert p.stop_reason == "context-window", f"{p.stop_reason!r}\n{p.log}"
    assert "(reset 2/2)" in p.log, p.log
    assert len(p.calls()) == 2, f"stopped late: {len(p.calls())} rounds\n{p.log}"


def test_an_overflow_is_not_charged_to_the_fault_counters(tmp_path):
    """A refusal for size is not a harness fault, and must not end the phase as one.

    Both counters that end a phase early key off a short round, and an overflow
    round is short.  If either one counted it, a run would be reported as an
    infrastructure fault -- unrunnable, worth retrying from a clean image -- when
    in fact the model was working and the transcript simply filled up.
    """
    p = _run_loop(tmp_path, STUB_ALWAYS_OVERFLOWS,
                  SRB_AGENT_MIN_ROUND_SEC="20", SRB_AGENT_MAX_FAST_FAILS="2",
                  SRB_AGENT_MAX_GW_STALLS="2", SRB_AGENT_MAX_CTX_RESETS="4",
                  SRB_AGENT_MAX_ROUNDS="8")
    assert p.stop_reason == "context-window", f"{p.stop_reason!r}\n{p.log}"
    assert "fast fail" not in p.log, p.log
    assert "reached the gateway" not in p.log, p.log


def test_a_gateway_that_never_clears_is_bounded_and_named(tmp_path):
    """A gateway that never clears is bounded, and the stop names it.

    A capacity refusal deserves patience, so this stays a wait rather than a fault
    count -- but an error that repeats identically for ever is not transient, and
    without a bound one round can hold a 40-hour deadline at zero progress.
    """
    p = _run_loop(tmp_path, STUB_GATEWAY_ERROR,
                  SRB_AGENT_MIN_ROUND_SEC="20", SRB_AGENT_MAX_GW_STALLS="2",
                  SRB_AGENT_MAX_ROUNDS="8")
    assert p.stop_reason == "gateway", f"{p.stop_reason!r}\n{p.log}"
    assert "(2/2)" in p.log, p.log
    # It kept resuming while it waited: patience is the point of the branch.
    assert "--continue" in p.calls()[1]


def test_a_capped_round_that_only_errored_is_counted_as_a_wedge(tmp_path):
    """An error event is a request that did not complete.

    reached_gateway() matches error events, so a wedge branch that asked only whether
    the gateway had been reached would excuse a round that spent its entire budget
    failing as "still answering", and the wedge counter would never move.
    """
    p = _run_loop(tmp_path, STUB_CAPPED_ERRORS_ONLY,
                  SRB_AGENT_MAX_ROUND_SEC="2", SRB_AGENT_MAX_WEDGES="2",
                  SRB_AGENT_MAX_ROUNDS="8")
    assert "(wedge 1/2)" in p.log, p.log
    assert p.stop_reason == "infrastructure", f"{p.stop_reason!r}\n{p.log}"


def test_a_capped_round_that_answered_is_still_not_a_wedge(tmp_path):
    """The other half, which the fix must not break.

    Counting a round that was mid-answer is what stopped a working phase 95
    minutes into a 40-hour budget.  A round with real output is dropped and
    resumed, and the counter stays where it was.
    """
    p = _run_loop(tmp_path, STUB_CAPPED_MID_ANSWER,
                  SRB_AGENT_MAX_ROUND_SEC="2", SRB_AGENT_MAX_WEDGES="2",
                  SRB_AGENT_MAX_ROUNDS="3")
    assert "not counted as a wedge" in p.log, p.log
    assert "(wedge " not in p.log, p.log
    # "max-rounds", not "": this asserted an EMPTY stop reason until the loop began
    # naming the round-budget exit, because a missing reason reads downstream as "the
    # harness never finished" rather than "the budget ran out".  The claim here is
    # unchanged -- the phase ended by exhausting its 3 rounds, not as a fault.
    assert p.stop_reason == "max-rounds", (
        f"phase ended as {p.stop_reason!r} rather than running out its rounds\n{p.log}")


def test_a_gateway_error_that_clears_does_not_count_against_the_next_one(tmp_path):
    """Recovery has to clear the counter, or patience is spent cumulatively.

    Every other fixture in this file fails for ever, so the give-up bounds are the
    only thing they can measure.  This is the ordinary case instead -- a wobble that
    clears -- and it is the one the campaign runs on: two campaigns (opus 70 rounds,
    glm-5.2 87 rounds) recorded 331 resumed rounds and not one gateway error, so this
    branch has never run outside a test.

    ``wedges`` is reset unconditionally on the good path (``rc != 124``), one line
    before the branches.  ``gw_stalls`` and ``fast_fails`` are reset only *inside*
    the failure branches, and a successful round returns to the top of the loop
    before reaching either -- so an error every other round accumulates toward a
    bound that is documented, and logged, as counting CONSECUTIVE rounds.  A run
    that recovers 25 times would then be reported as a gateway the harness gave up
    on, with its remaining budget unspent.
    """
    p = _run_loop(tmp_path, STUB_GATEWAY_ERROR_THEN_WORKS,
                  SRB_AGENT_MIN_ROUND_SEC="20", SRB_AGENT_MAX_GW_STALLS="2",
                  SRB_AGENT_MAX_ROUNDS="6")
    calls = p.calls()
    # The premise: the stub really did alternate, and the good rounds really worked.
    assert len(calls) >= 4, f"only {len(calls)} rounds ran\n{p.log}"
    assert "--continue" in calls[1], "the round after an error did not resume"

    assert p.stop_reason != "gateway", (
        "a gateway error that cleared was still counted against the bound; the run "
        f"ended as {p.stop_reason!r} with the model working:\n{p.log}")
    assert "(2/2)" not in p.log, (
        "the counter reached its bound across non-consecutive errors:\n" + p.log)


def test_a_fast_failure_that_clears_does_not_count_against_the_next_one(tmp_path):
    """The same defect on the counter that ends a phase as an *infrastructure* fault.

    This one is the more reachable of the two: the bound is 5, not 25, so five short
    failures anywhere in a 30-hour run end it -- and "infrastructure" is the verdict
    that says the image is broken and the run is worth relaunching, which is a false
    statement about a run whose model kept working in between.
    """
    p = _run_loop(tmp_path, STUB_FAST_FAIL_THEN_WORKS,
                  SRB_AGENT_MIN_ROUND_SEC="20", SRB_AGENT_MAX_FAST_FAILS="2",
                  SRB_AGENT_MAX_ROUNDS="6")
    calls = p.calls()
    assert len(calls) >= 4, f"only {len(calls)} rounds ran\n{p.log}"

    assert p.stop_reason != "infrastructure", (
        "a fast failure that cleared was still charged to the fault budget; the run "
        f"ended as {p.stop_reason!r} while the model was working:\n{p.log}")
    assert "(fast fail 2/2)" not in p.log, (
        "the fault counter reached its bound across non-consecutive failures:\n"
        + p.log)


def test_a_gateway_stall_rotates_the_session_before_giving_up(tmp_path):
    """Waiting is not the only move, and on the measured shape it is the wrong one.

    The stall this covers is a fixed point: the request that fails is re-sent
    unchanged every round, because failing produced no work and so grew no
    transcript.  lang04 spent 43 rounds -- 1.55h -- in it, and the loop's only
    response was to keep waiting until MAX_GW_STALLS.  Patience is the correct
    response to a capacity blip and is kept; it just cannot be the ONLY one, since
    nothing about the request changes while the loop waits.
    """
    p = _run_loop(tmp_path, STUB_STALLS_UNTIL_FRESH_SESSION,
                  SRB_AGENT_MIN_ROUND_SEC="20", SRB_AGENT_GW_STALL_ROTATE="3",
                  SRB_AGENT_MAX_GW_STALLS="20", SRB_AGENT_MAX_ROUNDS="8")
    calls = p.calls()
    assert len(calls) >= 4, f"only {len(calls)} rounds ran\n{p.log}"

    # Rounds 1-3 stall and are resumed; round 4 is the rotation.
    assert "--continue" in calls[2], (
        "the loop stopped resuming before it reached its rotation threshold; "
        "patience is still the first response:\n" + " ".join(calls[2][:12]))
    assert "--continue" not in calls[3], (
        f"round 4 resumed the session that had stalled 3 times:\n{p.log}")
    assert "rotating to a fresh session" in p.log, p.log

    # And the rotation is what ended it: the stub answers a fresh session with work.
    assert p.stop_reason == "completed", (
        f"the run did not recover through the rotation; ended {p.stop_reason!r}\n{p.log}")


def test_a_rotated_session_is_told_the_gateway_not_the_context_window(tmp_path):
    """The cause handed to the model has to be the cause that happened.

    The fresh-session prompt existed for one caller and asserted its cause in the
    first sentence.  Reused for a stall, that sentence becomes false, and falsely
    specific: a model told its transcript was too long will start reading and
    writing less to stay small -- the opposite of what it should do, since the
    request that failed was inside the window and the tree is what it must read to
    find out what it already did.
    """
    p = _run_loop(tmp_path, STUB_STALLS_UNTIL_FRESH_SESSION,
                  SRB_AGENT_MIN_ROUND_SEC="20", SRB_AGENT_GW_STALL_ROTATE="2",
                  SRB_AGENT_MAX_GW_STALLS="20", SRB_AGENT_MAX_ROUNDS="6")
    fresh = [c for c in p.calls()[1:] if "--continue" not in c]
    assert fresh, f"no fresh session was started at all\n{p.log}"
    prompt = fresh[0][-1]

    assert _GW_CAUSE_MARKER in prompt, (
        "the rotated session was not told why its conversation ended:\n" + prompt[:400])
    assert "context window" not in prompt, (
        "a session rotated off a gateway stall was told it outgrew the context "
        "window, which it never did:\n" + prompt[:400])
    # The rest of the prompt is what makes a fresh session usable, and is shared with
    # the overflow caller: the work is on disk, look before you touch it.
    assert "/workspace/repo" in prompt and "start over" in prompt, prompt[:400]
    assert "Port the repository" in prompt, "the instruction was not re-handed:\n" + prompt[:400]


def test_rotation_is_bounded_and_still_names_the_gateway(tmp_path):
    """A gateway that refuses a small request too is down, and must be reported so.

    Rotation costs the model its memory, so it cannot be retried indefinitely: two
    attempts prove or disprove that context size was the constraint, and after that
    the honest verdict is the one the branch already had.  Without a bound here the
    fix would convert a reportable `gateway` into an unbounded loop that ends on the
    round cap instead -- which publishes as a blank row rather than a named fault.
    """
    p = _run_loop(tmp_path, STUB_STALLS_FOREVER,
                  SRB_AGENT_MIN_ROUND_SEC="20", SRB_AGENT_GW_STALL_ROTATE="2",
                  SRB_AGENT_MAX_GW_ROTATIONS="2", SRB_AGENT_MAX_GW_STALLS="3",
                  SRB_AGENT_MAX_ROUNDS="14")
    assert p.stop_reason == "gateway", (
        f"a permanently stalled gateway ended as {p.stop_reason!r}\n{p.log}")
    assert "gateway rotation 1/2" in p.log and "gateway rotation 2/2" in p.log, p.log
    assert "gateway rotation 3/2" not in p.log, "rotation was not bounded:\n" + p.log
    # The give-up line has to say a fresh session was tried, or the next reader
    # repeats the experiment by hand.
    assert "including after 2 fresh session(s)" in p.log, p.log


def test_a_rotation_that_bought_work_is_not_charged_to_the_next_stall(tmp_path):
    """The cap is per stall, not per run, because this stall recurs by construction.

    A fresh session starts small and grows back to the size that stalled it -- lang04
    reached ~151K over ~50 rounds -- so a long healthy run meets this branch several
    times legitimately.  Counted cumulatively, a 2-rotation cap ends the third onset
    as `gateway`: a fault verdict, and a blank row, for a run that was working
    between the stalls.

    The reset has to sit outside the rc=0 block to be reachable at all.  lang04's 99
    rounds were 96 rc=1 and 3 rc=124 -- not one rc=0 -- because its work rounds ended
    nonzero, having produced real work before the stream broke.  This stub's work
    rounds exit 1 for that reason.
    """
    p = _run_loop(tmp_path, STUB_STALLS_RECOVERS_STALLS,
                  SRB_AGENT_MIN_ROUND_SEC="20", SRB_AGENT_GW_STALL_ROTATE="2",
                  SRB_AGENT_MAX_GW_ROTATIONS="1", SRB_AGENT_MAX_GW_STALLS="20",
                  SRB_AGENT_MAX_ROUNDS="10")
    # Rounds 1-2 stall, round 3 rotates and works, 4-5 work, 6+ stall again.  With a
    # cap of 1 counted per-run the second onset can never rotate; per-stall it can.
    assert p.log.count("rotating to a fresh session") >= 2, (
        "the second onset of the same stall could not rotate: the cap is being "
        "counted per run rather than per stall\n" + p.log)
    assert "gateway rotation 1/1" in p.log, p.log
    # And the counter really was reset, rather than the cap being ignored.
    assert "gateway rotation 2/1" not in p.log, p.log


#: An init event, as the client's first line, offering the tools named.  The guard
#: reads that line and nothing else, so it is the whole fixture.
def _init(*tools: str) -> str:
    listed = ",".join(f'"{t}"' for t in tools)
    return ('{"type":"system","subtype":"init","cwd":"/workspace/repo",'
            f'"tools":[{listed}]}}')


#: Offers a web tool the deny should have removed: a client that ignored it, or a
#: gateway that answered with one anyway.
STUB_OFFERS_WEB_TOOL = f"""#!/bin/bash
{_RECORD}
{_emit(_init("Bash", "Edit", "WebSearch"))}
{_emit('{"type":"result","is_error":false,"result":"SRB_TASK_COMPLETE"}')}
printf 'changed\\n' >> /workspace/repo/f
exit 0
"""

#: The same round with the deny in force -- the control the guard must not fire on.
STUB_OFFERS_NO_WEB_TOOL = f"""#!/bin/bash
{_RECORD}
{_emit(_init("Bash", "Edit", "Read"))}
{_emit('{"type":"result","is_error":false,"result":"SRB_TASK_COMPLETE"}')}
printf 'changed\\n' >> /workspace/repo/f
exit 0
"""


def test_the_loop_denies_the_web_tools_by_name(tmp_path):
    """The deny reaches the real command line, as one bare name per tool.

    Bare rather than scoped: a scoped rule refuses calls, while a bare name keeps
    the tool out of what the model is offered at all.
    """
    p = _run_loop(tmp_path, STUB_OFFERS_NO_WEB_TOOL)
    assert p.calls(), f"the loop never invoked claude (rc={p.rc})\n{p.log}"
    argv = p.calls()[0]
    assert "--disallowed-tools" in argv, argv
    denied = argv[argv.index("--disallowed-tools") + 1].split(",")
    assert set(denied) == {"WebSearch", "WebFetch"}, denied
    assert all("(" not in d for d in denied), (
        f"{denied}: a scoped rule only refuses calls; a bare name is what keeps "
        f"the tool out of the offer"
    )


def test_a_round_offered_a_web_tool_ends_the_phase(tmp_path):
    """A round the deny did not reach is not a measurement of an offline agent.

    The init event names what the round was actually offered.  Unread, the phase
    scores as though the agent worked from the repository alone.
    """
    p = _run_loop(tmp_path, STUB_OFFERS_WEB_TOOL)
    assert p.stop_reason == "web-tools", (
        f"a round offered WebSearch ended as {p.stop_reason!r}\n{p.log}")
    assert "was offered a web tool" in p.log, p.log
    # One round, not four: the phase stops rather than accumulating rounds whose
    # egress cannot be accounted for.
    assert len(p.calls()) == 1, f"{len(p.calls())} rounds ran after the ending"


def test_a_clean_round_is_not_stopped_by_the_guard(tmp_path):
    """The guard's passing state has to be reachable, or it stops every phase."""
    p = _run_loop(tmp_path, STUB_OFFERS_NO_WEB_TOOL)
    assert p.stop_reason == "completed", (
        f"a round offered no web tool ended as {p.stop_reason!r}\n{p.log}")
    assert "was offered a web tool" not in p.log, p.log
