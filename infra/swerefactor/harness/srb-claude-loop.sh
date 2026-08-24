#!/bin/bash
# The agent phase for Claude Code, supervised from inside the container.
#
# Mirrors srb-agent-loop.sh's contract for codex: a deadline-bounded round loop
# that resumes the same conversation after each round, so a gateway capacity
# error or a dropped stream does not restart the work from nothing. Claude Code's
# `--print` mode runs one turn and exits, so every round is a fresh process that
# either completed a turn (rc=0) or was interrupted (rc!=0); `--continue` (or
# `--resume <id>`) picks up the same session_id the init event reported.
#
# Completion is signalled, not inferred: a turn that ends cleanly (rc=0,
# terminal_reason=completed) is not necessarily the job being done -- it may be
# a mid-thought pause -- so the loop only stops on the sentinel the task
# instruction tells the model to emit, or on a few idle turns that changed
# nothing. Mirrors the codex loop's reasons verbatim where the two clients agree.
set -uo pipefail

INSTRUCTION="${SRB_INSTRUCTION:-/opt/srb-agent/instruction.md}"
MIRROR_DIR="${SRB_AGENT_MIRROR_DIR:-/logs/agent}"
LOG_DIR="${SRB_AGENT_LOG_DIR:-/opt/srb-agent-logs}"
DEADLINE_SEC="${SRB_AGENT_DEADLINE_SEC:-144000}"
MAX_ROUNDS="${SRB_AGENT_MAX_ROUNDS:-400}"
MIN_ROUND_SEC="${SRB_AGENT_MIN_ROUND_SEC:-20}"
MAX_FAST_FAILS="${SRB_AGENT_MAX_FAST_FAILS:-5}"
MAX_ROUND_SEC="${SRB_AGENT_MAX_ROUND_SEC:-1800}"
# Consecutive wedged rounds before the phase gives up on the gateway.  A wedge
# costs MAX_ROUND_SEC, so a fixed *count* fixes the waiting-out budget at 90
# minutes regardless of how much run is left -- and the runs that hit it are the
# long ones, where 90 minutes is nothing.  Allow a fraction of the budget
# instead: ~15% of the declared deadline may be spent on a gateway that is not
# answering.  lang01/lang04 (107100s) tolerate ~4.5h, a 6-hour task still gets
# the old 90 minutes, and no task gets less -- the previous constant is the floor.
# The counter resets on any round that produces work, and the deadline is checked
# before every round, so a truly dead endpoint still ends the phase, as
# "deadline" with the hours it waited on the record rather than as an
# infrastructure fault with most of the budget unspent.
MAX_WEDGES="${SRB_AGENT_MAX_WEDGES:-}"
if [ -z "$MAX_WEDGES" ]; then
    MAX_WEDGES=$(( (DEADLINE_SEC * 15 / 100) / MAX_ROUND_SEC ))
    [ "$MAX_WEDGES" -lt 3 ] && MAX_WEDGES=3
fi
DONE_SENTINEL="${SRB_AGENT_DONE_SENTINEL:-SRB_TASK_COMPLETE}"
MAX_IDLE_TURNS="${SRB_AGENT_MAX_IDLE_TURNS:-3}"

# Claude Code auth + model are env vars the binary reads natively.
MODEL="${SRB_MODEL:-claude-opus-5}"
EFFORT_FLAG=""
if [ -n "${SRB_CLAUDE_EFFORT:-}" ]; then EFFORT_FLAG="--effort ${SRB_CLAUDE_EFFORT}"; fi

# The task is solved from the repository alone.  A bare tool name here denies the
# tool outright, so it is never offered.
DENIED_TOOLS="WebSearch,WebFetch"

mkdir -p "$LOG_DIR" "$MIRROR_DIR" 2>/dev/null
rm -f "$LOG_DIR/stop-reason.txt" "$LOG_DIR/finished-at.txt" \
      "$MIRROR_DIR/stop-reason.txt" "$MIRROR_DIR/finished-at.txt" 2>/dev/null

# Claude Code refuses every bypass-permissions mode under root/sudo ("cannot be
# used with root/sudo privileges"), so the loop must run claude as a non-root
# user. Tasks that declare [agent] user already run as that user; for the ones
# that don't (default root), drop to a fixed unprivileged uid here, having made
# the directories claude and the loop write writable by it.
if [ "$(id -u)" = "0" ]; then
    AGENT_UID="${SRB_AGENT_UID:-1000}"
    AGENT_GID="${SRB_AGENT_GID:-$AGENT_UID}"
    # chown only what this phase writes; /workspace/repo must be writable by the
    # agent so the migration lands, and CLAUDE_HOME/logs so sessions + events do.
    chown -R "$AGENT_UID:$AGENT_GID" "$LOG_DIR" "$MIRROR_DIR" \
        "${CLAUDE_HOME:-/opt/claude-home}" 2>/dev/null || true
    chmod -R a+rwX /workspace/repo 2>/dev/null || true
    # Drop to an unprivileged uid. setpriv --init-groups needs a /etc/passwd
    # entry, which a minimal env image may not have, so set HOME by hand and use
    # reuid/regid only (no supplementary groups needed here).
    exec setpriv --reuid="$AGENT_UID" --regid="$AGENT_GID" --clear-groups \
        --inh-caps=-all env HOME="${CLAUDE_HOME:-/opt/claude-home}" "$0" "$@"
fi

