#!/bin/bash
# The agent phase, supervised from inside the container.
#
# Round 1 hands the harness instruction.md.  Every later round resumes the same
# session, which is what keeps a 20k-line rewrite from restarting from nothing
# each time the gateway drops a stream.
#
# The continuation prompt is deliberately content-free.  It says "keep going" and
# nothing about what has been graded, what a gate is, or what remains untested --
# the task's ground rule is that the agent gets the goal and not the rubric, and
# a supervisor that nudges with "the renderers still fail" has handed over the
# rubric one hint at a time.  It is also not a hint that anything is wrong: an
# interrupted round is an interrupted round, not a verdict.
#
# Three things here were learned the hard way on the first real run:
#
#  1. `codex exec resume` does not accept -C.  That flag exists on `codex exec`
#     only; passing it to resume is a clap usage error, exit 2, before any model
#     call.  The cd above the loop is what sets the working directory, and resume
#     filters recorded sessions by cwd anyway.
#
#  2. --last is the wrong selector.  A long round spawns subagent threads, each
#     with its own rollout file and a more recent mtime than the root thread's.
#     --last picks the newest recording, which is usually a subagent -- so the
#     resume would continue the wrong conversation.  The root thread is the one
#     whose session_meta has no forked_from_id and source "exec"; we resume it by
#     id.
#
#  3. The event stream must not live on a filesystem someone else can fill.
#     Round 1 died mid-turn with turn_aborted/interrupt because codex's stdout
#     was a bind mount on a full disk.  It is written inside the container now
#     (the writable layer has room) and mirrored out to the bind mount, so a full
#     host /tmp costs log freshness instead of the run.
set -uo pipefail

INSTRUCTION="${SRB_INSTRUCTION:-/opt/srb-agent/instruction.md}"
# The bind mount, which may be on a disk someone else can fill.  Overridable so
# a resumed phase can mirror into a subdirectory instead of overwriting the
# event stream of the round it is resuming.
MIRROR_DIR="${SRB_AGENT_MIRROR_DIR:-/logs/agent}"
LOG_DIR="${SRB_AGENT_LOG_DIR:-/opt/srb-agent-logs}"   # container-local, safe
DEADLINE_SEC="${SRB_AGENT_DEADLINE_SEC:-144000}"
MAX_ROUNDS="${SRB_AGENT_MAX_ROUNDS:-400}"
MIN_ROUND_SEC="${SRB_AGENT_MIN_ROUND_SEC:-20}"
MAX_FAST_FAILS="${SRB_AGENT_MAX_FAST_FAILS:-5}"
# The longest a *single* round may hold the phase.  Distinct from the deadline,
# and the distinction is the whole point: `timeout "$remaining"` gives one round
# the entire remaining budget, so anything that hangs instead of failing holds
# the phase until the deadline and the loop cannot break out, because it only
# ever branches on an exit code that never arrives.
#
# Measured on pf02 run 3, where this cost 911s before I killed the round by
# hand.  kimi-k3's SSE stream dropped 24s into round 16; codex then waited its
# full `stream_idle_timeout_ms` (600s) before even calling it an idle timeout,
# and `stream_max_retries = 10` was queued up behind that -- each retry
# re-sending the whole prompt to a server measured at 41.7s to first byte for an
# 8-token completion.  Left alone that round would have burned ~100 minutes
# without emitting one event, and the phase would have looked alive throughout:
# the loop logs nothing between rounds, so a supervisor watching stdout sees the
# same silence a long think produces.
#
# 1800s against a measured healthy maximum of 287s over 15 rounds (median 70s),
# so the cap cannot truncate a round that is merely slow -- a wedge costs 30
# minutes instead of the phase.
MAX_ROUND_SEC="${SRB_AGENT_MAX_ROUND_SEC:-1800}"
# Consecutive wedged rounds before the phase gives up on the gateway.
#
# This used to be the constant 3, described as "90 minutes of nothing, which is
# past any transient worth waiting out".  That measured patience in the wrong
# unit.  A wedge costs MAX_ROUND_SEC seconds, so a *count* fixes the waiting-out
# budget at 90 minutes no matter how much of the run is left -- and the runs that
# hit it were the long ones, where 90 minutes is nothing.  On the 2026-08-07 sol
# fleet it ended 14 of 110 phases, every one of them with the same three lines:
# three rounds of rc=124 having reached the gateway zero times, then
# "infrastructure" written with 140280s -- 39 hours -- still unspent.  Those runs
# were graded, so a transient outage at minute 20 was published as the model's
# score.
#
# So the allowance is a fraction of the budget instead: about 15% of the declared
# deadline may be spent waiting for a gateway that is not answering.  A 40-hour
# task tolerates ~6 hours of outage, a 6-hour task still gets the old 90 minutes,
# and no task gets less than before -- the floor is the previous constant.
#
# Spending budget on an outage is the right trade in this direction.  The counter
# resets on any round that produces work (see the rc=0 branch), so reaching the
# cap means the gateway answered nothing for that whole span; and the deadline is
# checked before each round regardless, so a genuinely dead endpoint still ends
# the phase -- as "deadline", with the hours it actually waited on the record,
# rather than as an infrastructure fault 39 hours early.
MAX_WEDGES="${SRB_AGENT_MAX_WEDGES:-}"
if [ -z "$MAX_WEDGES" ]; then
    MAX_WEDGES=$(( (DEADLINE_SEC * 15 / 100) / MAX_ROUND_SEC ))
    [ "$MAX_WEDGES" -lt 3 ] && MAX_WEDGES=3
