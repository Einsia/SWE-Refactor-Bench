"""Stage 1's read-only scan: mechanical observations, handed to the reviewer.

Why this exists
---------------
Some of what a migration has to be true of is visible by reading files and by
nothing else.  Whether a retired dependency is still named in a lock file,
whether an entry point still launches the old server, whether a directory called
``vendor/werkzeug`` appeared -- these are answered by opening the tree, and the
answer does not need a build.

Those checks used to live in stage 2, where they were the wrong shape twice over.
Stage 2 builds both sides and compares what they produce; a check in it that
greps a source file is asserting on the implementation rather than on the
behaviour, and it is doing so with the score attached.  ``PATH_INFO`` in a
comment failed a submission.  A ``.js`` asset one byte under a threshold failed a
submission.  The observation was often worth having; scoring on it was not.

So the scan runs here, in the stage that may read and may not execute, and its
findings are *evidence for the review* rather than verdicts of their own:

* every check it emits is recorded with ``required = False``, so no scan finding
  can fail the gate on its own.  ``scoring.grade_audit`` gates on the
  required checks, which are the prose gates and only those;
* the findings are rendered into the reviewer's prompt as a digest, so a model
  that would otherwise have to grep for a lock file entry is handed it and
  spends its turns on the question the gate actually asks;
* the reviewer is told, in the prompt, that a finding is a lead and not a
  verdict.  A tool report of "the token ``wsgi`` appears in app.py" is worth
  following; whether it means the old protocol is still being served is a
  judgement, and the judgement is the model's.

That inversion is the whole point.  A string match cannot decide a migration, but
it is an excellent way to decide *where to look*, and stage 1 is the stage that
can look.

The contract with a scan module
------------------------------
Identical to a behavioural module's -- deliberately, so that a module can be moved
between the stages without rewriting it -- with one difference in what it is
allowed to do::

    inputs (environment)
      SRB_REPO        the submission, exactly as delivered.  READ ONLY.
      SRB_ORIGINAL    State A, read-only
      SRB_MODULE_DIR  this module's own directory
      SRB_SUITE_DIR   tests/scan, for shared helpers in lib/
      SRB_WORK        scratch that already exists
      SRB_SUITE_WORK  scratch shared across scan modules
      SRB_RESULT      where to write {"checks": [...]}
      SRB_MODULE_ID   the module's declared id
      SRB_SCAN        "1" -- a module shared with stage 2 can tell where it is

    output
      $SRB_RESULT, in the stage-result shape, same as a behavioural module.

    exit code
      Advisory, same as a behavioural module.

A scan module must not build, install, start or otherwise execute the submission.
Nothing sandboxes that: the stage-1 image has no toolchain, which is the same
argument the rest of the ladder makes -- the image a stage runs in is what makes
its contract true, and a review that could build would be grading a build log.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import SCAN_SCHEMA, Suite
from .behavioural import SuiteRunner
from .result import Check, StageResult

#: Prefix on every scan check id, so a scan finding is distinguishable from a
#: gate verdict in a merged report without consulting `required`.
PREFIX = "scan"

#: Findings rendered into the prompt.  A digest, not a dump: a reviewer given
#: 900 lines of tool output reads none of them, and the failures are the part
#: worth reading.  The passes are summarised by count.
MAX_RENDERED = 60


class Scanner:
    """Runs a task's scan suite and reduces it to evidence for the review."""

    def __init__(self, suite: Suite, original: Path, repo: Path, work: Path,
                 log) -> None:
        self.suite = suite
        self.original = Path(original)
        self.repo = Path(repo)
        self.work = Path(work)
        self.log = log

    def run(self) -> StageResult:
        runner = SuiteRunner(self.suite, self.repo, self.original, self.work,
                             self.log)
        runner.result.stage = "audit"
        # A scan module that is also a stage-2 module can branch on this.
        self.suite.env.setdefault("SRB_SCAN", "1")
        res = runner.run()
        for check in res.checks:
            # Evidence, not a gate.  Whatever the module declared, a scan finding
            # does not decide the stage: `grade_audit` reads `required`.
            check.required = False
            check.id = f"{PREFIX}/{check.id}"
            check.metadata["advisory"] = True
        for unit in res.units:
            unit.metadata["advisory"] = True
            unit.metadata.pop("required", None)
        return res