# Everything this loop says about itself, into a file the mirror carries out; see
# srb-agent-loop.sh, where the same stream was going nowhere.  Harbor captures a
# setup command's output into its own machinery and never writes it to the trial
# directory, so a finished run's agent/ held the events and the exit codes and no
# account of which rounds were dropped, how many wedges accumulated, or which of
# the exits ended the phase.
#
# Below the setpriv block on purpose: that branch re-execs this script as an
# unprivileged user, so a redirect above it would open the file twice and stamp two
# headers for one phase.  The chown above has already made $LOG_DIR writable by
# that uid, and loop.log does not exist yet, so it is created by its owner.
exec 2>> "$LOG_DIR/loop.log"
echo "=== srb-claude loop starting $(date -u '+%Y-%m-%dT%H:%M:%SZ') pid=$$ uid=$(id -u) ===" >&2

START="${SRB_AGENT_START:-$(date +%s)}"
CONTINUE_PROMPT='Continue from where you left off. The previous round ended before you were finished, for reasons on our side and not yours: the connection to the model dropped. Nothing about your work has been reviewed or judged. Pick up your own plan and keep working until you consider the job done.'
# Handed to a *fresh* session whenever the previous one had to be abandoned.  The
# conversation is gone, so this prompt cannot say "carry on where you left off" --
# the model has no memory of a plan to carry.  It must say what is true: the
# repository already holds work, some of it this model's, and the only way to know
# what is done is to look.  The instruction is re-read alongside it, because a
# fresh session has never seen the task.
#
# The cause is a separate string because there are now two, and the body is true of
# both while the first sentence is true of only one.  A session rotated off a
# gateway stall never outgrew anything, and telling that model its transcript was
# too long invites exactly the wrong correction -- it would start writing less and
# reading less to stay small, when the request that failed was already inside the
# window and the tree is what it should be reading.
FRESH_SESSION_CAUSE_CTX='Your previous session grew past the model context window and had to be closed.'
FRESH_SESSION_CAUSE_GW='Your previous session was closed because its requests stopped completing: several in a row came back empty from the gateway that carries them, and a new conversation sends a smaller request that does complete.'
FRESH_SESSION_BODY=' That is a limit on our side, not a judgement: nothing about your work has been reviewed, and no deadline has passed.

The repository at /workspace/repo is exactly as your previous session left it, and its work is preserved -- but this conversation is new, so you cannot remember what you had done or what you had planned. Do not assume the tree is untouched, and do not start over from scratch.

Read the task instruction below again, then inspect the repository to establish what is already finished before you change anything. Continue from there.

--- task instruction ---
'
# Default so the existing single-cause callers and tests keep their exact text.
FRESH_SESSION_PROMPT="${FRESH_SESSION_CAUSE_CTX}${FRESH_SESSION_BODY}"
TURN_ENDED_PROMPT="Your last turn ended without a tool call, so the harness stopped it there. That is a turn boundary and not a verdict -- nothing about your work has been reviewed or judged, and no deadline has passed.

If you are genuinely finished, reply with the single line ${DONE_SENTINEL} and nothing else. That is the only signal that ends this phase; a turn that simply stops talking will be resumed like this one.

Otherwise pick up your own plan and keep working."

cd /workspace/repo || exit 1