fi
# How many times the phase may retire a full thread and start a new one.  A
# thread that has run out of context cannot be resumed -- every resume re-sends
# the same over-long prompt -- so the only way forward is a new thread, and the
# only way to bound that is a count.  Three, because a thread filled after 15
# rounds here, so three covers a 10-hour budget; and because a task whose
# instruction alone overflows the window would otherwise rotate until the
# deadline, each rotation looking like fresh progress.
MAX_SESSION_ROTATIONS="${SRB_AGENT_MAX_SESSION_ROTATIONS:-3}"
# What the harness has to say for the phase to stop; see the rc=0 branch below.
DONE_SENTINEL="${SRB_AGENT_DONE_SENTINEL:-SRB_TASK_COMPLETE}"
# Turns that end cleanly, say nothing, and change no file before we accept that
# the work is over anyway.
MAX_IDLE_TURNS="${SRB_AGENT_MAX_IDLE_TURNS:-3}"

mkdir -p "$LOG_DIR" "$MIRROR_DIR" 2>/dev/null

# A phase that is restarted from a `docker commit` of an earlier one starts with
# that phase's log directory already in place -- including its terminal-state
# files.  On pf02's second kimi-k3 run that meant `stop-reason.txt` said
# "completed" and `finished-at.txt` held a timestamp from 16 minutes before the
# container existed, and `mirror()` copied both to the host on every round.  A
# supervisor reading the mirror to decide whether to grade would have graded a
# tree that was still being written.
#
# These two files are the only ones that assert the phase is over, and only this
# process may write them, so they are cleared before the first round.  Per-round
# files are left alone: they are overwritten by the round that owns them, and
# what is left of a previous run is history rather than a claim about this one.
rm -f "$LOG_DIR/stop-reason.txt" "$LOG_DIR/finished-at.txt" \
      "$MIRROR_DIR/stop-reason.txt" "$MIRROR_DIR/finished-at.txt" 2>/dev/null

# Everything this loop says about itself, into a file the mirror carries out.
#
# Harbor runs this script as a setup command and captures its output into its own
# machinery, which never reaches the trial directory -- so the round-by-round
# account went nowhere.  That is the only record of *why* a phase ended: which
# rounds were dropped and resumed, how many wedges accumulated, whether the
# sentinel was seen or the deadline hit.  Without it a finished run's agent/ holds
# events-N.jsonl and rc-N.txt and no explanation, and answering "is this run
# wedged or working?" needs `docker exec` into a container that will not exist once
# the phase is over.
#
# Appended, and stamped: a phase restarted from a `docker commit` inherits this
# file, and the two phases' accounts are both history worth keeping as long as the
# boundary between them is visible.  Only stop-reason.txt and finished-at.txt
# assert the phase is over, and those are still cleared above.
exec 2>> "$LOG_DIR/loop.log"
echo "=== srb-agent loop starting $(date -u '+%Y-%m-%dT%H:%M:%SZ') pid=$$ ===" >&2

START="${SRB_AGENT_START:-$(date +%s)}"
CONTINUE_PROMPT='Continue from where you left off. The previous round ended before you were finished, for reasons on our side and not yours: the connection to the model dropped. Nothing about your work has been reviewed or judged. Pick up your own plan and keep working until you consider the job done.'

# The other way a round can end: cleanly, because the turn ran out of tool calls
# rather than out of work.  It needs its own prompt, because telling an agent the
# connection dropped when it did not is a lie it can check -- and because this is
# the only place the harness can state how to stop.  Still no rubric: it says how
# to end the phase, not what is wrong with the tree.
TURN_ENDED_PROMPT="Your last turn ended without a tool call, so the harness stopped it there. That is a turn boundary and not a verdict -- nothing about your work has been reviewed or judged, and no deadline has passed.

If you are genuinely finished, reply with the single line ${DONE_SENTINEL} and nothing else. That is the only signal that ends this phase; a turn that simply stops talking will be resumed like this one.

Otherwise pick up your own plan and keep working."

cd /workspace/repo || exit 1

# The root thread of this container's run: session_meta with no forked_from_id.
# Empty on the first round, which is what tells us to start rather than resume.
root_session() {
    local f
    for f in $(find "${CODEX_HOME:-/opt/codex-home}/sessions" -name '*.jsonl' \
               -printf '%T@ %p\n' 2>/dev/null | sort -n | cut -d' ' -f2); do
        head -1 "$f" 2>/dev/null \
          | grep -q '"type":"session_meta"' || continue
        head -1 "$f" 2>/dev/null | grep -q '"forked_from_id"' && continue
        head -1 "$f" 2>/dev/null \
          | grep -o '"session_id":"[^"]*"' | head -1 | cut -d'"' -f4
        return 0
    done
    return 0
}

