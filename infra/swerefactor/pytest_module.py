"""A pytest plugin that makes a pytest directory a behavioural module.

Four tasks already own tens of thousands of validated pytest cases.  Rewriting
them to emit JSON by hand would be a large edit with nothing to show for it, so
instead pytest is taught to write the module contract:

    pytest -p swerefactor.pytest_module ...

Every collected test becomes one check.  ``nodeid`` is the check id, which makes
the report diffable against a plain pytest run and lets a failure be reproduced
by copying the id back onto a command line.

Markers let a task say which of its cases score, and how a skip is recorded::

    @pytest.mark.srb_weight(0)         # reported, not scored
    @pytest.mark.srb_skip_ok           # this skip keeps its own wording

``srb_weight`` carries one bit.  Zero takes the check out of its module's
denominator -- for an observation, a case that reports what it found without
judging it -- and any positive value is one scored check like every other, since
``result.pooled`` counts checks rather than summing weights and a module divides
its declared share equally among them.  A zero is worth a ``srb_note`` saying
why, or the reader cannot tell it from a mistake.

Every skip costs its module credit — ``result.pooled`` counts it and none of them
passes — because the environment is fixed and offline: nothing is skipped unless
the artifact under test was never produced.  ``srb_skip_ok`` decides how the skip
is *recorded*, not whether it is charged: with the marker the check keeps the
suite's own reason and ``permitted_skip``, without it the record is rewritten to
``fail`` and says the contract does not permit it.

The marker used to buy an exemption, for conditional checks — "if you implement
this optional macro, keep State A's value".  That was two different situations
under one word.  Where the *reference* has no answer, the check is not a question
at all and has been deleted from its suite; where the reference does have one, a
submission that cannot be asked has failed to produce the thing being asked
about, and the model that did produce it should not be the only one paying for
the check.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from typing import Any

MARKERS = (
    "srb_weight(value): 0 to report this test without scoring it (default 1.0)",
    "srb_skip_ok: this skip keeps the suite's own reason instead of being rewritten",
    "srb_group(name): a sub-group label carried into the report's metadata",
)

#: Set at run time rather than by a marker, through
#: ``request.node.user_properties.append(("srb_note", ...))``.  A weight decided
#: by what the tree turns out to be cannot be a collection-time marker, and the
#: reason for it belongs next to the weight in the report; see ``_Recorder``.
NOTE_PROPERTY = "srb_note"


def pytest_configure(config):
    for marker in MARKERS:
        config.addinivalue_line("markers", marker)
    config._srb = _Recorder(os.environ.get("SRB_RESULT", ""))


def pytest_unconfigure(config):
    rec = getattr(config, "_srb", None)
    if rec is not None:
        rec.flush()


def pytest_runtest_logreport(report):
    config = getattr(report, "_srb_config", None) or _current_config()
    rec = getattr(config, "_srb", None) if config else None
    if rec is None:
        return
    rec.observe(report)


# pytest does not hand the config to logreport, so keep the last one configured.
_CONFIG = None


def pytest_cmdline_main(config):  # pragma: no cover - wiring
    global _CONFIG
    _CONFIG = config


def _current_config():
    return _CONFIG


class _Recorder:
    """Accumulates one record per test, then writes the module result once.

    A test can report twice (a setup failure and a teardown failure), and the
    first decisive record wins: a test that failed in setup did not also pass.
    """

    def __init__(self, out_path: str) -> None:
        self.out_path = out_path
        self.records: dict[str, dict[str, Any]] = {}

    def observe(self, report) -> None:
        nodeid = report.nodeid
        if report.when == "call":
            verdict = {"passed": "pass", "failed": "fail",
                       "skipped": "skip"}.get(report.outcome, "error")
        elif report.when == "setup":
            if report.outcome == "failed":
                verdict = "error"      # never reached the assertion
            elif report.outcome == "skipped":
                verdict = "skip"
            else:
                return
        elif report.when == "teardown" and report.outcome == "failed":
            verdict = "error"
        else:
            return

        existing = self.records.get(nodeid)
        if existing and existing["verdict"] != "pass":
            return  # keep the first decisive verdict

        keywords = set(getattr(report, "keywords", {}) or {})
        weight = 1.0
        note = ""
        # Markers do not survive onto the report, but their names do; a weight
        # needs its argument, so it is passed through user_properties instead.
        for name, value in getattr(report, "user_properties", []) or []:
            if name == "srb_weight":
                try:
                    weight = max(0.0, float(value))
                except (TypeError, ValueError):
                    pass
            elif name == "srb_note":
                note = str(value)
        record: dict[str, Any] = {
            "id": nodeid,
            "verdict": verdict,
            "weight": weight,
            "summary": _summary(report, verdict),
            "duration_sec": round(getattr(report, "duration", 0.0) or 0.0, 3),
        }
        if verdict == "skip" and "srb_skip_ok" not in keywords:
            # A skip that the contract did not license is a miss, and saying so
            # here rather than in the scorer keeps the policy in one place.
            #
            # Both fields are written unconditionally.  `_summary` returns the
            # truthy word "skipped" for a skip, so the `or` fallback that used to
            # stand here could never fire, and the detail below was guarded on a
            # local `verdict` this branch does not update -- so every unlicensed
            # skip reached the report, and the stage-1 reviewer's prompt, as the
            # bare word "skipped" with pytest's reason discarded.  A check that
            # costs its module credit has to say why it cost it.
            #
            # The detail says three things because two branches each needed a
            # different one of them: that the check did not run, that the reason
            # it is a miss rather than neutral is the missing marker, and the
            # author's own explanation -- which in lang04's case names the check
            # reporting the same fact as a proper observation, so a reader does
            # not count it twice.
            reason = _skip_reason(report)
            record["verdict"] = "fail"
            record["summary"] = ("skipped, which the contract does not permit"
                                 + (": " + reason if reason else ""))
            record["detail"] = (
                "This check did not run, and carries no srb_skip_ok marker, so "
                "the harness records it as a miss rather than as neutral. The "
                "environment is fixed and offline, so nothing here is skipped "
                "for environmental reasons: a skip means the artifact under "
                "test was never produced."
                + ("\nThe suite's own reason: " + reason if reason else ""))

        elif verdict == "skip":
            record["metadata"] = {"permitted_skip": True}
            reason = _skip_reason(report)
            if reason:
                record["summary"] = "skipped: " + reason
        # A note travels with the check rather than only into the junit XML,
        # which lives in $SRB_WORK and is discarded with the container.  It is
        # what makes a `weight: 0.0` entry self-explaining: a check that scores
        # nothing has to say why in the report a reviewer actually reads, or the
        # zero is indistinguishable from a mistake.
        if note:
            record.setdefault("metadata", {})["note"] = note[:600]
        # Read the recorded verdict, not the local one: the skip branch above
        # rewrites a skip to "fail", and testing the local variable is what left
        # those entries with no detail at all.  The second half of the condition
        # is what keeps that fix from causing the harm the other branch guarded
        # against by testing the local variable instead -- a reclassified skip
        # has already written its own detail, and its longrepr is the
        # (path, lineno, reason) triple, so overwriting would both discard the
        # explanation and put a verifier path into a graded payload.  Guarding on
        # the written field rather than on the verdict holds for any later branch
        # that writes a detail too.
        if record["verdict"] in ("fail", "error") and "detail" not in record:
            text = str(getattr(report, "longrepr", "") or "")
            # A `fail` is an assertion the author wrote, and the traceback framing
            # around it -- the `file.py:NNN: in test_name` location and the echoed
            # source -- is what would carry the verifier's own line numbers into a
            # model's prompt (see ``authored``).  An `error` is an exception nobody
            # planned, where the traceback *is* the information: a check that raises
            # on its first line is a broken check, and diagnosing that needs the
            # frames.  So they are treated differently rather than uniformly
            # stripped.
            #
            # `_e_block` and not `authored` for the fail: the summary wants the one
            # sentence the author wrote, but the detail is the field that has to
            # keep pytest's `assert 1 == 2` rewrite and its `+ where` operands.
            # That rewrite is the most useful line in a failure whose own message
            # says only that two things differ, it names no line number, and a test
            # pins that it survives here.  Narrowing this to `authored` as well
            # would strip it in service of a rule it isn't the subject of.
            #
            # `_detail` wraps both branches and is a separate question from either
            # -- which characters to drop when the block is too long, rather than
            # which lines belong in it.  Two branches wrote one of these each and
            # they compose: choose the content, then trim it from the middle so
            # that neither the frames nor the closing sentence is the half that
            # goes.
            #
            # A third branch arrived here dropping the block's FIRST line as well,
            # on the ground that it is already the summary and so spends the 240
            # characters `scan.digest` allows a detail on saying it twice.  The
            # measurement was real and it is not fixed here, deliberately: it is
            # fixed at the consumer, by `scan._detail_beyond_summary`, which strips
            # the repeated headline after the frame has gone and before the 240-char
            # cut.  That is the right layer for it -- the report prints summary and
            # detail in different places and wants both, and `_detail_beyond_summary`
            # names the further reason not to move it here, which is that stage 2's
            # identity run is pinned to the number this artifact records.  Dropping
            # line one at the producer would also empty the detail of a bare
            # `assert 2 == 3`, whose E block is that one line.
            if verdict == "error":
                chosen = _detail(text)
            else:
                chosen = _detail(_e_block(text) or text)
            # Written only when there is something to write.  A `fail` whose
            # longrepr pytest never formatted has no evidence to show, and an absent
            # key says that, where `detail: ""` reads as a detail that was computed
            # and came out blank.  From the same third branch; it costs nothing,
            # since every reader of this field already uses `.get`.
            if chosen:
                record["detail"] = chosen
        self.records[nodeid] = record

    def flush(self) -> None:
        if not self.out_path:
            return
        payload = {
            "checks": list(self.records.values()),
            "metadata": {
                "runner": "pytest",
                "tests": len(self.records),
            },
        }
        directory = os.path.dirname(self.out_path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=1)
        os.replace(tmp, self.out_path)


def _skip_reason(report) -> str:
    """The reason pytest recorded for a skip, without the machine's path.

    A skip's ``longrepr`` is the 3-tuple ``(path, lineno, "Skipped: <reason>")``,
    and that path is the build machine's absolute path -- it must not travel into
    a graded payload, so only the reason survives.  The line number is dropped
    for the same reason: it pins the reference's source layout.

    Four things pytest writes are not reasons, and each was found separately.
    ``unconditional skip`` is its filler for a bare ``@pytest.mark.skip``.  A
    bare ``pytest.skip()`` renders as the word ``Skipped`` alone, which passed
    through as a reason reads as though one were given.  ``longrepr`` is not
    always the triple, so a non-tuple value is stringified rather than dropped.
    And a ``skipif`` keeps its reason nowhere in ``longrepr`` at all -- it is in
    the marker, which the plugin records as a ``srb_skip_reason`` user property.

    The bare reason, not a sentence: three callers embed it in three different
    sentences ("did not apply: X", "the suite's own reason: X", and the
    unlicensed-skip detail), and each ends up saying nothing twice if this
    returns prose.  ``""`` means no reason was given, which the callers spell.
    """
    raw = getattr(report, "longrepr", None)
    if isinstance(raw, (tuple, list)) and len(raw) >= 3:
        text = str(raw[2] or "")
    elif raw is not None:
        text = str(raw)
    else:
        text = ""
    text = text.strip()
    for prefix in ("Skipped:", "Skipped "):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
            break
    if text.rstrip(":").strip().lower() in ("", "skipped", "unconditional skip"):
        text = ""
    if not text:
        # Where a `skipif` keeps its reason: the marker, surfaced as a user
        # property.  Checked after `longrepr` and not before, because a
        # `pytest.skip()` call inside the test is the more specific statement.
        for name, value in getattr(report, "user_properties", None) or []:
            if name == "srb_skip_reason":
                text = str(value).strip()
                break
    return text[:240]


#: A pytest traceback's location line: ``test_closure.py:105: in test_name``.
_LOCATION = re.compile(r"^\S+\.py:\d+: in \S+\s*$")


def authored(text: str) -> str:
    """The message the check's author wrote, without pytest's framing.

    A pytest failure arrives as a traceback: a ``file.py:NNN: in test_name``
    location, an echo of the asserted source, then an ``E``-prefixed block holding
    the author's message followed by pytest's own restatements of the expression.
    Only the author's message is a finding.

    The rest is not merely noise. These checks reach a model: stage 1 hands the
    scan's findings to the reviewer as a digest, and both stages put them in the
    report. The location line pins the verifier's own line numbers into a graded
    payload, so reformatting a test file changes what the model reads; the echoed
    expression is the check's assertion *spelling*, and the argument stage 1 is
    built on is that a spelling is the one thing a shortcut can afford to change.
    Neither belongs in front of the reviewer being asked to judge the tree itself.
    """
    if not text:
        return ""
    kept: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or _LOCATION.match(line):
            continue
        if not line.startswith("E"):
            continue                       # the echoed source, before the E block
        body = line[1:].strip()
        if not body:
            continue
        # pytest's restatements: the asserted expression, and the `+ where ...`
        # explanation of its operands.  Interior spacing varies between them, so
        # these match on the lead alone.
        if body.startswith("assert ") or body.startswith("+"):
            continue
        # The exception's own name is kept, ``AssertionError:`` included.  Stripping
        # that one while leaving ``TypeError: yargs.config is not a function`` is a
        # rule with no edge: both are pytest printing the raised exception, and for
        # every exception that is not AssertionError the name *is* the finding.  A
        # reviewer reading a digest of headlines also has nothing else to tell an
        # assertion apart from a crash, which is the first thing they need to know.
        if body:
            kept.append(body)
    return "\n".join(kept).strip()


def _e_block(text: str) -> str:
    """The whole ``E`` block: the author's message *and* pytest's restatements.

    What ``detail`` wants, where ``authored`` is what ``summary`` wants.  The two
    differ on one thing only -- whether pytest's ``assert ...`` rewrite and its
    ``+ where ...`` operands are kept -- and both callers are right about their own
    field.  A headline is the sentence a human wrote; a detail is everything that
    helps diagnose it, and the rewrite showing the operands' actual values is the
    most useful line in a failure whose message says only "they differ".

    What neither wants is the framing *around* the block: the
    ``file.py:NNN: in test_name`` location and the echoed source.  Those pin the
    verifier's own line numbers and its assertion's spelling into a graded payload,
    so reformatting a test file would change what a reviewing model reads.  That is
    the whole of ``authored``'s argument, and it applies here unchanged -- it is
    only the restatements that this keeps and that one drops.
    """
    if not text:
        return ""
    kept: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or _LOCATION.match(line) or not line.startswith("E"):
            continue
        body = line[1:].strip()
        if body:
            kept.append(body)
    return "\n".join(kept).strip()


#: A line that is only a place: ``file:///app/src/cli/index.js:10``, or
#: ``src/cli/index.js:10:6``.  No spaces, ending in a line (and maybe a column)
#: number.
_BARE_LOCATION = re.compile(r"^\S+:\d+(?::\d+)?$")

#: A line that states a cause: an identifier, a colon, a *space*, then prose --
#: ``TypeError: yargs.config is not a function``,
#: ``harness.runner.ServerFailed: launcher exited with 1``.  The required space is
#: what keeps ``file:///tmp/x.js:10`` out, since a URL's colon is followed by a
#: slash; that one character is the whole difference between the two shapes.
_CAUSE_LINE = re.compile(r"^[A-Za-z_][\w.]*: \S")

#: How far past the location to look for the cause.  Node puts four lines between
#: them (the offending source line and a caret rule), so this is that with room to
#: spare and not enough to wander into a server log.
_CAUSE_WINDOW = 6

#: The bracket that opens a dump the sentence is introducing, with the colon that
#: introduces it: ``header mismatch: {``, ``offenders: [``.  Only *opening*
#: brackets, and only ``[``/``{``, which are the two shapes measured -- a mapping
#: dump and a list dump.  A trailing ``(`` was not observed, and widening the rule
#: on speculation would put every sentence that legitimately ends in a parenthesis
#: one typo away from being truncated.
_DUMP_OPENER = re.compile(r"\s*:?\s*[\[{]$")


def _drop_dump_opener(head: str) -> str:
    """Drop a trailing dump bracket, and the colon that introduced it.

    ``compare.py`` writes ``assert False, f"{case}::{name}: header mismatch"`` and
    then dumps the two header sets, so on 22 of one fw05 run's 47 failures the
    author's sentence and the dump's first brace shared a line.  The sentence is
    the finding; the brace is punctuation belonging to the lines below it.

    Never returns empty: a headline that is *only* a bracket is useless, but an
    empty one is worse, so that degenerate case keeps what it had.
    """
    return _DUMP_OPENER.sub("", head) or head


def _headline(written: str, limit: int = 300) -> str:
    """Line one of the author's message -- plus line two, when line one only announces.

    A sentence ending in a colon is not a finding.  It is a promise that the finding
    follows, and ``assert not offenders, f"<sentence>:\\n{detail}"`` is the natural
    idiom for writing one: 53 sites across five of this repo's tasks use it.  Taking
    line one alone turns every one of them into a headline that names a category and
    withholds every particular.

    Measured on fw02's round-3 submission, which did not boot: 3672 checks reached
    the artifact with the summary ``session read did not start:``, the words
    ``launcher exited with 1 before becoming ready`` on line two, and
    ``TypeError: yargs.config is not a function`` a few lines below that.  The cause
    was in the detail 3672 times and in no headline once.  That matters more than it
    sounds, because stage 2's report prints no check summaries at all -- only the
    module table -- so for a reader of the report the colon was the whole story, and
    for a reviewer reading the scan's digest the headline is the line most likely to
    be the only one that survives (``scan.digest`` caps detail at 240 characters).

    The join is bounded by the caller's own budget, so a long line two is truncated
    rather than allowed to run.  It is deliberately one line and not the rest of the
    block: line three onwards is a server log or a JSON dump, and a headline that
    swallows a stack trace stops being a headline.

    One exception to "line two", and a Node crash is the reason.  Node reports an
    uncaught exception by printing the *location* first and the exception four lines
    later::

        node .../src/cli/bin.js --host 127.0.0.1 --port 36791 exited with 1 ...:
        file:///tmp/.../src/cli/index.js:10
        .config('config')
             ^
        TypeError: yargs.config is not a function

    so taking line two gave all seven of fw02's ``entrypoint`` checks a headline
    whose cause was a file URL -- and fix 18 then synthesised the module note "6 of
    6 failing checks end on the same cause: file:///...index.js:10", which names a
    place where it promises a cause.  When the continuation is only a location, the
    cause is looked for just below it and both are kept: the location is the more
    actionable half and is what a reader needs to open the file.
    """
    lines = [ln for ln in (written or "").splitlines() if ln.strip()]
    if not lines:
        return ""
    # Before the colon test, not after: a sentence that ends ``: {`` has already
    # been given its finding, and the line below it is the dump's first entry
    # rather than a continuation.  Dropping the brace first is what stops
    # ``header mismatch: {`` from being joined to ``'content-type': ...,``.
    head = _drop_dump_opener(lines[0].strip())
    if head.endswith(":") and len(lines) > 1:
        cont = lines[1].strip()
        if _BARE_LOCATION.match(cont):
            for ln in lines[2:2 + _CAUSE_WINDOW]:
                if _CAUSE_LINE.match(ln.strip()):
                    cont = f"{ln.strip()} (at {cont})"
                    break
        # And again after the join, for the other arrangement of the same two
        # parts: the sentence on line one, the bare brace on line two.
        head = _drop_dump_opener(f"{head} {cont}")
    return head[:limit]


#: Budget for a check's ``detail``, and how much of it the head gets.  Both
#: public, and the head is public because a test pins the exact prefix -- the
#: split is the property, so it has to be nameable from outside.
DETAIL_LIMIT = 1800
DETAIL_HEAD = 1200


def _detail(text: str, limit: int = DETAIL_LIMIT, head: int = DETAIL_HEAD) -> str:
    """The failure block, trimmed from the middle rather than from either end.

    This used to be ``text[-1800:]``.  A long block is long because the diff in
    it is long, and the diff is at the *bottom*: keeping the tail dropped the
    frames and the author's sentence, so a reader met a value with nothing
    saying what it was.  Measured on one run, ``test_body``'s detail began
    mid-word -- ``ty-name: body differs`` -- having lost the whole
    ``AssertionError:`` line to the cut.  Measured on another, a scan report that
    reached the budget arrived beginning mid-sentence at "which this scan cannot
    read", its ``REPORT (not a defect)`` headline -- the words that keep a report
    from being read as an accusation -- sliced off at the producer and
    unrecoverable downstream.

    Trimming the *head* instead is no better and fails the other way: an error
    carries its exception after however many frames it took to get there, so a
    head-only budget loses a ``ModuleNotFoundError`` six frames deep and reports
    a traceback with no error in it.  A failure puts its message at the top and
    an error puts it at the bottom, so both ends are kept and the middle goes.

    The head is the larger half because every consumer reads the front:
    ``scan.digest`` renders 240 characters into the reviewer's prompt,
    ``report.render`` the first six lines, ``scoring`` stores 2000.

    The marker counts against the budget rather than being added to it, so a
    result file does not grow by the number of failures it records.  It is
    marked at all so the gap is visible and cannot be read as the block itself;
    pytest elides inside its own diffs too, which is why this wording names
    ``srb`` and says which part went -- an unattributed ``...`` in a detail field
    is indistinguishable from one pytest wrote.

    ``limit`` and ``head`` are arguments as well as constants so a test can drive
    the boundary with a short string instead of building a 9000-character one.
    Nothing in the harness passes either; both callers take the defaults.
    """
    if len(text) <= limit:
        return text
    marker = "\n[srb: %d characters elided from the middle of this block]\n"
    # The count appears inside the marker and the marker's own length changes how
    # much is kept, so the two are solved together.  A handful of passes settles
    # it -- only the digit count moves -- and the loop exits on the count it
    # would print, so the number is the number actually dropped.  Two branches
    # each shipped a first draft that got this wrong in opposite directions --
    # one overstated by 31 characters, the other understated by exactly the
    # marker's length, saying 3000 where 3038 was true -- which is the argument
    # for solving it rather than estimating it.  A number a reader cannot check
    # is worse than no number, because they will check it.
    dropped = len(text) - limit
    keep_head = tail = 0
    for _ in range(4):
        room = limit - len(marker % dropped)
        keep_head = min(head, max(0, room))
        tail = max(0, room - keep_head)
        settled = len(text) - keep_head - tail
        if settled == dropped:
            break
        dropped = settled
    filled = marker % dropped
    return f"{text[:keep_head]}{filled}{text[len(text) - tail:]}" if tail \
        else f"{text[:keep_head]}{filled}"


def _summary(report, verdict: str) -> str:
    """The one line that says what went wrong, for a reader who sees only it.

    Takes the **first** ``E`` line, not the last.  pytest's ``E`` block is the
    author's message followed by pytest's own restatement of the assert, and a
    message that spans lines -- a header diff, a JSON dump of the offending
    mapping -- puts its closing bracket last.  Walking backwards therefore finds
    the bracket or the restatement rather than the sentence: measured on this
    run's 47 behavioural failures, the last ``E`` line was unusable in **22** of
    them (20 of those were literally ``]``) and the first in **none**.

    ``detail`` still carries the whole block, so nothing was lost -- but the
    summary is what a report and a reviewer headline show, and ``]`` tells a
    reader nothing about a real divergence.

    Measured a second time, independently, on an fw07 stage-1 result: 21 of the
    reviewer's headlines were pytest restating the assert.  A check whose author
    wrote ``assert not hits, "'getRequestURI(' appears in 1 delivered source
    file(s); an annotated handler does not need the raw path, a dispatcher does:
    ..."`` was headlined with the dict repr and the explaining sentence discarded.
    Two branches found this in two different tasks, which is the argument for
    stating the rule as ordering rather than as a fix for whichever shape each one
    happened to see.

    A third measurement, from lang01, says what the discarded part costs rather than
    how often it is discarded: of that task's four flagged stage-1 findings, three
    were headlined by the repr, and the one whose message ended "A macro-generated
    `mod` would produce the same report, so confirm before concluding" was a false
    positive.  The caveat that would have told the reviewer so was the part that did
    not survive -- so the tail of a long message is not a qualification the headline
    can afford to drop, which is also why continuations are joined rather than cut at
    the first physical row.

    One rule from the other branch is deliberately NOT here: it stripped a leading
    ``AssertionError:``, on the ground that every failed assert is one and the name
    buys nothing inside 300 characters.  ``authored`` keeps it, and that reason
    decided this: stripping that one name while keeping ``TypeError: yargs.config is
    not a function`` is a rule with no edge, and a reviewer reading a digest of
    headlines has nothing else to tell an assertion apart from a crash -- which is
    the first thing they need to know.  Both readings are defensible; this one is
    pinned by two tests and is what the merged tree already did.
    """
    if verdict == "pass":
        return ""
    text = str(getattr(report, "longrepr", "") or "")
    # First ``E`` line, not the last.  pytest writes the exception the test raised
    # at the top of that block and its own rewritten restatement of the assertion
    # underneath it, so the first line is the sentence the check's author wrote and
    # the last is a dict dump keyed on internal paths.  Reading the block backwards
    # turned "8 delivered source file(s) still import github.com/gin-gonic/gin"
    # into "assert not {'pkg/.../logger.go': [...], ...}", and elsewhere
    # `assert 'fastify' in {...}` in place of "the manifest does not declare
    # fastify".  This string is the headline a reviewer reads for every finding in
    # the stage-1 digest, so it has to be the one written to be read.  The rest
    # survives in ``detail``.
    #
    # Through `authored`/`_headline` rather than a bare scan for the first E line:
    # two branches fixed this and that pair also drops pytest's location line and
    # its `assert`/`+ where` restatements, and continues a headline that ends in a
    # colon onto its second line -- the `assert not offenders, f"<sentence>:\n..."`
    # idiom, 53 sites across five tasks, whose first line names a category and
    # withholds every particular.
    written = authored(text)
    if not written:
        # A bare `assert 2 == 3` has no authored message at all: every line of its
        # E block is the restatement `authored` exists to drop, so it returns "".
        # pytest's rewrite is then the only thing that says anything, and the next
        # fallback below would reach past it to the traceback's last line -- which
        # is the location, `test_sample.py:20: AssertionError`, a headline naming
        # a line number in the verifier's own source and no finding whatsoever.
        for raw in text.splitlines():
            line = raw.strip()
            if line.startswith("E ") or line.startswith("E\t"):
                written = line[2:].strip()
                break
    if written:
        return _headline(written)
    if verdict == "skip":
        # The prose is composed here, not in `_skip_reason`: that returns the bare
        # reason because two other callers embed it in sentences of their own, and a
        # helper that returns a finished sentence cannot be embedded in one.
        reason = _skip_reason(report)
        return (f"did not apply: {reason}"[:300] if reason
                else "skipped, with no reason recorded")
    return text.strip().splitlines()[-1][:300] if text.strip() else verdict

def pytest_collection_modifyitems(items):
    """Copy each ``srb_weight`` argument onto the item so the report can see it."""
    for item in items:
        marker = item.get_closest_marker("srb_weight")
        if marker and marker.args:
            item.user_properties.append(("srb_weight", marker.args[0]))