def digest(checks: list[Check], units: list | None = None, *,
           limit: int = MAX_RENDERED) -> str:
    """Render scan findings for the reviewer's prompt.

    Failures first and in full, because they are the leads.  Passes as a count,
    because "412 checks found nothing" is the whole of what they say and listing
    them would bury the twelve that did.

    ``units`` is how a module that produced *no* checks gets said out loud.  A
    module that dies contributes nothing to ``checks``, so counting checks cannot
    see it: the summary line reports "0 could not run" and, if the surviving
    modules were quiet, the next line says nothing was flagged.  That is a clean
    bill of health for a scan that did not run -- measured on this benchmark, a
    tree whose retired framework was entirely intact rendered as "57 observations,
    57 found nothing" once the module that asks about the framework failed to
    collect.  The build-time collect-check exists to stop precisely this, and it
    can only stop the version of it that is true at build time.

    Passing units is optional so that a caller with only checks still works, but a
    caller that has them should hand them over; there is no way to recover a dead
    module from its checks, because it has none.
    """
    dead = [u for u in (units or [])
            if getattr(u, "status", "ok") not in ("ok", "skip")]
    empty = [u for u in (units or [])
             if getattr(u, "status", "ok") == "ok"
             and not any((c.unit or "") == u.id for c in checks)]

    alarm: list[str] = []
    if dead or empty:
        alarm = [
            "SCAN INCOMPLETE. Some of it did not run, so what follows is not a "
            "survey of the tree -- it is a survey of the part that worked. Treat "
            "the silence of these modules as no information, not as a pass:",
            "",
        ]
        for unit in dead:
            why = _one_line(getattr(unit, "summary", "") or "", 200)
            alarm.append(f"  - {unit.id}: status={unit.status}"
                         + (f" -- {why}" if why else ""))
        for unit in empty:
            alarm.append(f"  - {unit.id}: reported success and wrote no checks, "
                         f"which means it measured nothing")
        alarm.append("")
        alarm.append("Read those areas of both trees yourself. The gates below are "
                     "yours to decide and none of them depends on this scan.")
        alarm.append("")

    if not checks:
        return "\n".join(alarm) + "(the scan produced no findings)"

    failed = [c for c in checks if c.verdict == "fail"]
    errored = [c for c in checks if c.verdict == "error"]
    passed = sum(1 for c in checks if c.verdict == "pass")
    skipped = sum(1 for c in checks if c.verdict == "skip")

    lines: list[str] = alarm + [
        f"{len(checks)} mechanical observation(s): {passed} found nothing, "
        f"{len(failed)} found something, {len(errored)} could not run"
        + (f", {skipped} did not apply" if skipped else ""),
        "",
    ]
    if not failed and not errored:
        lines.append("Nothing was flagged. That is not a verdict on the gates "
                     "below -- the scan can only see what a string can see."
                     if not (dead or empty) else
                     "Nothing was flagged by the modules that ran, which given "
                     "the above is close to no information at all.")
        return "\n".join(lines)

    shown = (failed + errored)[:limit]
    lines.append("Flagged, in the scan's own words. Each is a place to look, not "
                 "a finding you may report as your own:")
    lines.append("")
    for check in shown:
        head = f"  - [{check.verdict}] {check.id}"
        if check.summary:
            head += f": {_one_line(check.summary)}"
        lines.append(head)
        for cite in check.evidence[:3]:
            path = cite.get("path")
            if not path:
                continue
            line = cite.get("line")
            where = f"{path}:{line}" if line else str(path)
            quote = _one_line(str(cite.get("quote") or ""), 120)
            lines.append(f"      {where}" + (f"  {quote!r}" if quote else ""))
        # Two subtractions from the same 240-character window, each measured on a
        # different real review, and neither one implies the other: `_message`
        # drops pytest's frame (83 of the 240, restating the check id printed on
        # the line above), `_detail_beyond_summary` drops the headline the detail
        # opens by repeating (a mean of 112 of the 240 across 23 of 24 findings).
        # Composed in that order because the frame sits *before* the repeated
        # headline, so removing it first is what lets the prefix match at all.
        # `_message` is called without `already_said`: its own prefix strip is the
        # weaker of the two -- no leading-punctuation strip, and no rule for a
        # detail that is a tail rather than a restatement -- so that half of the
        # job is left to the function whose docstring measured it.
        detail = _detail_beyond_summary(_message(check.detail), check.summary)
        if detail:
            lines.append(f"      {detail}")
    hidden = len(failed) + len(errored) - len(shown)
    if hidden > 0:
        lines.append(f"  ... and {hidden} more, in the stage result file.")
    return "\n".join(lines)


def _one_line(text: str, limit: int = 300) -> str:
    text = " ".join((text or "").split())
    return text[:limit] + ("..." if len(text) > limit else "")