# Retire the current thread so the next round starts a new one.
#
# `root_session` finds a thread by globbing '*.jsonl', so renaming the recording
# out of that glob is all it takes: the next call returns empty, and the loop's
# existing "no session" branch starts a fresh thread from instruction.md.  The
# recording is kept, not deleted -- it is the transcript of everything the
# harness did, and the phase's own record of why it rotated.
#
# Subagent forks are left alone.  Their session_meta carries forked_from_id, so
# `root_session` already skips them, and a fork's recording is the only copy of
# whatever that subagent did.
retire_session() {
    local n="$1" dir f moved=0
    dir="${CODEX_HOME:-/opt/codex-home}/sessions"
    for f in $(find "$dir" -name '*.jsonl' 2>/dev/null); do
        head -1 "$f" 2>/dev/null | grep -q '"type":"session_meta"' || continue
        head -1 "$f" 2>/dev/null | grep -q '"forked_from_id"' && continue
        mv "$f" "$f.retired-$n" 2>/dev/null && moved=$(( moved + 1 ))
    done
    echo "$moved"
}

mirror() {
    cp -f "$LOG_DIR"/* "$MIRROR_DIR"/ 2>/dev/null || true
}

# Cheap "did anything change" signal over the submitted tree.  Sizes and mtimes
# rather than contents, because this runs between every round and the point is
# only to tell a harness that is still working from one that has stopped.
# node_modules and .git are excluded: an npm command or a commit is not progress
# on the port, and node_modules is discarded before scoring anyway.
tree_fingerprint() {
    find /workspace/repo \
         -path /workspace/repo/node_modules -prune -o \
         -path /workspace/repo/.git -prune -o \
         -type f -printf '%s %T@ %p\n' 2>/dev/null \
      | sort | cksum
}

fast_fails=0
wedges=0
rotations=0
idle_turns=0
fingerprint="$(tree_fingerprint)"

# Where this phase's round numbering starts.  Per-round artifacts are named by
# round number -- events-N.jsonl, rc-N.txt, last-message-N.txt -- and a restarted
# container numbers from 1 again, so a phase resumed into the same LOG_DIR
# overwrites the earlier phase's stream file by file.  On pf02 run 3 that was the
# reason a live loop fix could not be picked up at all: restarting to load the
# fixed script would have destroyed events-1..30, the only record of how the run
# reached that point.  So the counter is seeded from what is already on disk, and
# the previous rounds simply stay where they are.
#
# Read from LOG_DIR rather than tracked in a state file, because the artifacts are
# the state: a file that says round 30 while events-30.jsonl is absent describes a
# phase that did not happen.
FIRST_ROUND=1
if [ -d "$LOG_DIR" ]; then
    highest=$(ls "$LOG_DIR" 2>/dev/null \
              | sed -n 's/^events-\([0-9]\{1,\}\)\.jsonl$/\1/p' \
              | sort -n | tail -1)
    if [ -n "${highest:-}" ]; then
        FIRST_ROUND=$(( highest + 1 ))
        echo "srb-agent: $LOG_DIR already holds rounds up to ${highest}; numbering from ${FIRST_ROUND}" >&2
    fi
fi
LAST_ROUND=$(( FIRST_ROUND + MAX_ROUNDS - 1 ))

# Which prompt the first resume of a *restarted* container uses.  A container
# that is picking up a session whose last turn ended cleanly must not be told the
# connection dropped -- that is a false statement about its own history, and one
# it can check by reading the transcript it is resuming.
if [ "${SRB_AGENT_FIRST_ROUND_ENDED_TURN:-0}" = "1" ]; then
    resume_prompt="$TURN_ENDED_PROMPT"
else
    resume_prompt="$CONTINUE_PROMPT"
fi
# The task is solved from the repository alone.  Passed on the command line,
# where it overrides the config file rather than trusting it.
WEB_SEARCH_FLAG=(-c web_search=disabled)

for round in $(seq "$FIRST_ROUND" "$LAST_ROUND"); do
    now=$(date +%s)
    elapsed=$(( now - START ))
    remaining=$(( DEADLINE_SEC - elapsed ))
    if [ "$remaining" -le 60 ]; then
        # Rounds this phase ran, not the round number: with the counter seeded from
        # disk those differ, and "after 45 rounds" for a phase that ran 15 would
        # misreport how much work the deadline actually bought.
        echo "srb-agent: deadline reached after ${elapsed}s, $(( round - FIRST_ROUND )) round(s) this phase (through round ${round})" >&2
        echo "deadline" > "$LOG_DIR/stop-reason.txt"; mirror
        break
    fi

    session=$(root_session)
    # A round gets the smaller of what is left and the per-round cap, so a hung
    # round costs MAX_ROUND_SEC rather than everything.  `capped` records which
    # limit applied, because rc=124 means "the phase is over" under one and "drop
    # this round and resume" under the other, and the exit code cannot tell them
    # apart on its own.
    round_budget="$remaining"
    capped=0
    if [ "$MAX_ROUND_SEC" -gt 0 ] && [ "$remaining" -gt "$MAX_ROUND_SEC" ]; then
        round_budget="$MAX_ROUND_SEC"
        capped=1
    fi
    echo "srb-agent: round ${round} starting (elapsed ${elapsed}s, budget ${round_budget}s of ${remaining}s left, session ${session:-new})" >&2
    round_start=$(date +%s)
    rc=0
    if [ -z "$session" ]; then
        timeout "$round_budget" codex exec \
            --json \
            "${WEB_SEARCH_FLAG[@]}" \
            -o "$LOG_DIR/last-message-${round}.txt" \
            -C /workspace/repo \
            - < "$INSTRUCTION" \
            > "$LOG_DIR/events-${round}.jsonl" 2> "$LOG_DIR/stderr-${round}.log"
        rc=$?
    else
        timeout "$round_budget" codex exec resume "$session" \
            --json \
            "${WEB_SEARCH_FLAG[@]}" \
            -o "$LOG_DIR/last-message-${round}.txt" \
            "$resume_prompt" \
            > "$LOG_DIR/events-${round}.jsonl" 2> "$LOG_DIR/stderr-${round}.log"
        rc=$?
    fi
    round_sec=$(( $(date +%s) - round_start ))
    # Budget left *now*, for the messages below.  `remaining` was computed at the top
    # of this iteration, before the round ran, and the two drop branches printed it as
    # "N still left" after spending up to MAX_ROUND_SEC of it -- so every such line
    # overstated what was left by the length of the round that had just ended.  On this
    # campaign that read as a budget that did not move: fw07/xhigh's rounds 1, 2 and 3
    # were each capped at 1800s and each reported the figure from before itself.
    #
    # The loop's own control was never wrong -- the deadline test at the top recomputes
    # from the clock every iteration -- which is what made the message worth fixing
    # rather than the arithmetic.  A supervisor reads these lines to decide whether a
    # run still has time to recover, and a separate name is used instead of reassigning
    # `remaining` so the pre-round uses keep reading as what they are.
    remaining_now=$(( DEADLINE_SEC - ( $(date +%s) - START ) ))
    [ "$remaining_now" -lt 0 ] && remaining_now=0

    echo "srb-agent: round ${round} exited rc=${rc} after ${round_sec}s" >&2
    echo "$rc" > "$LOG_DIR/rc-${round}.txt"
    mirror

    # Did this round reach the model at all?  Measured once here, above every
    # branch that reads it, because two of them do and they must not disagree
    # about the same round: the wedge counter below and the fast-failure counter
    # further down.  It used to be computed only at the fast-failure site, which
    # left the wedge branch asserting something it had never measured.
    #
    # One event does not count, and finding that out took a healthy run to notice.
    # A model that is not in codex's own catalogue makes it emit
    #   {"type":"item.completed","item":{"type":"error","message":"Model metadata
    #    for `kimi-k3` not found. Defaulting to fallback metadata..."}}
    # at thread start, before any request goes out.  It matches on `item.` alone,
    # so with kimi-k3 every round that got as far as starting a thread looked like
    # a round that reached the gateway -- including one that never reached it.
    #
    # One awk pass rather than `grep -v ... | grep -q`.  Under `set -o pipefail`
    # the -q exits on the first match, SIGPIPEs the upstream grep, and the
    # pipeline reports 141: measured on a real 240KB round, the two-grep version
    # answered "did not reach the gateway" for a round that plainly did, which is
    # the opposite of the bug it was written to fix.
    #
    # Two answers, not one, because the two readers below need different questions
    # answered and one flag cannot say both.  The fast-failure counter asks "did
    # anything come back?", and an error must count: "Selected model is at
    # capacity" arrives in seconds as an error and nothing else, and that is the
    # transient the phase exists to ride out.  The wedge counter asks "did the
    # gateway answer?", and for that an error is the evidence AGAINST, not for.
    #
    # Sharing one flag cost lang04/max fourteen hours.  From round 39 to the wall
    # every round emitted `thread.started`, `turn.started` and nine
    #   {"type":"error","message":"Reconnecting... N/10 (stream disconnected before
    #    completion: error sending request for url (.../v1/responses))"}
    # -- a 52MB thread the gateway would no longer accept, so no request ever
    # completed and no byte of work came back.  The first two events are emitted
    # locally before any socket opens, and the nine are failures, yet the whole
    # round matched as "reached the gateway", so the wedge counter stayed at 0 and
    # the phase retried a hopeless request 14 times at 1800s each.  Requiring
    # `item.` for the wedge branch keeps the lang01/medium fix that branch exists
    # for -- a round cut off mid-answer has hundreds of `item.` events -- while a
    # round that produced none of them now counts, which is what "the gateway is
    # not answering" was always supposed to mean.
    reached_gateway=0
    produced_work=0
    if [ -s "$LOG_DIR/events-${round}.jsonl" ]; then
        case "$(awk '/Model metadata for/ { next }
                     /"type":"item\./     { work = 1; exit }
                     /"type":"(turn\.|error)/ { seen = 1 }
                     END { print work ? "work" : (seen ? "reached" : "none") }' \
                    "$LOG_DIR/events-${round}.jsonl" 2>/dev/null)" in
            work)    reached_gateway=1; produced_work=1 ;;
            reached) reached_gateway=1 ;;
        esac
    fi

    # Before any branch that `continue`s, so that a round which returned at all
    # clears the streak.  Putting this after the rc=0 branch would leave a
    # success unable to reset it, and the counter would then pool wedges that
    # were minutes and several working rounds apart into a false "the gateway has
    # stopped answering".
    if [ "$rc" -ne 124 ]; then
        wedges=0
    fi

    # And the same argument, unchanged, for the other counter that ends a phase on a
    # REPEATED fault.  `fast_fails=0` lives at the bottom of the round body, on the
    # else of the short-round test -- so a round that failed slowly clears the
    # streak, and a round that SUCCEEDED does not, because the rc=0 branch just
    # below returns to the top of the loop without ever reaching it.
    #
    # That makes the two claims it stops on false.  Its own message says
    # "consecutive", and the stop-reason it writes is `infrastructure`: the verdict
    # that says the image or the key is broken and the run is worth relaunching from
    # scratch.  At MAX_FAST_FAILS=5, five short failures ANYWHERE in a 30-hour run
    # ended it that way, with the model working and the deadline unspent.
    #
    # rc, not produced_work: rc=0 is a completed turn by any reading, and it is not a
    # short failure whatever it did or did not write.  A round that exits 0 having
    # done nothing is the idle branch's business, and that counter clears itself on a
    # fingerprint change a few lines down.
    if [ "$rc" -eq 0 ]; then
        fast_fails=0
    fi

    if [ "$rc" -eq 0 ]; then
        # rc=0 means the *turn* ended, which is not the same as the work being
        # over.  `codex exec` exits 0 whenever the model emits a message with no
        # tool call -- including "let me now survey the remaining files:", which
        # is how the first real run of this task stopped after one round of 19
        # minutes with 133 of 137 modules ported, `lib/` still present and no
        # exports map.  Taking rc=0 as completion turned a mid-thought pause into
        # a submitted answer.
        #
        # So completion has to be said rather than inferred.  The sentinel is the
        # only signal that ends the phase, and the harness is told what it is in
        # TURN_ENDED_PROMPT -- which is why this cannot be checked on round 1,
        # where the harness has seen instruction.md and nothing else.
        #
        # ...and "said" has to mean SAID, not "mentioned".  A substring grep accepts the
        # sentinel anywhere in the message, including inside a sentence that denies it.
        #
        # Measured over all 236 published codex cells, in two parts because the evidence
        # differs: 218 by replaying this exact grep against the `last-message-<round>.txt`
        # files it reads, and the remaining 18 -- whose raw logs no longer exist anywhere --
        # from the per-round records archived for them, where every one ends in a bare
        # sentinel and is clean.  So the count below is a total, not a sample.
        #
        # 21 cells ended here on a mention the model wrote to DENY completion, and not one
        # of them ever emitted the sentinel on its own line.  All 21 broke on the grep's
        # matching round and recorded stop-reason `completed`.  Their closing lines say so
        # outright --
        #   "The rewrite is not complete. I cannot honestly emit SRB_TASK_COMPLETE."
        #   "I'm not finished. The migration is incomplete and cannot truthfully be
        #    marked SRB_TASK_COMPLETE."
        # -- and 11 of the 21 were ended at round 7 or earlier, two at round 2, with 87-99%
        # of the wall budget unspent.  That is strictly worse than the mid-thought pause
        # this check was written to prevent: it does not merely stop a working phase, it
        # labels the fragment a finished submission.
        #
        # TURN_ENDED_PROMPT asks for "the single line ${DONE_SENTINEL} and nothing else",
        # so hold the check to what was asked: the LAST non-empty line, stripped of
        # whitespace and of markdown decoration (backticks, asterisks, trailing period),
        # must equal the sentinel exactly.  A mention inside prose no longer matches, and
        # neither does a line that merely contains it.
        #
        # The asymmetry is deliberate and it runs the other way.  A false negative costs
        # deadline that was allocated to be spent, and the tree is graded either way; a
        # false positive submits unfinished work under the verdict `completed`.  When in
        # doubt this check must keep working.
        #
        # Verified on a rerun of one victim: lang02/medium's new run mentions the sentinel
        # in rounds 3,4,5,7,8,9,21,23,26 -- the old grep ended it at round 3 -- and under
        # this test it ran to round 76 and stopped on a 17-byte bare sentinel.
        last_line="$(sed -e 's/\r$//' "$LOG_DIR/last-message-${round}.txt" 2>/dev/null \
                     | grep -v '^[[:space:]]*$' | tail -1 \
                     | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' \
                           -e 's/^[`*_]*//' -e 's/[`*_.]*$//')"
        if [ "$last_line" = "$DONE_SENTINEL" ]; then
            echo "completed" > "$LOG_DIR/stop-reason.txt"
            echo "srb-agent: harness declared completion after $(( round - FIRST_ROUND + 1 )) round(s) this phase (round ${round})" >&2
            mirror
            break
        fi
        # Logged, not silent: a phase that runs on past a mention is indistinguishable from
        # one where the model never referred to the sentinel at all, and the difference is
        # what this change is about.
        if grep -q "$DONE_SENTINEL" "$LOG_DIR/last-message-${round}.txt" 2>/dev/null; then
            echo "srb-agent: round ${round} mentioned ${DONE_SENTINEL} without declaring it (last line: ${last_line:0:80}); not treating that as completion" >&2
        fi

        # A harness that will not say the sentinel and will not touch the tree
        # either has stopped in a way it cannot be nudged out of.  Resuming it
        # forever would burn the deadline re-reading its own work, so a few such
        # turns are accepted as an ending -- recorded distinctly, because a phase
        # that ran out of momentum is not one that finished.
        #
        # An unchanged tree is not enough on its own to say that.  pf02's round 6
        # ran `cat src/core/nodes/arguments.js` to work out a circular import,
        # said what it planned to do about it, and ended its turn mid-sentence
        # without writing anything -- 42s against 208-1469s for the rounds around
        # it.  The tree was untouched, so an mtime fingerprint called that round
        # identical to one that did nothing at all, and three of them in a row
        # would have declared a working phase over.  A round that ran a command
        # acted; the discriminator is whether it acted, not whether the action
        # happened to be a write.  So both have to hold before a round counts as
        # idle, and a round that only read is reported as thinking rather than
        # silently folded into the same counter.
        #
        # The cost is that a harness stuck in a genuine read-only loop now runs to
        # the deadline instead of stopping here.  That is the trade the run wants:
        # the deadline is budget that was allocated to be spent, while ending a
        # phase that is still working costs the whole run.
        new_fingerprint="$(tree_fingerprint)"
        acted=0
        if [ -s "$LOG_DIR/events-${round}.jsonl" ] \
           && grep -qm1 '"type":"command_execution"' \
                   "$LOG_DIR/events-${round}.jsonl" 2>/dev/null; then
            acted=1
        fi
        if [ "$new_fingerprint" = "$fingerprint" ] && [ "$acted" -eq 1 ]; then
            echo "srb-agent: round ${round} ran commands but wrote nothing; thinking, not idle (idle stays ${idle_turns}/${MAX_IDLE_TURNS})" >&2
        elif [ "$new_fingerprint" = "$fingerprint" ]; then
            idle_turns=$(( idle_turns + 1 ))
            echo "srb-agent: round ${round} ended cleanly, changed nothing, did not declare completion (idle ${idle_turns}/${MAX_IDLE_TURNS})" >&2
            if [ "$idle_turns" -ge "$MAX_IDLE_TURNS" ]; then
                echo "idle" > "$LOG_DIR/stop-reason.txt"
                echo "srb-agent: ${idle_turns} clean rounds changed nothing; treating the phase as over" >&2
                mirror
                break
            fi
        else
            idle_turns=0
            fingerprint="$new_fingerprint"
        fi

        echo "srb-agent: round ${round} ended its turn without finishing; resuming" >&2
        resume_prompt="$TURN_ENDED_PROMPT"
        mirror
        sleep 5
        continue
    fi

    # Any other exit is a dropped round rather than a finished turn, so the next
    # resume says so instead of asking a question the harness did not prompt.
    resume_prompt="$CONTINUE_PROMPT"

    if [ "$rc" -eq 124 ]; then
        # 124 under the per-round cap is a wedged round, not the end of the
        # budget: the model produced no byte for MAX_ROUND_SEC while time
        # remained.  Dropping it and resuming is the whole reason the cap exists,
        # so this must not write stop-reason=deadline -- doing so would end a
        # phase with hours left and label a harness stall as the model running
        # out of time.  Counted, because a gateway that wedges every round is an
        # infrastructure fault and should not retry to MAX_ROUNDS.
        if [ "$capped" = "1" ]; then
            # A round that hit the cap while the model was answering is not a
            # wedge.  The counter exists for a gateway that has stopped
            # responding, and three of those in a row end the phase as an
            # infrastructure fault -- so counting a working round here ends a run
            # that was making progress, with its whole deadline unspent.
            #
            # That is not hypothetical.  lang01/medium wrote 406, 478 and 394
            # events in rounds 2, 3 and 4, each cut off by this cap; the loop
            # recorded all three as "produced nothing", reached the cap and
            # stopped the phase 95 minutes into a 40-hour budget.  It was graded
            # 0.0, and the zero was published as the model's.  Across that whole
            # fleet, all 30 capped rounds had produced between 142KB and 5MB: the
            # counter's true-positive rate was zero and it had already cost a run.
            #
            # So the claim has to be measured before it is made.  A round that
            # produced work is dropped and resumed exactly as before -- the work
            # survives in the thread either way -- but it does not advance a
            # counter whose meaning is "the gateway is not answering".
            #
            # `produced_work`, not `reached_gateway`: an error event means a request
            # failed, which is the evidence for a wedge and not against it.  Reading
            # the wrong one of those two let lang04/max retry an unsendable 52MB
            # thread from round 39 to its wall -- see the measurement above.
            #
            # The residual risk is narrower than it was but not gone: a gateway that
            # emits one `item.` and then stalls forever still runs to the deadline
            # rather than stopping at the cap.  That is the trade the idle branch
            # above already makes in the same direction, for the same reason -- the
            # deadline is budget that was allocated to be spent, while ending a
            # phase that is still working costs the whole run.
            if [ "$produced_work" = "1" ]; then
                echo "srb-agent: round ${round} was still answering when the ${round_budget}s cap fired; dropped and resuming, not counted as a wedge (wedges stay ${wedges}/${MAX_WEDGES}, ${remaining_now}s still left)" >&2
                mirror
                sleep 5
                continue
            fi
            wedges=$(( wedges + 1 ))
            echo "srb-agent: round ${round} produced nothing for ${round_budget}s and was dropped (wedge ${wedges}/${MAX_WEDGES}, ${remaining_now}s still left)" >&2
            mirror
            if [ "$wedges" -ge "$MAX_WEDGES" ]; then
                # Before giving up on the gateway, try the one move that changes the
                # request.  "The gateway is not answering" and "the gateway will not
                # accept THIS thread" produce identical rounds -- rc=124, no work --
                # and only the second is survivable, by retiring the thread and
                # starting a fresh one.  The rotation branch below already does
                # exactly that, but it triggers on error text
                # (context_length_exceeded and friends), and a gateway that drops the
                # stream mid-upload instead of refusing it outright never emits any
                # of those strings.  The case, as reported by the session that hit
                # it -- its artifacts are not on the box this was written on, so
                # the figures are its measurement and not one reproduced here: a
                # lang04/max thread of 52MB, fourteen rounds of "stream
                # disconnected before completion", not one context error, so the
                # branch that could have saved it never fired.  The argument below
                # does not rest on those numbers.
                #
                # Reaching the wedge cap is the evidence that licenses this.  A
                # single dropped round is a transient and must not retire a working
                # thread -- that mistake costs the model its accumulated plan -- but
                # MAX_WEDGES consecutive rounds with zero work is not a transient,
                # and it is the same standard the phase already accepts as grounds
                # for ending outright.  Rotating is strictly cheaper than that: the
                # tree survives either way, and a fresh thread that gets a turn in
                # proves the endpoint was never the problem.
                #
                # Shares the rotation budget with the context-error path on purpose.
                # Both mean "this thread cannot be sent", so the phase gets three
                # attempts at a new one however it arrived here, and a run that
                # exhausts them stops as an infrastructure fault -- which by then it
                # has actually measured, having failed on a thread carrying nothing
                # but the instruction.
                if [ "$rotations" -lt "$MAX_SESSION_ROTATIONS" ]; then
                    rotations=$(( rotations + 1 ))
                    retired=$(retire_session "$round")
                    echo "srb-agent: ${wedges} consecutive rounds produced nothing; retired ${retired} thread recording(s) and starting a new thread (rotation ${rotations}/${MAX_SESSION_ROTATIONS}, ${remaining_now}s still left)" >&2
                    # The fresh thread gets its own allowance: the counter is
                    # measuring the endpoint, and the request it will now send is
                    # not the one that was failing.
                    wedges=0
                    resume_prompt="$CONTINUE_PROMPT"
                    mirror
                    sleep 10
                    continue
                fi
                echo "infrastructure" > "$LOG_DIR/stop-reason.txt"
                echo "srb-agent: ${wedges} consecutive rounds wedged after ${rotations} thread rotation(s); treating as an infrastructure fault" >&2
                mirror
                break
            fi
            sleep 60
            continue
        fi
        echo "deadline" > "$LOG_DIR/stop-reason.txt"
        echo "srb-agent: round ${round} hit the wall clock" >&2
        mirror
        break
    fi

    # The one failure a resume loop cannot retry its way out of: the thread has
    # filled the model's context window.  Every resume re-sends the whole
    # conversation, so the prompt that was rejected is the prompt the next round
    # sends again -- and it grows a little each time, because the resume prompt
    # itself is appended to the recording whether or not the round succeeded.
    #
    # Measured on pf02 run 3: kimi-k3's real ceiling is 278528 tokens, the thread
    # crossed it at round 16, and rounds 16-20 all died the same way, +68 tokens
    # apart.  Nothing above catches it.  It is not a wedge (the round exits), and
    # at ~204s -- ten stream retries against a fast rejection -- it is far longer
    # than MIN_ROUND_SEC, so the fast-fail counter resets on every one of them.
    # Left alone the phase spends its whole remaining budget re-sending a prompt
    # that is rejected in one second, and reports `deadline` at the end: a
    # configuration fault wearing the model's clothes.
    #
    # codex 0.146 has no recovery to lean on.  `codex exec --help` exposes no
    # compaction flag, and its own window accounting never fired -- config
    # declares model_context_window = 256000 and it sent 278833 -- because
    # "Model metadata for `kimi-k3` not found. Defaulting to fallback metadata"
    # on every round means it never knew this model's real window.
    #
    # So the thread is retired and the next round starts a new one, which is the
    # only move that changes the prompt.  The work is not lost: it is in the tree,
    # which is what gets graded, and a fresh thread reads the tree.  What is lost
    # is the harness's accumulated plan -- the same loss a restarted container
    # takes, and cheaper than the alternative of losing the rest of the budget.
    # Matched on strings only an error path produces, and deliberately not on a
    # bare "context window": an agent that says "I'm running low on context
    # window" in a message would otherwise have its working thread retired, which
    # is a far more expensive mistake than failing to detect the real thing.
    #   reason: context_length_exceeded  - codex's own reconnect line
    #   ContextWindowExceeded            - litellm's exception class
    #   maximum context length is        - the OpenAI-compatible error text
    exhausted=0
    if [ -s "$LOG_DIR/events-${round}.jsonl" ] \
       && grep -aqm1 'reason: context_length_exceeded\|ContextWindowExceeded\|maximum context length is' \
               "$LOG_DIR/events-${round}.jsonl" 2>/dev/null; then
        exhausted=1
    fi

    if [ "$exhausted" = "1" ]; then
        rotations=$(( rotations + 1 ))
        echo "srb-agent: round ${round} failed because the thread is out of context (rotation ${rotations}/${MAX_SESSION_ROTATIONS})" >&2
        if [ "$rotations" -gt "$MAX_SESSION_ROTATIONS" ]; then
            # Rotating again would just refill the window.  If a brand-new thread
            # cannot get a turn in, the request is too big before the harness has
            # done anything, which is a configuration fault and not a model one.
            echo "context-exhausted" > "$LOG_DIR/stop-reason.txt"
            echo "srb-agent: ${rotations} thread rotations did not help; the context window cannot fit this task" >&2
            mirror
            break
        fi
        retired=$(retire_session "$round")
        echo "srb-agent: retired ${retired} thread recording(s); round $(( round + 1 )) will start a new thread" >&2
        # A new thread is not a resumed one: it gets instruction.md on stdin, so
        # the continuation prompt must not be left set to the turn-ended text.
        resume_prompt="$CONTINUE_PROMPT"
        mirror
        sleep 10
        continue
    fi

    # A round that dies faster than a model call can complete did not fail for a
    # model reason -- it is a usage error, a missing key, an unreachable gateway.
    # Retrying it 400 times changes nothing and destroys the round budget in
    # minutes, so a run of them stops the phase and says so.
    # ...unless the round actually reached the gateway.  "Selected model is at
    # capacity" comes back in seconds and is exactly the transient the run is
    # supposed to ride out, so an event stream carrying a real model turn or a
    # real error is what separates "retry harder" from "give up".  Dropping the
    # catalogue-warning line is narrower than requiring `turn.started` -- a
    # capacity refusal may well arrive before any turn begins, and that one has to
    # keep retrying.  $reached_gateway is measured once, just after the round
    # exits, where the wedge counter can read the same answer this does.
    if [ "$round_sec" -lt "$MIN_ROUND_SEC" ] && [ "$reached_gateway" = "1" ]; then
        echo "srb-agent: round ${round} failed in ${round_sec}s but reached the gateway; backing off" >&2
        sleep $(( round < 10 ? 60 : 120 ))
        continue
    fi

    if [ "$round_sec" -lt "$MIN_ROUND_SEC" ]; then
        fast_fails=$(( fast_fails + 1 ))
        echo "srb-agent: round ${round} failed in ${round_sec}s (fast failure ${fast_fails}/${MAX_FAST_FAILS})" >&2
        if [ "$fast_fails" -ge "$MAX_FAST_FAILS" ]; then
            echo "infrastructure" > "$LOG_DIR/stop-reason.txt"
            {
                echo "srb-agent: ${fast_fails} consecutive rounds failed in under ${MIN_ROUND_SEC}s."
                echo "srb-agent: this is a harness or configuration fault, not a model fault."
                echo "srb-agent: last stderr follows."
                tail -20 "$LOG_DIR/stderr-${round}.log" 2>/dev/null
            } >&2
            mirror
            break
        fi
    else
        fast_fails=0
    fi

    sleep $(( round < 10 ? round * 5 : 60 ))
done

echo "srb-agent: agent phase over" >&2
date +%s > "$LOG_DIR/finished-at.txt"
mirror