mirror() { cp -f "$LOG_DIR"/* "$MIRROR_DIR"/ 2>/dev/null || true; }

tree_fingerprint() {
    find /workspace/repo \
         -path /workspace/repo/node_modules -prune -o \
         -path /workspace/repo/.git -prune -o \
         -type f -printf '%s %T@ %p\n' 2>/dev/null \
      | sort | cksum
}

# Read the session_id from a round's stream-json (the init event carries it).
session_of() {
    local f="$1"
    grep -m1 -o '"session_id":"[^"]*"' "$f" 2>/dev/null | head -1 | cut -d'"' -f4
}

# Did the round reach the model (a real assistant turn or error, not a usage
# rejection before any call)? Claude's stream emits an "assistant" event when
# the model answered, and a "result" event on completion.
reached_gateway() {
    local f="$1"
    [ -s "$f" ] || return 1
    grep -qm1 '"type":"assistant"\|"type":"result"\|"type":"error"' "$f" 2>/dev/null
}

# Did the round produce actual work, as opposed to merely getting an answer of
# some kind out of the gateway?  These are two different questions and the loop
# needs both.  reached_gateway is the right test for the fast-fail backoff: a 429
# or a capacity refusal *is* an error event, and a round that got one should keep
# retrying rather than be counted against a fault budget.  It is the wrong test
# for a wedge, because an error event is a request that did not complete -- it is
# evidence *for* a wedge, not against it, and a capped round whose whole stream is
# errors would otherwise be excused as "still answering" and never counted.
#
# Only an assistant event says the model produced something.  A result event does
# not qualify: it is also how a failed round ends, and a round killed by the cap
# never emits one anyway.  Reported by the session working on the codex loop,
# which measured the same defect there: 14 rounds at 1800s each, every one a
# stream of reconnect failures, wedge counter parked at 0/3 the whole time.
# A bare '"type":"assistant"' test is not enough, because the CLI reports its own
# failures AS an assistant event with `"model":"<synthetic>"`.  Measured on the
# sonnet-5/max fleet of 2026-08-09: lang07 rounds 3-9 were byte-identical 5524-byte
# streams -- one assistant event, model=<synthetic>, duration_api_ms=0, zero tokens
# in every field -- i.e. rounds that did nothing at all, and the old test called
# every one of them work.  That is the same defect this comment was written to
# prevent, wearing the client's error shape instead of the gateway's.  Excluding
# the synthetic model is what makes the wedge and gateway bounds able to count.
#
# Deliberately NOT `grep ... | grep -qv ...`.  This script runs under
# `set -o pipefail`, and in that shape the downstream `-q` exits on its first
# match while the upstream grep is still writing; once the 64KB pipe buffer is
# full it takes SIGPIPE and the pipeline reports 141, so the function answers
# "no work" for a round that did a great deal of it.  It is a race on stream
# size -- it passed on the 5KB no-op streams and failed on the 317KB real one --
# and the direction it fails in is the harmful one: every capped round would be
# recorded as a wedge and the run would stop against MAX_WEDGES while healthy.
# The loop-free forms have the same hazard, so this is a plain read: no pipe, no
# subshell (a `<` redirect keeps the loop in this shell, so `return` works), and
# no external binary, which also settles whether every task image ships awk.
# `|| [ -n "$line" ]` matters -- a round killed by the cap mid-write leaves a
# final line with no newline, and that line can be the only real turn.
produced_work() {
    # "The model did something", not "the stream contains an assistant record".
    #
    # Two shapes have to be excluded, and each cost a real run:
    #
    #  1. model="<synthetic>".  Those records are the CLI's OWN error text
    #     ("API Error: ...") wearing an assistant role.  A round whose only
    #     assistant event is synthetic did nothing at all.
    #
    #  2. a real assistant event carrying only an empty thinking block.  This
    #     gateway strips thinking text, so a round that thought past the proxy's
    #     read tolerance and was cut leaves exactly one record --
    #     model="claude-sonnet-5", one thinking block of length 0 with a
    #     signature, output_tokens=2 -- and nothing else.  lang07 produced 56 of
    #     these consecutively while every bound in the loop read them as work.
    #
    # So require SUBSTANCE: a tool call, or text the model actually emitted.  A
    # tool_use is unambiguous; for text, require the block to be non-empty.
    # Thinking blocks are deliberately not evidence, because their text is not
    # observable here -- an empty one is indistinguishable from a stripped one,
    # and treating "it might have been thinking" as work is what made the stall
    # invisible.
    local f="$1" line
    [ -s "$f" ] || return 1
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            *'"type":"assistant"'*) ;;
            *) continue ;;
        esac
        case "$line" in
            *'"model":"<synthetic>"'*) continue ;;
        esac
        case "$line" in
            *'"type":"tool_use"'*) return 0 ;;
        esac
        # A non-empty text block: '"type":"text","text":"' followed by anything
        # other than the closing quote.  Matches the CLI's field order, which puts
        # type before text in every stream record observed across 20 trials.
        case "$line" in
            *'"type":"text","text":""'*) continue ;;
            *'"type":"text","text":"'*) return 0 ;;
        esac
    done < "$f"
    return 1
}

# Did the round carry a GATEWAY failure signature?  A third question, distinct
# from both above, and the one the stall bound actually needs.
#
# reached_gateway cannot answer it.  That predicate matches '"type":"result"',
# and a result record is written at the END OF EVERY ROUND -- including a round
# whose only other record is the CLI's own synthetic error.  Measured on lang07's
# second attempt: true for 35 of 35 rounds, the 4 pure-524 rounds that never
# reached a model included.  So the stall branch it gated was really "produced no
# work", and the `gateway` stop-reason that branch writes was being applied
# without anything ever having tested for a gateway.
#
# It happened to be right there -- all 31 no-work rounds did carry a signature
# (27 `Connection closed mid-response`, 4 `API Error: 524`) -- but it would say
# `gateway` just as confidently about a model that answered with an empty message
# and no error at all.  That exculpates the model by construction, which is the
# one direction a benchmark harness must not err in.
#
# Match on MECHANISM rather than a list of status codes: '"model":"<synthetic>"'
# is by construction the CLI's own error text wearing an assistant role, so it
# covers every code the client can surface, including ones not observed yet.  An
# upstream error shape that matches nothing here falls through to the no-work
# counter and is reported as `no-progress` -- less specific, but not a false
# accusation against the infrastructure.
gateway_error() {
    local f="$1"
    [ -s "$f" ] || return 1
    grep -qm1 '"model":"<synthetic>"\|Connection closed mid-response\|API Error:' \
        "$f" 2>/dev/null
}

# Did the model itself claim completion on this round?  The sentinel is checked in
# the result text and ONLY there -- grepping the whole events file also matches
# text the model never replied.  Classified across the 20 published trials of
# 2026-08-08, every sentinel occurrence sits in one of four records:
# assistant/text (20), result (20), assistant/thinking (10), user/tool_result (3).
# No prompt echo appeared at all, though the `TURN_ENDED_PROMPT` above does carry
# the sentinel and would match if the CLI ever streamed it back.
#
# The tool_result shape is the one that decided a run.  This script assigns
# DONE_SENTINEL in cleartext above, so a model that `cat`s it puts the sentinel
# into its own transcript as tool output.  fw07's first trial did that at rounds
# 28-32 and nowhere else -- no assistant record, no result record -- and the
# file-scoped test then in place wrote `completed` after round 32: 32 rounds and
# 3.5h graded as a completion the model never claimed.  The failure is silent and
# one-directional, ending the phase early and publishing `completed`, so a
# truncated run is averaged as a model that finished.  The thinking-block matches
# are harmless only by luck -- all 10 fall on the same round as that run's real
# reply -- but two runs would have stopped early on the tool_result alone:
# build02 at round 1 rather than the round 3 it finished on, fw07's relaunch at
# round 4 rather than round 7.
#
# The `result` event carries the final assistant text and nothing else, so an
# rc=0 round without the sentinel there cannot have replied with a single-line
# sentinel: replayed over all 20 published trials the narrowed test still matches
# 20 of 20, each on that run's own last round, and returns nothing on fw07's
# first trial.  Unobserved residual: a model that writes the sentinel as prose
# inside its final reply still matches.
#
# Pipe-free for the reason given on produced_work above: under `set -o pipefail`
# a `grep ... | grep -q` pipeline can report 141 when the downstream exits first,
# and here that direction discards a real completion -- the run would keep going
# and publish `idle` or burn the deadline instead of `completed`.  The two
# positive fixtures are 938KB and 615KB, well past the 64KB pipe buffer, so this
# was reachable and not theoretical.
declared_done() {
    local f="$1" line
    [ -s "$f" ] || return 1
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            *'"type":"result"'*)
                case "$line" in
                    *"$DONE_SENTINEL"*) return 0 ;;
                esac ;;
        esac
    done < "$f"
    return 1
}

# Did the round die because the session's own transcript no longer fits the
# model's context window?  --continue then replays the exact request the gateway
# just refused, so the round fails in about a second and reaches the gateway on
# the way -- which lands it in the transient-backoff branch below, where waiting
# cannot help, because nothing about the request will change on its own.
#
# Measured 08-08 against dsv4-flash (278528-token window), read from the raw
# events-N.jsonl stream, which is the only source carrying every assistant turn
# and so counts a refusal once per refusal: 15 of 20 runs hit it, every one of
# those for at least 20 rounds, the worst 34 consecutive (lang01).  The five that
# escaped had all stopped for other reasons inside three rounds, so they never
# built a large transcript.
#
# The CLI does eventually rescue itself -- all 15 ran between 2 and 7 sessions,
# because --continue auto-compacts once the transcript will not fit, and that
# emits a new session id.  fw01 is the shape of it: round 1 worked for 1690s and
# was refused, rounds 2 to 24 were 6677-byte replays for 36 minutes, and round 25
# came back on a new session and did another 1727s of real work.  So this is not
# a run-ending fault, and the claim to make for it is narrow: the loop rotates on
# the first refusal instead of the twenty-fifth.
#
# The transcript is also the only thing lost by starting over: the work is in the
# repo on disk.  So the answer is a fresh session, not a retry.
#
# The match must be case-INSENSITIVE.  Claude Code 2.1.220 writes the refusal as
# `"result":"Prompt is too long"` -- capital P -- so the lowercase pattern above
# matched nothing, and on the sonnet-5/max fleet of 2026-08-09 the rotation never
# fired once: fw02 spent rounds 1-3 on it, lang02 rounds 1-4, lang05 rounds 2-4,
# fw05 rounds 3-4, fw07 round 3.  Every one of those runs did recover in-session a
# few rounds later, and the session_id never changed while doing so, so the 08-08
# claim above (that --continue auto-compacts and emits a NEW session id) does not
# hold for this client: recovery is in-place, and the only cost is the refused
# rounds.  A grep whose red branch has never executed is not evidence of absence,
# which is why the predicates now have a test driving them both ways against real
# streams (srb-runs-0809/sonnet5-max/test-loop-predicates.sh).
ctx_window_exceeded() {
    local f="$1"
    [ -s "$f" ] || return 1
    grep -qim1 'ContextWindowExceeded\|context_length_exceeded\|maximum context length\|prompt is too long' \
        "$f" 2>/dev/null
}

fast_fails=0; wedges=0; idle_turns=0; fingerprint="$(tree_fingerprint)"
# Set when the previous round overflowed the context window; consumed by the next
# round, which then starts a new session instead of resuming.  resets counts how
# often that has happened: each reset costs the model its memory, so a run that
# keeps overflowing is thrashing rather than progressing, and is worth ending as a
# recorded stop reason rather than letting it consume the whole deadline.
fresh_session=0; resets=0; fresh_reason=""
MAX_CTX_RESETS="${SRB_AGENT_MAX_CTX_RESETS:-12}"
# Consecutive rounds that carried a gateway signature and produced nothing,
# before the loop stops waiting on it.  25 rounds at the 60-120s backoff is
# roughly 45 minutes of patience, which outlasts any capacity blip observed
# against these gateways, and the counter resets on the first round that works.
# "reached the gateway" is deliberately NOT the test -- see gateway_error().
gw_stalls=0; MAX_GW_STALLS="${SRB_AGENT_MAX_GW_STALLS:-25}"
# Stall rounds to absorb before trying a fresh session.  Waiting is only the right
# response to a stall the gateway will get over by itself; the observed shape is
# instead a fixed point in the loop's own context size (see the branch), and 6
# rounds at ~130s is ~13 minutes of "maybe it is transient" before spending one
# reset to test that.  lang04 spent 43 rounds -- 1.55h -- never testing it.
GW_STALL_ROTATE="${SRB_AGENT_GW_STALL_ROTATE:-6}"
# ...and how many times this path may do it.  A FRESH session's first round is
# small by construction, so if that one also stalls, context size was not the
# constraint and rotating again only re-buys the same disproof while costing the
# model its memory.  Two attempts, then fall through to MAX_GW_STALLS and call it
# what it is.  Worst case 6+6+25 = 37 stall rounds, ~80 min, and the give-up path
# keeps its meaning.  Rotations here also draw on the shared MAX_CTX_RESETS budget,
# because the cost being bounded there -- a run thrashing its own memory -- is the
# same cost however the rotation was decided.
gw_rotations=0; MAX_GW_ROTATIONS="${SRB_AGENT_MAX_GW_ROTATIONS:-2}"
# Consecutive rounds that produced no work and named no gateway fault.  Same
# order as MAX_GW_STALLS because it bounds the same kind of thing -- a failure
# that repeats identically -- and generous on purpose: at ~140s a round that is
# ~1h, and a barren streak can still recover (13 consecutive zero-tool rounds
# once did).  These two counters partition the no-work rounds between them: with
# a signature the run is blocked (`gateway`), without one the model answered and
# did nothing (`no-progress`), and only the second is charged to the model.
dead_rounds=0; MAX_DEAD_ROUNDS="${SRB_AGENT_MAX_DEAD_ROUNDS:-25}"
FIRST_ROUND=1
if [ -d "$LOG_DIR" ]; then
    highest=$(ls "$LOG_DIR" 2>/dev/null | sed -n 's/^events-\([0-9]\{1,\}\)\.jsonl$/\1/p' | sort -n | tail -1)
    if [ -n "${highest:-}" ]; then FIRST_ROUND=$(( highest + 1 )); fi
fi
LAST_ROUND=$(( FIRST_ROUND + MAX_ROUNDS - 1 ))
resume_prompt="$CONTINUE_PROMPT"

for round in $(seq "$FIRST_ROUND" "$LAST_ROUND"); do
    now=$(date +%s); elapsed=$(( now - START )); remaining=$(( DEADLINE_SEC - elapsed ))
    if [ "$remaining" -le 60 ]; then
        echo "srb-claude: deadline reached after ${elapsed}s" >&2
        echo "deadline" > "$LOG_DIR/stop-reason.txt"; mirror; break
    fi
    round_budget="$remaining"; capped=0
    if [ "$MAX_ROUND_SEC" -gt 0 ] && [ "$remaining" -gt "$MAX_ROUND_SEC" ]; then
        round_budget="$MAX_ROUND_SEC"; capped=1
    fi
    echo "srb-claude: round ${round} starting (elapsed ${elapsed}s, budget ${round_budget}s, session $(session_of "$LOG_DIR/events-$((round-1)).jsonl" 2>/dev/null || echo new))" >&2
    round_start=$(date +%s); rc=0

    # Round 1 hands the instruction; later rounds resume the session.  A round
    # following a context-window overflow starts a *new* session instead: no
    # --continue, and the instruction re-handed under FRESH_SESSION_PROMPT, since
    # the model it is handed to has no memory of the task or of its own plan.
    # Two branches set fresh_session, for different reasons -- an overflow the CLI
    # reported, and a gateway stall the loop diagnosed -- so the reason travels in
    # fresh_reason.  It is not decoration: this line is the only place the log says
    # why a transcript restarted, and naming the wrong cause sends the next reader
    # looking for an overflow that never happened.
    if [ "$round" -eq "$FIRST_ROUND" ]; then
        prompt="$INSTRUCTION"; resume_args=""
    elif [ "$fresh_session" = "1" ]; then
        prompt_file="$LOG_DIR/.fresh-prompt-${round}.txt"
        { printf '%s' "$FRESH_SESSION_PROMPT"; cat "$INSTRUCTION" 2>/dev/null; } > "$prompt_file"
        prompt="$prompt_file"; resume_args=""
        fresh_session=0
        echo "srb-claude: round ${round} starts a fresh session (${fresh_reason:-reason unrecorded})" >&2
    else
        prompt_file="$LOG_DIR/.resume-prompt-${round}.txt"
        printf '%s' "$resume_prompt" > "$prompt_file"
        prompt="$prompt_file"; resume_args="--continue"
    fi

    timeout "$round_budget" claude -p --verbose --output-format stream-json \
        $EFFORT_FLAG --model "$MODEL" --permission-mode bypassPermissions \
        --disallowed-tools "$DENIED_TOOLS" \
        $resume_args -- "$(cat "$prompt" 2>/dev/null)" \
        > "$LOG_DIR/events-${round}.jsonl" 2> "$LOG_DIR/stderr-${round}.log"
    rc=$?
    round_sec=$(( $(date +%s) - round_start ))
    echo "srb-claude: round ${round} exited rc=${rc} after ${round_sec}s" >&2
    echo "$rc" > "$LOG_DIR/rc-${round}.txt"; mirror

    # The init event names what the round was actually offered, which is the only
    # place the deny can be read back.  A web tool there is not a measurement of an
    # offline agent, so stop rather than record one.  Read with shell expansion so
    # the result does not depend on a pipeline's exit status under `pipefail`.
    init=$(head -1 "$LOG_DIR/events-${round}.jsonl" 2>/dev/null || true)
    case "$init" in *'"subtype":"init"'*)
        offered=${init#*'"tools":['}; offered=${offered%%]*}
        case "$offered" in *'"Web'*)
            echo "srb-claude: round ${round} was offered a web tool" >&2
            echo "web-tools" > "$LOG_DIR/stop-reason.txt"; mirror; break ;;
        esac ;;
    esac

    [ "$rc" -ne 124 ] && wedges=0

    # Every counter that ends a phase on a REPEATED fault is cleared here, on the
    # good path, for the same reason wedges is cleared on the line above: each one
    # is incremented inside a failure branch, and each is reset only inside one, but
    # a round that SUCCEEDS returns to the top of the loop from the rc=0 block just
    # below and reaches none of them.  So a recovery did not clear anything, and a
    # gateway that wobbled every other round accumulated toward a bound whose own
    # message says "consecutive": at MAX_GW_STALLS=25 a run that recovered 25 times
    # would be ended as a gateway the harness gave up on, with the model working and
    # the deadline unspent, and at MAX_FAST_FAILS=5 -- five short failures anywhere
    # in a 30-hour run -- it would be ended as "infrastructure", the verdict that
    # says the image is broken and the run is worth relaunching from scratch.
    # wedges was right and the other three were wrong, which is the whole of the bug.
    #
    # Two campaigns could not have caught this: opus (70 rounds) and glm-5.2 (87
    # rounds) recorded 331 resumed rounds and zero API errors between them, so every
    # one of these branches has only ever run in this file's own tests -- and every
    # fixture there failed FOREVER, which is exactly the shape that cannot see a
    # counter survive a recovery.
    #
    # produced_work rather than rc, for the two counters that are about work: a
    # round can exit 0 having said nothing (the idle bound below is what covers
    # that), and one can exit nonzero after real work before the stream broke.
    if [ "$rc" -eq 0 ]; then
        # A completed round is not a short failure by any reading.
        fast_fails=0
        if produced_work "$LOG_DIR/events-${round}.jsonl"; then
            gw_stalls=0; dead_rounds=0
        fi
    fi

    if [ "$rc" -eq 0 ]; then
        # A completed turn: did the model claim completion?  See declared_done()
        # above for why the sentinel counts only inside the `result` record.
        if declared_done "$LOG_DIR/events-${round}.jsonl"; then
            echo "completed" > "$LOG_DIR/stop-reason.txt"
            echo "srb-claude: harness declared completion after round ${round}" >&2
            mirror; break
        fi
        new_fp="$(tree_fingerprint)"
        if [ "$new_fp" = "$fingerprint" ]; then
            idle_turns=$(( idle_turns + 1 ))
            echo "srb-claude: round ${round} changed nothing (idle ${idle_turns}/${MAX_IDLE_TURNS})" >&2
            if [ "$idle_turns" -ge "$MAX_IDLE_TURNS" ]; then
                echo "idle" > "$LOG_DIR/stop-reason.txt"
                echo "srb-claude: ${idle_turns} idle rounds; treating phase as over" >&2
                mirror; break
            fi
        else
            idle_turns=0; fingerprint="$new_fp"
        fi
        resume_prompt="$TURN_ENDED_PROMPT"; mirror; sleep 5; continue
    fi

    resume_prompt="$CONTINUE_PROMPT"
    if [ "$rc" -eq 124 ]; then
        if [ "$capped" = "1" ]; then
            # Only a round that produced nothing is a wedge; see srb-agent-loop.sh,
            # where counting a round that was still answering stopped a working
            # phase as an infrastructure fault 95 minutes into a 40-hour budget,
            # and where all 30 capped rounds in that fleet had produced output.
            # produced_work, not reached_gateway: a round whose stream is only
            # errors reached the gateway and still did nothing, and excusing it
            # here is how a phase spends its entire budget on an unsendable
            # request with the wedge counter sitting at zero.
            if produced_work "$LOG_DIR/events-${round}.jsonl"; then
                echo "srb-claude: round ${round} was still answering when the ${round_budget}s cap fired; dropped and resuming, not counted as a wedge (wedges stay ${wedges}/${MAX_WEDGES})" >&2
                mirror
                sleep 5
                continue
            fi
            wedges=$(( wedges + 1 ))
            echo "srb-claude: round ${round} produced nothing for ${round_budget}s and was dropped (wedge ${wedges}/${MAX_WEDGES})" >&2
            mirror
            if [ "$wedges" -ge "$MAX_WEDGES" ]; then
                echo "infrastructure" > "$LOG_DIR/stop-reason.txt"; mirror; break
            fi
            sleep 60; continue
        fi
        echo "deadline" > "$LOG_DIR/stop-reason.txt"; mirror; break
    fi

    # A transcript that outgrew the window is checked before the transient-error
    # branches, and regardless of how long the round lasted: the overflow can end
    # a round in one second (the request is refused outright) or after several
    # minutes of real work (the turn that crosses the limit is the one refused),
    # and both leave a session that can never be resumed again.
    if ctx_window_exceeded "$LOG_DIR/events-${round}.jsonl"; then
        resets=$(( resets + 1 ))
        echo "srb-claude: round ${round} overflowed the model context window (reset ${resets}/${MAX_CTX_RESETS}); the next round starts a fresh session" >&2
        if [ "$resets" -ge "$MAX_CTX_RESETS" ]; then
            echo "context-window" > "$LOG_DIR/stop-reason.txt"
            echo "srb-claude: ${resets} context-window resets; the session cannot be kept inside the window" >&2
            mirror; break
        fi
        fresh_session=1; fresh_reason="transcript outgrew the context window"
        FRESH_SESSION_PROMPT="${FRESH_SESSION_CAUSE_CTX}${FRESH_SESSION_BODY}"
        # Not a fast fail: the round was refused for its size, not by a fault in
        # the harness, and the counter that ends the phase on a config fault must
        # not be advanced by it.
        fast_fails=0
        mirror; sleep 5; continue
    fi

    # The trigger is "answered but did nothing", NOT "failed quickly".  A duration
    # test cannot see this shape at all: Claude Code retries a 5xx internally
    # before surfacing it, so lang07 on 2026-08-09 spent rounds 3 through 9 on
    # 524s at 155-156s each -- eight times MIN_ROUND_SEC -- with gw_stalls and
    # fast_fails both parked at 0 and nothing bounding it but the 400-round cap.
    # 18 minutes lost there; on a run whose gateway did not recover it would have
    # been the whole deadline, reported as "deadline" with no cause named.
    # produced_work is the right second half, and only became able to answer this
    # once it stopped counting the CLI's own <synthetic> error event as work.
    # gateway_error, not reached_gateway: the latter is satisfied by the result
    # record every round writes, so it gated this branch on nothing and let it
    # file a silent model under `gateway`.  Requiring a signature is what makes
    # the label mean what it says -- and what makes the no-work counter below
    # reachable at all, since this branch ends in `continue`.
    if gateway_error "$LOG_DIR/events-${round}.jsonl" \
       && ! produced_work "$LOG_DIR/events-${round}.jsonl"; then
        # Transient by assumption: a 429/503/dropped stream clears on its own, so
        # this branch waits rather than counting.  Bound it anyway -- an error that
        # reaches the gateway and repeats identically forever is not transient, and
        # before this bound existed such a round could hold a 30-hour deadline at
        # zero progress.  The cap is generous (a real capacity outage recovers well
        # inside it -- every 524-hit run in that fleet came back within 7 rounds:
        # fw03 at 4, fw06 at 3, lang03 at 5, fw05 at 5, lang07 at 10) and ends the
        # phase with its own stop reason, so the run is reported as blocked on the
        # gateway rather than as work the model chose not to do.  25 dead rounds is
        # ~45 min when they fail fast and ~90 min at lang07's 156s, both well past
        # anything these gateways have needed.
        gw_stalls=$(( gw_stalls + 1 ))
        # Not "failed fast": this branch stopped being duration-gated, and saying
        # otherwise in the log misdates the fault -- lang07's rounds took 129-217s
        # and a reader who trusts the word "fast" looks for a config fault instead
        # of an unanswerable request.  Say what was actually tested.
        echo "srb-claude: round ${round} reached the gateway but produced no work in ${round_sec}s; backing off (${gw_stalls}/${MAX_GW_STALLS})" >&2

        # Waiting is the wrong and only response this branch used to have, because
        # the commonest shape of this stall is NOT transient -- it is a fixed point
        # the loop holds itself in.
        #
        # Measured on lang04 (sonnet-5/max, 2026-08-10), 40 consecutive no-work
        # rounds (r70-r109): each round re-sends ~150K tokens (median 150,200
        # read from the ASSISTANT record -- the result event of a dropped round
        # reports all zeros, because usage never comes back), the model thinks
        # for ~132s, this gateway strips thinking text so not one byte reaches
        # the client, an idle read closes the stream, and the round yields a
        # single empty thinking block carrying a signature.
        #
        # Waiting does not hold the terms constant -- it makes them worse.  Over
        # those 40 rounds the request grew on 37 of 37 steps, strictly monotone,
        # 147,773 -> 153,259 (~148 tokens/round): the empty block and the error
        # are themselves appended to the transcript.  So the loop is not sitting
        # at a fixed point it could wait out; it is climbing, slowly, in the one
        # direction that cannot help.  Round duration over the window: median
        # 132.0s, range 130-176s, and not one of the 40 produced work.
        #
        # It is escapable, and cheaply.  A fresh session against the SAME
        # container, gateway, model and --effort max returned in 6.6s with
        # cache_creation 45537 and a real text block.  Context size is the whole
        # difference, and a fresh session is the one lever this loop has on it.
        # (Work-producing rounds prove the connection itself is fine: they carry
        # the same conn-closed signature and ran as long as 1592s, because bytes
        # were flowing.)
        #
        # So rotate before giving up, and keep the give-up path for the case that
        # rotation does not fix -- a gateway that refuses a 45K request too is
        # genuinely down, and that is what MAX_GW_STALLS should mean.  The
        # transcript is the only thing a rotation costs, the repo on disk is
        # untouched, and FRESH_SESSION_PROMPT already says so to the model.
        if [ "$gw_stalls" -ge "$GW_STALL_ROTATE" ] \
           && [ "$gw_rotations" -lt "$MAX_GW_ROTATIONS" ] \
           && [ "$resets" -lt "$MAX_CTX_RESETS" ]; then
            fresh_session=1
            fresh_reason="${gw_stalls} rounds stalled at the gateway with no work; shrinking the request"
            FRESH_SESSION_PROMPT="${FRESH_SESSION_CAUSE_GW}${FRESH_SESSION_BODY}"
            gw_rotations=$(( gw_rotations + 1 )); resets=$(( resets + 1 )); gw_stalls=0
            echo "srb-claude: ${GW_STALL_ROTATE} rounds stalled at the gateway with no work; rotating to a fresh session (gateway rotation ${gw_rotations}/${MAX_GW_ROTATIONS}, reset ${resets}/${MAX_CTX_RESETS}) because a smaller request is the one thing not yet tried" >&2
            mirror; sleep 30; continue
        fi
        if [ "$gw_stalls" -ge "$MAX_GW_STALLS" ]; then
            echo "gateway" > "$LOG_DIR/stop-reason.txt"
            { echo "srb-claude: ${gw_stalls} consecutive rounds reached the gateway and produced no work, including after ${resets} fresh session(s); giving up on it."
              tail -20 "$LOG_DIR/stderr-${round}.log" 2>/dev/null; } >&2
            mirror; break
        fi
        sleep $(( round < 10 ? 60 : 120 )); continue
    fi
    gw_stalls=0
    if [ "$round_sec" -lt "$MIN_ROUND_SEC" ]; then
        fast_fails=$(( fast_fails + 1 ))
        echo "srb-claude: round ${round} failed in ${round_sec}s (fast fail ${fast_fails}/${MAX_FAST_FAILS})" >&2
        if [ "$fast_fails" -ge "$MAX_FAST_FAILS" ]; then
            echo "infrastructure" > "$LOG_DIR/stop-reason.txt"
            { echo "srb-claude: ${fast_fails} fast failures; harness/config fault."; tail -20 "$LOG_DIR/stderr-${round}.log" 2>/dev/null; } >&2
            mirror; break
        fi
    else
        fast_fails=0
    fi

    # Last resort: the no-work round that named no cause.
    #
    # This is the complement of the gw_stalls branch above, not a superset of it.
    # That branch takes every no-work round carrying a gateway signature and ends
    # in `continue`, so what arrives here is the residue: the model answered, the
    # transport reported nothing wrong, and no work came out.  That residue is the
    # only no-work shape chargeable to the model, which is why it gets a stop
    # reason (`no-progress`) that publishes rather than one that reads as a fault.
    #
    # It was originally written as the catch-all, because fast_fails is gated on
    # round_sec < MIN_ROUND_SEC and the wedge branch on rc=124, so a round that
    # failed SLOWLY and produced nothing reached no branch at all: both counters
    # were reset just above, and the loop slept and went round again.  lang07 (sonnet-5/max, 2026-08-09) spent 56 consecutive rounds
    # there -- rc=1 after 129-217s, each stream carrying one real assistant event
    # whose only block was a zero-length thinking block, then the CLI's synthetic
    # `Connection closed mid-response.` -- and would have reached MAX_ROUNDS with
    # every counter at 0, ~12 hours later, having produced nothing since round 17.
    #
    # Counting no-work rounds directly is what closes that hole.  Reset on any
    # round that produced work, so this cannot truncate a slow but working phase:
    # a barren streak is not by itself a fault (13 consecutive zero-tool rounds
    # once recovered on the 14th), which is why the bound is generous.
    #
    # produced_work, not "rc!=0": a round can exit nonzero having done real work
    # before the stream broke, and that round must clear the counter.
    if produced_work "$LOG_DIR/events-${round}.jsonl"; then
        # gw_rotations resets here and not in the rc=0 block above, because that
        # block is unreachable for the runs this bound exists for: lang04 had 106
        # rc=1 rounds and 3 rc=124 rounds out of 109, and not one rc=0 -- its work
        # rounds ended nonzero, having produced real work before the stream broke.
        # A reset placed there would have been a guard as unreachable as its branch.
        #
        # It resets at all because every bound in this loop counts CONSECUTIVE
        # failures, and a rotation that bought real work is not a failed attempt.
        # Uncounted, the cap would be on rotations per RUN rather than per stall,
        # and this stall is expected to recur: a rotated session drops the
        # transcript (the probe's first request was 45,537 tokens against 150,200
        # in the stalled rounds) and then grows again as work accumulates.  How
        # many rounds that takes is NOT measured -- no run in the 20-run campaign
        # this was written from ever rotated a session, so there is no observation
        # of a post-rotation climb, and lang04 is not one: it ran a single session
        # start to finish and its very first round already sent 164,580 tokens.
        # A long healthy run may therefore meet the branch several times
        # legitimately, and would be ended at the third onset as `gateway` -- a
        # fault verdict, which blanks the row of a run that was working.
        dead_rounds=0; gw_rotations=0
    else
        dead_rounds=$(( dead_rounds + 1 ))
        echo "srb-claude: round ${round} produced no work in ${round_sec}s (dead ${dead_rounds}/${MAX_DEAD_ROUNDS})" >&2
        if [ "$dead_rounds" -ge "$MAX_DEAD_ROUNDS" ]; then
            echo "no-progress" > "$LOG_DIR/stop-reason.txt"
            { echo "srb-claude: ${dead_rounds} consecutive rounds produced no work; ending the phase."
              tail -20 "$LOG_DIR/stderr-${round}.log" 2>/dev/null; } >&2
            mirror; break
        fi
    fi
    sleep $(( round < 10 ? round * 5 : 60 ))
done

# The for loop can also end by exhausting MAX_ROUNDS.  Every stop-reason above is
# written inside a `break`, so that exit used to leave no stop-reason.txt at all --
# and a missing stop reason is read downstream as "the harness never finished",
# which is a different claim from "the round budget ran out".  Name it.
if [ ! -s "$LOG_DIR/stop-reason.txt" ]; then
    echo "max-rounds" > "$LOG_DIR/stop-reason.txt"
    echo "srb-claude: round budget (${MAX_ROUNDS}) exhausted" >&2
fi

echo "srb-claude: agent phase over" >&2
date +%s > "$LOG_DIR/finished-at.txt"; mirror