def _detail_beyond_summary(detail: str, summary: str, limit: int = 240) -> str:
    """The detail line, carrying only what the headline did not already say.

    A pytest assertion is written ``assert not offenders, (f"<sentence>:\\n{json}")``
    -- the natural idiom, and the right one.  The recorder splits that into a summary
    (line one) and a detail (the whole message), so the detail *opens by repeating
    the headline* and the repetition is charged against this line's budget.

    That was measured on fw02's live submission rather than assumed: 23 of 24 flagged
    findings restated their own summary here, a mean of 112 of the 240 characters, and
    six were left with nothing at all for the file paths -- which appear nowhere else,
    since ``evidence`` is empty in this suite and the headline stops at the colon.  So
    the reviewer read the same sentence twice and was shown fewer of the offending
    files for it.

    The truncation therefore has to happen *after* the repeated prefix is removed, not
    before.  Comparing the two truncated strings, as this did previously, can only
    catch a detail that is nothing but its summary -- the guard was already reaching
    for this, and was simply too weak to see a prefix.

    What this does *not* do is change what the recorder stores.  The duplication is
    real in the artifact too, and fixing it there would move a number that stage 2's
    identity run is pinned to; here it changes only what the reviewer is handed, which
    is the channel that was actually losing information.
    """
    flat_detail = " ".join((detail or "").split())
    flat_summary = " ".join((summary or "").split())
    if not flat_detail:
        return ""
    if flat_summary and flat_detail.startswith(flat_summary):
        flat_detail = flat_detail[len(flat_summary):]
        # A summary is line one capped at 300 characters, so when the first line was
        # longer the strip leaves its remainder -- which is wanted: the headline ends
        # in an ellipsis and the detail resumes exactly where it was cut.
    #
    # Anything that does *not* open with its summary is kept whole.  Measured across
    # every result on disk: 4239 of 4453 checks restate their summary here, 214 do
    # not, and all 214 are a traceback or the tail of a message longer than the
    # recorder's 1800-character detail cut.  A tail is not a restatement.
    flat_detail = flat_detail.lstrip(" :;,-")
    if not flat_detail:
        return ""
    return flat_detail[:limit] + ("..." if len(flat_detail) > limit else "")


def _message(detail: str, *, already_said: str = "") -> str:
    """The part of a pytest report worth spending the digest's 240 characters on.

    Two subtractions, both measured on a real review.  A ``detail`` straight from
    the plugin opens with the frame -- ``modules/dispatch/test_dispatch.py:116: in
    test_report_the_registration_table pytest.fail( E Failed:`` -- which is 83 of
    the 240, a third of the window, spent restating the check id printed on the
    line above it.  And ``summary`` is now the first ``E`` line, so rendering the
    detail from its start repeats that sentence twice in consecutive lines.

    Dropping both gave a scan report 3.4x the reviewer-visible text on the same
    budget.  The frame is not lost: it is in the stage result file, which is where
    someone reproducing a check looks anyway.

    A detail with no ``E`` block -- a verifier's own prose, an verification round's
    error -- is returned unchanged, because there is no frame in it to drop.
    """
    text = detail or ""
    # The elision marker is kept, and matched on the word rather than on the shape.
    # This arrived matching a leading ``...``, which was the marker's spelling on
    # the branch that wrote this; the merged `pytest_module._detail` emits
    # ``[srb: N characters elided from the middle of this block]``, so a
    # shape-matched filter would have dropped the very marker whose purpose is to
    # stop the gap from being invisible -- silently, and only in production, since
    # the test for it builds the old spelling by hand.
    marked = [ln for ln in text.splitlines()
              if ln.lstrip().startswith(("E ", "E\t"))
              or "elided" in ln]
    if marked:
        text = "\n".join(ln.lstrip().removeprefix("E").strip() for ln in marked)
    said = _one_line(already_said)
    flat = _one_line(text, limit=len(text) + 1)
    if said and flat.startswith(said):
        text = flat[len(said):].strip()
    return text


def build(suite_path: Path, original: Path, repo: Path, work: Path,
          log) -> Scanner:
    """Assemble a scanner from a task's ``tests/scan/scan.toml``."""
    suite = Suite.load(suite_path, schema=SCAN_SCHEMA)
    return Scanner(suite, original, repo, work, log)


def summary(res: StageResult) -> dict[str, Any]:
    """What the audit result records about the scan, beside the gates."""
    return {
        "modules": len(res.units),
        "checks": len(res.checks),
        "flagged": sum(1 for c in res.checks if c.verdict == "fail"),
        "errored": sum(1 for c in res.checks if c.verdict == "error"),
        "status": res.status,
    }
